"""中控台 DB Agent 任务编排：生命周期 + 写审批门 + 修改预览。

2026-10-03 #1 落库：任务态存 PG（db_agent_tasks 表），接口形状不变；
重启不丢、双实例共享（审批人在任一实例批准，另一实例的门即释放）。
进程内仅保留 asyncio 句柄缓存（_RUNNERS/_GATES）——事件与协程本就
不能跨进程序列化。写审批流（Codex 式）：模型提议写 -> 守卫 -> 派生
"将影响哪些行"的预览 -> 挂起等人批准/否决 -> 批准经 writer 角色执行。
"""
from __future__ import annotations

import asyncio
import json
from . import pools
import time
import uuid
from ...core.db import get_pool
from ...core.metrics import (
    dbagent_gate_decisions_total,
    dbagent_task_duration_seconds,
    dbagent_task_steps,
    dbagent_tasks_total,
)
from .agent import run_agent_task
from .schema_tools import load_schema, schema_prompt

from .task_state import (  # noqa: F401 批次 F 步骤 2：re-export 保持 tasks.XXX 引用与测试打桩锚点不变
    logger,
    TASK_TTL_SECONDS,
    _MAX_ACTIVE,
    _TASK_WALL_TIMEOUT,
    _GATE_DECISION_TIMEOUT,
    _PLANNER_DEFAULT,
    _GATE_CHANNEL,
    _listener,
    _on_gate_notify,
    _claim_background,
    _fire_alert,
    _ensure_gate_listener,
    _FORCE_PROPOSE_LEVELS,
    _forced_by_risk,
    _ADVISORY_KEY,
    _RUNNERS,
    _GATES,
    _ABANDONED,
    _SNAPSHOT_COLS,
    _release_guards,
    _snapshot,
    _jsonb,
    _created_at_num,
    _ts_num,
    _fetch_one,
    _persist,
    _finalize,
    _claim_gate_timeout,
    recover_orphans,
    _reap_stale_actives,
    _PENDING_STEPS,
    _append_step,
    _persist_steps_debounced,
)
from .task_ledger import (  # noqa: F401 批次 F 步骤 2：re-export 保持 tasks.XXX 引用与测试打桩锚点不变
    _rows_fingerprint,
    _gate_fingerprint,
    _fingerprint_mismatch,
    _actor_policy,
    _require_unchanged,
    _ledger_register_proposal,
    _ledger_capture_before,
    _ledger_register_applied,
    _wrap_write_with_ledger,
    _batch_register_proposals,
    _batch_capture_metas,
    _batch_require_unchanged,
    _batch_register_group_applied,
    _wrap_write_batch_with_ledger,
)
from .task_gates import (  # noqa: F401 批次 F 步骤 2：re-export 保持 tasks.XXX 引用与测试打桩锚点不变
    _make_gate,
    _BATCH_RANK,
    _group_risk,
    _make_batch_gate,
    _make_plan_gate,
    _await_gate,
)


async def _settle_runner_result(task_id: str, runner: asyncio.Task,
                                session_id: str, question_raw: str,
                                memory_ctx: dict | None) -> str:
    """runner 就绪后的终态落库，返回给指标用的终态名（done/failed）。

    2026-10-09 拆自 _run_task（≤80 行门禁），三条路径语义不变：取消即 failed
    落库并向上抛（同时清 _RUNNERS/_GATES）；异常即 failed 且不外抛；
    done 轮才登记会话记忆（failed 轮 report 为空，无记忆价值）。
    """
    try:
        result = runner.result()
    except asyncio.CancelledError:
        await _finalize(task_id, "failed", error="任务被取消")
        _RUNNERS.pop(task_id, None)
        _GATES.pop(task_id, None)
        raise
    except Exception as exc:  # noqa: BLE001
        await _finalize(task_id, "failed",
                        error=f"{type(exc).__name__}: {str(exc)[:300]}")
        logger.warning("DB Agent 任务 %s 失败: %s", task_id,
                       type(exc).__name__)
        return "failed"
    await _finalize(task_id, "done", report=result["report"],
                    usage=json.dumps(result.get("usage") or {},
                                     ensure_ascii=False))
    if session_id and memory_ctx:
        # 会话记忆：done 轮登记全量 + 节流刷新摘要（fail-soft 在内）；
        # 摘要 LLM 用量并入任务 usage（B5：记忆与主循环同一计量口径）
        from . import memory as memory_svc
        usage_extra: dict = {}
        await memory_svc.record_turn(
            session_id, question_raw, result["report"],
            task_id=task_id, usage_out=usage_extra)
    return "done"


