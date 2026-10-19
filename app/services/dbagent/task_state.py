"""任务态的进程内注册表与 PG 读写原语（批次 F 步骤 2 自 tasks.py 拆出）。

这条链的最底层：只引标准库与 pools/db/metrics/agent，不引 ledger/gates/tasks，
所以拆完不可能形成导入环。_RUNNERS/_GATES/_ABANDONED/_PENDING_STEPS 仍是**同一批
对象**——上层与测试经 re-export 拿到的就是这里那份（改内容有效，改绑定无效）。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from ...core.db import get_pool


logger = logging.getLogger("rag_console.dbagent")


TASK_TTL_SECONDS = 3600.0   # 终态保留时长（过期清理）


_MAX_ACTIVE = 3             # 并发运行上限默认值（真实值 env DBAGENT_MAX_ACTIVE，P2-1 配置化）


# 业主 2026-10-05 口径（外部建议 #6）：内部人员使用，准确执行优先于速度
# ——门等待与墙钟大幅放宽，"不容易超时"；不死循环仍由步数/token 预算与
# should_stop 兜底。env 可按部署覆盖。
_TASK_WALL_TIMEOUT = float(os.environ.get("DBAGENT_TASK_WALL_TIMEOUT", "7200"))


_GATE_DECISION_TIMEOUT = float(os.environ.get("DBAGENT_GATE_TIMEOUT", "1800"))


# Planner-Executor 拆分（业主 2026-10-05 批准）默认关：开启后每个任务多一次
# 规划调用（+1 次 LLM），适合复杂多步分析为主的使用场景
_PLANNER_DEFAULT = os.environ.get("DBAGENT_PLANNER", "") in ("1", "true", "True")


# P2-1 门事件驱动唤醒：审批决定落库后 pg_notify 即时唤醒挂起门，
# 1s 轮询保留为兜底（监听断开只损失即时性，不损失正确性）
_GATE_CHANNEL = "dbagent_gate"


_listener: dict = {"conn": None, "starting": False}


def _on_gate_notify(conn, pid, channel, payload: str) -> None:
    """监听回调：notify 在决定落库的事务内发出，唤醒后轮询必读到决定。"""
    handle = _GATES.get(payload)
    if handle:
        handle["event"].set()


def _claim_background(task: asyncio.Task) -> None:
    """后台任务异常认领（Review 规则：create_task 必须挂认领回调）——
    send_alert 自身 fail-safe，此处只兜底其前置同步段的意外。"""
    if not task.cancelled() and task.exception() is not None:
        logger.warning("后台任务异常（已认领）: %s: %s",
                       type(task.exception()).__name__,
                       str(task.exception())[:120])


def _fire_alert(title: str, text: str) -> None:
    """审批事件即时外送（T1，2026-10-06）：提审/超时即触达 webhook
    （钉钉/飞书/内网 IM，ALERT_WEBHOOK_URL 配置；未配置静默跳过）。
    fire-and-forget 不阻塞门；标题含 task_id 避免跨任务被节流吞并。"""
    from ...core.alerting import send_alert

    asyncio.get_running_loop().create_task(
        send_alert(title, text)).add_done_callback(_claim_background)


async def _ensure_gate_listener() -> None:
    """懒启动门通知监听（首次挂门时连一次，进程生命周期内复用）。

    连接 DSN 按序试：DATABASE_URL（容器视角 host=postgres，与应用池同库
    同账号）→ DBAGENT_ADMIN_DSN（宿主视角 127.0.0.1）。readonly/writer
    角色限额已被池占满（2/1），监听专用连接多开必撞角色上限（部署实测）。
    失败模式全部退化为纯轮询：连接失败/断开 -> conn 置 None，下次挂门时
    检查并重连。
    """
    import os

    if _listener["conn"] is not None:
        if not _listener["conn"].is_closed():
            return
        _listener["conn"] = None  # 断线：清理后重连
    if _listener["starting"]:
        return
    _listener["starting"] = True
    try:
        import asyncpg

        candidates = [os.environ.get("DATABASE_URL", ""),
                      os.environ.get("DBAGENT_ADMIN_DSN", "")]
        conn = None
        for dsn in filter(None, candidates):
            try:
                conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=3.0)
                break
            except Exception as exc:  # noqa: BLE001 — 换下一个候选，留痕
                logger.info("[gate-trace] 门监听候选 DSN 连接失败（%s），试下一个",
                            type(exc).__name__)
                continue
        if conn is None:
            raise RuntimeError("无可用 DSN（候选均连不上或未配置）")
        await conn.add_listener(_GATE_CHANNEL, _on_gate_notify)
        _listener["conn"] = conn
        logger.info("[gate-trace] 门监听就绪（LISTEN %s）", _GATE_CHANNEL)
    except Exception as exc:  # noqa: BLE001 — 监听失败不影响正确性，只损失即时性
        logger.info("[gate-trace] 门监听不可用，退回 1s 轮询: %s: %s",
                    type(exc).__name__, str(exc)[:80])
    finally:
        _listener["starting"] = False


# P1-5：批准后仍强制转人工（proposed）的风险等级。L2=批量重写；unknown=
# 风险评估未完成（changes._assess_write_risk 的口径是"评不出来不假装安全"）
_FORCE_PROPOSE_LEVELS = ("L2", "unknown")


def _forced_by_risk(task_id: str) -> bool:
    """最近一道写门批准的提案风险等级是否达强制转人工档（P1-5）。

    门在批准时把等级暂存 _GATES（同 last_approved_by 通道）；写包装器与
    agent 观察层在紧随其后的 db_write 处读——顺序由 confirm→write 的
    调用序保证（agent._tool_write_sql）。
    """
    return _GATES.get(task_id, {}).get("last_risk") in _FORCE_PROPOSE_LEVELS


_ADVISORY_KEY = 0xDBA6E71   # db_agent_tasks 并发闸的咨询锁键（任意常量）


# 进程内协程/事件句柄（不可序列化，只在本进程存活期内有效）
_RUNNERS: dict[str, asyncio.Task] = {}


_GATES: dict[str, dict] = {}   # task_id -> {"event": Event, "kind": "write"|"plan"}


# P2-2：墙钟终态已落的废弃任务（僵尸循环 should_stop 检查 + 门轮询退出）
_ABANDONED: set[str] = set()


_SNAPSHOT_COLS = ("task_id", "question", "actor", "status", "steps", "report",
                  "proposal", "plan", "usage", "error", "created_at", "finished_at")


def _release_guards(runner: asyncio.Task, task_id: str) -> None:
    """撤除进程内登记，且必须等协程真停下（2026-10-07 C3/P1-8）。

    `should_stop`、门见废弃否决、写包装器拒绝三处守卫全看 `_ABANDONED` 的成员资格。
    墙钟到点时协程可能已被 httpx/anyio 吸收取消而仍在跑（一次 LLM 步可达分钟级），
    此刻 discard 等于放僵尸出去：它醒后判"未废弃"，会把已 failed 的任务翻回
    awaiting_approval、同意门、经批准真实写库。runner 未 done 就挂 done_callback
    延迟清理；协程永挂则标记永久滞留——宁可多占一个 set 元素也不放僵尸（重启即回收）。
    """
    def _release(_task=None):
        _RUNNERS.pop(task_id, None)
        _GATES.pop(task_id, None)
        _ABANDONED.discard(task_id)

    if runner.done():
        _release()
    else:
        runner.add_done_callback(_release)


def _snapshot(row: dict) -> dict:
    """行 -> 对外形状（camel 口径字段名与旧内存版完全一致）。"""
    return {
        "id": row["task_id"],
        "question": row["question"],
        "actor": row["actor"],
        "status": row["status"],
        "steps": row["steps"] if isinstance(row["steps"], list) else _jsonb(row["steps"], []),
        "report": row["report"],
        "proposal": _jsonb(row["proposal"], None),
        "plan": _jsonb(row["plan"], None),
        "usage": _jsonb(row["usage"], None) or {},
        "error": row["error"],
        "created_at": _created_at_num(row),
        "finished_at": _ts_num(row.get("finished_at")),
    }


def _jsonb(value, default):
    """asyncpg 的 jsonb 默认返回 str（未注册解码器时）：惰性解析。"""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _created_at_num(row: dict) -> float:
    v = row.get("created_at")
    return v.timestamp() if hasattr(v, "timestamp") else float(v or 0)


def _ts_num(v) -> float | None:
    if v is None:
        return None
    return v.timestamp() if hasattr(v, "timestamp") else float(v)


async def _fetch_one(conn, task_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT " + ", ".join(_SNAPSHOT_COLS) + " FROM db_agent_tasks "
        "WHERE task_id = $1", task_id)
    return dict(row) if row else None


async def _persist(task_id: str, **cols) -> None:
    """任务字段增量落库（fail-safe：持久化失败记日志不中断任务）。"""
    if not cols:
        return
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(cols))
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE db_agent_tasks SET {sets}, updated_at = now() "
            "WHERE task_id = $1", task_id, *cols.values())


async def _finalize(task_id: str, status: str, **cols) -> None:
    """终态写入：status + finished_at=now()（内联）+ 其余字段。"""
    sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(cols))
    extra = f", {sets}" if sets else ""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE db_agent_tasks SET status = $2, finished_at = now(){extra}, "
            "updated_at = now() WHERE task_id = $1", task_id, status, *cols.values())


async def _claim_gate_timeout(task_id: str) -> bool:
    """审批超时把状态从 awaiting_* 摘走（10-08 台账 450）。

    不摘的话，门已经自动否决、行却还显示待审批：这段窗口里审批人点批准仍会
    成功（决定指标双计：auto_rejected + 人工各一次），中控台也误导。
    CAS 语义——0 行表示刚被人工处理，交给门轮询既有分支去读决定。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE db_agent_tasks SET status = 'running', updated_at = now() "
            "WHERE task_id = $1 "
            "AND status IN ('awaiting_approval', 'awaiting_plan_approval') "
            "RETURNING task_id", task_id)
    return row is not None


async def recover_orphans() -> int:
    """启动回收：进程重启后，运行协程已死而库里仍挂 running/awaiting 的任务
    一律标 failed（双实例下只回收超过墙钟 2 倍的——另一实例可能还活着在跑）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(
            "UPDATE db_agent_tasks SET status='failed', "
            "error='recovered: runner process restarted', finished_at=now(), "
            "updated_at=now() WHERE status IN ('running','awaiting_approval',"
            "'awaiting_plan_approval') "
            "AND created_at < now() - ($1 || ' seconds')::interval",
            str(int(_TASK_WALL_TIMEOUT * 2)))


async def _reap_stale_actives() -> None:
    """滚动回收超龄活跃任务（P2-2 配套，2026-10-04 评测中途重启实测抓到）。

    recover_orphans 只在进程启动跑一次；实例在任务挂门时被重启/kill，
    孤儿要等下一次重启才被收——期间占着全局槽与 per-user 槽（新任务被 429）。
    并入每次提交都跑的清理路径：条件与启动回收一致（>2 倍墙钟 = 无活实例
    在跑），双实例安全。
    """
    try:
        await recover_orphans()
    except Exception as exc:  # noqa: BLE001 — 回收失败不挡提交
        logger.warning("超龄活跃任务回收失败: %s: %s",
                       type(exc).__name__, str(exc)[:80])


_PENDING_STEPS: dict[str, asyncio.Task] = {}


def _append_step(task_id: str, steps: list, entry: dict) -> None:
    """步骤时间线追加（内存缓冲 + 去抖落库；前端轮询渲染）。

    生产差距 #11：每步全量重写 steps JSON 是 O(n²) 写放大——0.5s 合并
    窗口内的追加由在途落库一次性带上（steps 列表共享，落库时序列化到的
    是最新全量）；轮询间隔 1.5s，窗口不损 UI 时效。"""
    steps.append({**entry, "at": time.time()})
    pending = _PENDING_STEPS.get(task_id)
    if pending is not None and not pending.done():
        return
    task = asyncio.get_running_loop().create_task(
        _persist_steps_debounced(task_id, steps))
    task.add_done_callback(_claim_background)
    _PENDING_STEPS[task_id] = task


async def _persist_steps_debounced(task_id: str, steps: list) -> None:
    try:
        await asyncio.sleep(0.5)  # 合并窗口
        try:
            await _persist(task_id, steps=json.dumps(steps, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — 步骤落库失败不中断执行
            logger.debug("steps 持久化失败 task=%s", task_id)
    finally:
        _PENDING_STEPS.pop(task_id, None)