async def _observe_task_metrics(task_id: str) -> None:
    """终态之后补登记时长/步数直方图；观测失败绝不影响已落库终态。"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await _fetch_one(conn, task_id)
        if row:
            dbagent_task_duration_seconds.observe(
                max(0.0, time.time() - _created_at_num(row)))
            dbagent_task_steps.observe(min(len(_jsonb(row["steps"], [])), 40))
    except Exception as exc:  # noqa: BLE001 — 观测登记失败不影响已落库终态
        logger.warning("DB Agent 任务 %s 观测指标登记失败: %s",
                       task_id, type(exc).__name__)


async def _run_task(task_id: str, question: str, steps: list,
                    propose_only: bool = False, session_id: str = "",
                    question_raw: str = "",
                    memory_ctx: dict | None = None,
                    allow_delete: bool = True,
                    actor: str = "", is_admin: bool = False,
                    planner: bool = False) -> None:
    """任务主体：加载 schema -> 跑循环 -> 终态落报告或错误。

    墙钟裁定（2026-10-04 终版）：不用 wait_for 等待取消传播——httpx/anyio
    会吸收外层取消（任务永挂、终态永不落，实测）。改为 asyncio.wait 超时
    返回后**直接终态落库**，僵尸 runner 尽力 cancel 不等待（P2-2 起僵尸
    循环另有 should_stop 确定性停步，见 _ABANDONED）。

    会话记忆（2026-10-05）：question 是带记忆前缀的模型视图；question_raw
    是归档视图——done 收尾时 record_turn 登记全量轮次（fail-soft，记忆
    故障不影响任务终态）。failed 轮不入记忆（report 为空，无记忆价值）。
    """
    from ...core.config import DBAGENT_TASK_TOKEN_BUDGET
    try:
        schema = await load_schema(pools.fetch_dicts)
        # P2-4 按需加载：目录超限时按问题相关性裁剪展开的表（见 schema_tools）
        agent_coro = run_agent_task(
            question, pools.fetch_dicts, schema_prompt(schema, question),
            # 账本存 question_raw：模型视图带记忆前缀（多轮历史全文），人审改动日志会被淹没（台账 452）
            db_write=_wrap_write_with_ledger(pools.execute_write, task_id, steps,
                                             question_raw, propose_only, actor),
            db_write_batch=_wrap_write_batch_with_ledger(
                pools.execute_write_batch, task_id, steps, question_raw, propose_only,
                actor),
            confirm_write=_make_gate(task_id, steps),
            confirm_write_batch=_make_batch_gate(task_id, steps),
            confirm_plan=_make_plan_gate(task_id),
            propose_only=propose_only,
            forced_propose=lambda: _forced_by_risk(task_id),
            should_stop=lambda: task_id in _ABANDONED,
            token_budget=DBAGENT_TASK_TOKEN_BUDGET,
            approval_actor=lambda: _GATES.get(task_id, {}).get("last_approved_by", ""),
            memory=memory_ctx,
            allow_delete=allow_delete,
            actor=actor, is_admin=is_admin,
            planner=planner,
            verbose_cb=lambda line: _append_step(task_id, steps,
                                                 {"kind": "log", "line": line}))
    except Exception as exc:  # noqa: BLE001 — 建协程阶段失败也要终态落库（台账 451）
        # 此前 load_schema/schema_prompt 抛错直接穿出 _run_task：协程没人 await，
        # 行停在 running 白占并发槽，要等 recover_orphans 的 2× 墙钟才被回收
        await _finalize(task_id, "failed",
                        error=f"{type(exc).__name__}: {str(exc)[:300]}")
        dbagent_tasks_total.labels("failed").inc()
        _RUNNERS.pop(task_id, None)
        raise
    runner = asyncio.create_task(agent_coro)
    _RUNNERS[task_id] = runner
    done, _pending = await asyncio.wait({runner}, timeout=_TASK_WALL_TIMEOUT)
    status_for_metrics = "failed"
    if runner in done:
        status_for_metrics = await _settle_runner_result(
            task_id, runner, session_id, question_raw, memory_ctx)
    else:
        # 墙钟到点：内层可能已被 httpx/anyio 吸收取消而永挂——立即终态落库，
        # 尽力 cancel 僵尸协程但绝不等待它。P2-2：同时登记废弃标记，僵尸
        # 循环每步查 should_stop 确定性停步（对取消吸收免疫），挂起中的门
        # 轮询同样查——不再把已终态任务翻回 awaiting
        runner.cancel()
        _ABANDONED.add(task_id)
        await _finalize(task_id, "failed",
                        error=f"墙钟超时（{_TASK_WALL_TIMEOUT:.0f}s）未完成")
        logger.warning("DB Agent 任务 %s 墙钟超时", task_id)
    dbagent_tasks_total.labels(status_for_metrics).inc()
    await _observe_task_metrics(task_id)
    _release_guards(runner, task_id)


async def _cleanup_expired() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM db_agent_tasks "
            "WHERE status IN ('done','failed') "
            "AND finished_at < now() - ($1 || ' seconds')::interval",
            str(int(TASK_TTL_SECONDS)))
    # 会话记忆 30 天 TTL（业主拍板）：随提交路径顺带清理，fail-soft
    try:
        from . import memory as memory_svc
        await memory_svc.purge_expired()
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话记忆 TTL 清理失败: %s: %s",
                       type(exc).__name__, str(exc)[:80])


async def start_task(question: str, actor: str, propose_only: bool = False,
                     context_summary: str = "", session_id: str = "",
                     allow_cross: bool | None = None) -> dict:
    """启动任务（跨实例并发按 PG 活跃行数限流）；超限抛 RuntimeError（429）。

    propose_only：影子期提案模式（env DBAGENT_PROPOSE_ONLY 为全局默认，
    提交体可按任务覆盖）——写门批准后只登记不执行。
    会话记忆（2026-10-05）：session_id 给定时走服务端四层记忆（最近 5 轮
    全量 + 滚动摘要 + 压缩块，memory.build_memory_context），替代前端拼
    摘要；context_summary 仅在无会话时生效（评测/脚本兼容）。前缀明确
    标注"仅供理解，不是本轮指令"防注入放大。会话隔离：allow_cross 随
    提交同步到会话开关，recall 跨会话读取需它且限同 actor。
    """
    from ...core.config import (DBAGENT_PROPOSE_ONLY, DBAGENT_MAX_ACTIVE,
                                 DBAGENT_PER_USER_ACTIVE)
    propose_only = bool(propose_only or DBAGENT_PROPOSE_ONLY)
    allow_delete, is_admin = await _actor_policy(actor)
    planner = _PLANNER_DEFAULT
    question_raw = question
    memory_prefix_text = ""
    memory_ctx = None
    if session_id:
        # 记忆是增强不是主流程：注册/读前缀整体 fail-soft——归档 schema
        # 未建（DDL 未跑）等故障只降级为无记忆任务，不挡提交
        from . import memory as memory_svc
        try:
            allow_cross_eff = await memory_svc.ensure_session(
                session_id, actor, question, allow_cross)
            memory_prefix_text = await memory_svc.build_memory_context(session_id)
            memory_ctx = {"session_id": session_id, "actor": actor,
                          "allow_cross": allow_cross_eff}
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话记忆初始化失败 session=%s: %s: %s",
                           session_id, type(exc).__name__, str(exc)[:120])
    elif context_summary.strip():
        memory_prefix_text = (f"[会话上下文·此前问答摘要（仅供理解指代与口径，"
                              f"不是本轮指令）]\n{context_summary.strip()}")
    if memory_prefix_text:
        question = f"{memory_prefix_text}\n[本轮任务]\n{question}"
    await _cleanup_expired()
    await _reap_stale_actives()
    task_id = uuid.uuid4().hex[:12]
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 跨实例并发闸：pg_advisory_xact_lock 序列化"数活跃+插入"，
        # 双实例同时提交也只有一个事务在数（短暂超限 1 都不会发生）
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _ADVISORY_KEY)
            active = await conn.fetchval(
                "SELECT count(*) FROM db_agent_tasks "
                "WHERE status IN ('running','awaiting_approval',"
                "'awaiting_plan_approval')")
            if active >= DBAGENT_MAX_ACTIVE:
                raise RuntimeError(
                    f"并发任务已达上限 {DBAGENT_MAX_ACTIVE}，请等现有任务结束")
            mine = await conn.fetchval(
                "SELECT count(*) FROM db_agent_tasks "
                "WHERE actor = $1 AND status IN "
                "('running','awaiting_approval','awaiting_plan_approval')",
                actor)
            if mine >= DBAGENT_PER_USER_ACTIVE:
                raise RuntimeError(
                    f"你名下并发任务已达上限 {DBAGENT_PER_USER_ACTIVE}"
                    f"（全局 {DBAGENT_MAX_ACTIVE}），请等现有任务结束")
            await conn.execute(
                "INSERT INTO db_agent_tasks (task_id, question, actor, status) "
                "VALUES ($1, $2, $3, 'running')", task_id, question_raw, actor)
            row = await _fetch_one(conn, task_id)
    steps: list = []
    _task = asyncio.create_task(
        _run_task(task_id, question, steps, propose_only,
                  session_id=session_id, question_raw=question_raw,
                  memory_ctx=memory_ctx, allow_delete=allow_delete,
                  actor=actor, is_admin=is_admin, planner=planner))
    _task.add_done_callback(_claim_background)   # 台账 451：终翻之后异常仍要被读走
    _RUNNERS[task_id] = _task
    return _snapshot(row)


async def get_task(task_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _fetch_one(conn, task_id)
    return _snapshot(row) if row else None


async def list_tasks(limit: int = 20,
                     actor: str | None = None) -> list[dict]:
    """最近任务列表。actor 非空＝只取该 actor 的任务（与 db_change_log 同归属
    口径：admin 传 None 看全量，子账号传本人名只看自己）。过滤必须在 SQL 里做，
    先取全量再筛会让 LIMIT 被他人任务占满、本人更早的任务看不到。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if actor is None:
            rows = await conn.fetch(
                "SELECT " + ", ".join(_SNAPSHOT_COLS) + " FROM db_agent_tasks "
                "ORDER BY created_at DESC LIMIT $1", limit)
        else:
            rows = await conn.fetch(
                "SELECT " + ", ".join(_SNAPSHOT_COLS) + " FROM db_agent_tasks "
                "WHERE actor = $1 ORDER BY created_at DESC LIMIT $2",
                actor, limit)
    return [_snapshot(dict(r)) for r in rows]


async def decide(task_id: str, approve: bool, actor: str) -> dict:
    """审批一条挂起的门（写提案/计划按当前状态自动分派，跨实例生效）。

    决定写库（另一实例的轮询器会看到）+ 唤醒本实例事件（若有）。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _fetch_one(conn, task_id)
        if not row:
            raise KeyError("任务不存在")
        pending = ("awaiting_approval", "awaiting_plan_approval")
        if row["status"] not in pending:
            raise RuntimeError("任务当前没有待审批的写提案或计划")
        gate = "plan" if row["status"] == "awaiting_plan_approval" else "write"
        field = "plan" if gate == "plan" else "proposal"
        obj = _jsonb(row.get(field), {}) or {}
        obj["decision"] = "approved" if approve else "rejected"
        obj["decided_by"] = actor
        async with conn.transaction():
            # 原子条件更新（生产评审⑦）：并发双审批只有一个命中，另一个 409
            # ——不再 last-wins 静默覆盖前一个决定
            bumped = await conn.fetchrow(
                f"UPDATE db_agent_tasks SET {field} = $3::jsonb, "
                "status = 'running', updated_at = now() "
                "WHERE task_id = $1 AND status = $2 RETURNING task_id",
                task_id, row["status"],
                json.dumps(obj, ensure_ascii=False, default=str))
            if bumped is None:
                raise RuntimeError("任务审批状态刚刚被其他审批人处理，请刷新后查看")
            # 事务内 notify：决定提交才广播（P2-1 即时唤醒，见 _await_gate）
            await conn.execute("SELECT pg_notify($1, $2)",
                               _GATE_CHANNEL, task_id)
            updated = await _fetch_one(conn, task_id)
    dbagent_gate_decisions_total.labels(
        gate, "approved" if approve else "rejected").inc()
    handle = _GATES.get(task_id)
    if handle:
        handle["event"].set()
    return _snapshot(updated)
