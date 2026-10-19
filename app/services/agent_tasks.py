"""Agent 任务治理：创建/等待/取消/恢复 + 后台并发槽位上限

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
含 P1 #42：幂等改为 claim_idempotency 原子 SET NX 占位，杜绝 GET 后再创建的竞态。
"""
import asyncio
import json
import os
import time

from fastapi import HTTPException

from ..models.schemas import CreateTaskRequest
from ..core.task_manager import (
    create_task, get_task, request_cancel, cancel_task_if_active,
    claim_idempotency, release_idempotency_claim, save_idempotent, read_accumulated_result,
    CLAIM_IN_PROGRESS,
)
from ..agents.runner import run_agent_task
from ..core.redis import get_redis, incr_arm_ttl
from ..core.quota import reserve_used_questions, rollback_used_questions, QuotaDependencyError
from ..core.logging import setup_logging

logger = setup_logging()

# ---------- 后台任务并发上限（P1 #18：防恶意用户无限创建任务拖垮 LLM/DB）----------
# 2026-09-14 审计 P1：进程内计数在 4 实例部署下全局上限失效（实例数 x 50），
# 改 Redis 原子计数共享全局槽位；Redis 故障 fail-open（放行 + 告警，不阻断任务创建）
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "50"))
_TASK_SLOT_KEY = "task:active_slots"
MAX_CONCURRENT_TASKS_PER_USER = int(os.getenv("MAX_CONCURRENT_TASKS_PER_USER", "5"))
_USER_TASK_SLOT_PREFIX = "task:active_slots:user:"


async def _try_reserve_task_slot() -> bool | None:
    """尝试预留一个全局后台任务槽位（Redis INCR/DECR 原子计数）；已满返回 False

    计数与 TTL 一次原子下发（2026-10-08 第四轮 D1）：原先 INCR、超限 DECR、
    EXPIRE 分三条命令，进程在 INCR 与 EXPIRE 之间被杀就留下无 TTL 的槽位计数，
    泄漏的槽位永不归零——全局上限 50 会被慢慢吃干，之后所有后台任务都起不来。
    TTL 口径同时从"每次预留都续期"改为"缺 TTL 才挂"（同一条 P2 的成因：有流量
    就永不自愈）。代价是键到期时计数直接归零，若那一刻仍有任务在跑会出现一段
    短暂超卖；相比原先的永久欠放，可自愈的超卖是可接受的那一侧。
    """
    try:
        r = await get_redis()
        count = await incr_arm_ttl(r, _TASK_SLOT_KEY, 86400)  # 全实例都停时可自愈清零
        if count > MAX_CONCURRENT_TASKS:
            await r.decr(_TASK_SLOT_KEY)  # 原子回滚，不占槽
            return False
        return True
    except Exception as e:
        logger.warning(f"任务槽位 Redis 计数失败（fail-open 放行）: {e}")
        return None


async def _try_reserve_user_task_slot(username: str) -> bool | None:
    """预留单用户任务槽，防一个账号占满全局后台任务并发。

    口径同 _try_reserve_task_slot（原子下发 + 缺 TTL 才挂）：这里泄漏一个槽位
    就是把该账号永久压在 per-user 上限之下，比短暂超卖更糟。
    """
    key = f"{_USER_TASK_SLOT_PREFIX}{username}"
    try:
        r = await get_redis()
        count = await incr_arm_ttl(r, key, 86400)
        if count > MAX_CONCURRENT_TASKS_PER_USER:
            await r.decr(key)
            return False
        return True
    except Exception as e:
        logger.warning(f"用户任务槽位 Redis 计数失败（fail-open 放行）: {e}")
        return None


async def _release_user_task_slot(username: str) -> None:
    """回收单用户任务槽，防计数下溢。"""
    key = f"{_USER_TASK_SLOT_PREFIX}{username}"
    try:
        r = await get_redis()
        count = await r.decr(key)
        if count < 0:
            await r.set(key, 0)
    except Exception as e:
        logger.warning(f"用户任务槽位释放失败（TTL 自愈）: {e}")


def _task_conversation_id(task: dict) -> str:
    """从任务存储读取会话ID（兼容旧任务仍存 session_id 字段的情况）"""
    return task.get("conversation_id", "") or task.get("session_id", "")


def task_user_perms(current_user: dict) -> list | None:
    """知识库检索权限（P0 修复）：admin 不过滤（None），普通用户按权限组、缺省仅公开。

    与 SSE 路径 chat_stream_ctx 同规则。任务路径曾对 search_knowledge 硬编码
    permissions=None（不过滤语义），普通用户经任务端点可越权检索 VIP 知识块。
    """
    if current_user.get("role") == "admin":
        return None
    return current_user.get("permissions") or []


def ensure_task_access(task: dict, username: str, role: str) -> None:
    """任务归属校验：仅 owner 本人（或 admin）可读/取消/恢复。

    缺 owner 字段（部署切换瞬间的旧任务）一律拒绝非 admin 访问——
    任务 TTL 仅 1 小时，宁可误拒也不开跨用户泄露口子（P1）。
    用 404 而非 403，不向探测者泄露他人任务的存在性。
    """
    if role == "admin":
        return
    if task.get("owner") != username:
        raise HTTPException(status_code=404, detail="task not found")


async def _task_quota_gate(username: str, role: str) -> bool:
    """任务端点按用户配额（2026-09-07 审查 P2）：任务路径原先只有全局 50 槽位、
    无任何按用户限额，登录用户可刷 LLM 配额。QPS + 日 req/token 三项与
    ensure_chat_allowed 同口径（UTC 日切）；并发槽位不适用后台任务（长生命周期
    占位会误伤正常排队），不纳入。
    """
    from ..middleware.rate_limit import (
        check_qps, reserve_daily_request,
    )
    if not await check_qps(username, role):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    from ..core.day_window import quota_day; today = quota_day()
    reserved = await reserve_daily_request(username, today, role)
    if reserved == 1:
        raise HTTPException(status_code=429, detail="今日请求次数已达上限")
    if reserved == 2:
        raise HTTPException(status_code=429, detail="今日 Token 消耗已达上限")
    # Redis 降级时未写入计数，调用方不得在失败路径误减已有额。
    return reserved == 0


async def _release_task_slot() -> None:
    """回收全局后台任务槽位（任务结束或启动失败时调用）"""
    try:
        r = await get_redis()
        n = await r.decr(_TASK_SLOT_KEY)
        if n < 0:
            await r.set(_TASK_SLOT_KEY, 0)  # 防下溢：异常重建等场景兜回 0
    except Exception as e:
        logger.warning(f"任务槽位释放失败（TTL 自愈）: {e}")


async def _launch_agent_task(**kwargs) -> None:
    """启动后台 Agent 任务，任务结束后自动释放并发槽位（防止计数泄漏）"""
    run_kwargs = {
        key: value for key, value in kwargs.items()
        if not key.startswith("release_")
    }

    async def _wrapped() -> None:
        """任务协程包装：无论成败都释放并发槽位，防计数泄漏"""
        try:
            await run_agent_task(**run_kwargs)
        finally:
            if kwargs.get("release_global_slot"):
                await _release_task_slot()
            if kwargs.get("release_user_slot"):
                await _release_user_task_slot(kwargs.get("username", ""))
    # spawn 持强引用：裸 create_task 的后台任务可被 GC 中途回收（2026-09-07 审查 P2）
    from ..core.concurrency import spawn
    spawn(_wrapped(), name=f"agent-task:{kwargs.get('task_id', '')}")


async def _persist_task_fields(task_id: str, persona_id: str,
                               file_ids: list, lang: str) -> None:
    """写入恢复任务所需的 persona/file/lang 字段。"""
    r = await get_redis()
    await r.hset(f"task:{task_id}", mapping={
        "persona_id": persona_id or "",
        "file_ids": json.dumps(file_ids or []),
        "lang": lang or "",
    })


async def create_agent_task(req: CreateTaskRequest, current_user: dict) -> dict:
    """创建 Agent 生成任务：幂等占位 → 并发槽位 → 建任务 → 后台启动

    幂等键含 username 防跨用户串号（P0 #5）；任务存储保留 persona_id/file_ids
    供 /resume 透传（P0 #6）。
    """
    username = current_user["username"]
    role = current_user.get("role", "user")
    from ..core.day_window import quota_day; today = quota_day()
    # 幂等：同一 username+conversation + 相同消息 10秒内复用（P0 #5：键含 username 防跨用户串号；
    # P1 #42：SET NX 原子占位替代 GET 后再创建的竞态窗口）。
    # 2026-09-14 审计 P1：占位等待超时返回 CLAIM_IN_PROGRESS——创建方仍在建或已崩溃，
    # 此时并发再建第二个任务正是幂等占位要防的事，改拒绝（409）让 TTL 自愈后重试
    existing = await claim_idempotency(f"{username}:{req.conversation_id}", req.message)
    if existing == CLAIM_IN_PROGRESS:
        raise HTTPException(status_code=409, detail="相同请求正在创建中，请稍候重试")
    if existing:
        return {"task_id": existing, "idempotent": True}

    slot_reserved = False
    user_slot_reserved = False
    quota_reserved = False
    daily_reserved = False
    try:
        # 先复用幂等结果再计配額，避免重复请求重复增加日请求数。
        daily_reserved = await _task_quota_gate(username, role)
        # 三态（2026-09-14 审计 P1）：额度耗尽 402；DB 故障 503（不再误报为额度用尽）
        try:
            _reserved = await reserve_used_questions(current_user)
        except QuotaDependencyError:
            raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后再试")
        if not _reserved:
            raise HTTPException(status_code=402, detail="免费额度已用完，请绑定手机号后继续使用")
        quota_reserved = True
        slot_state = await _try_reserve_task_slot()
        if slot_state is False:
            raise HTTPException(status_code=429, detail="系统任务已满，请稍后再试")
        slot_reserved = slot_state is True
        user_slot_state = await _try_reserve_user_task_slot(username)
        if user_slot_state is False:
            raise HTTPException(status_code=429, detail="当前账号任务过多，请稍后再试")
        user_slot_reserved = user_slot_state is True
        task_id = await create_task(req.conversation_id, req.message, owner=username)
        await _persist_task_fields(
            task_id, req.persona_id, req.file_ids or [], req.lang)
        await save_idempotent(
            f"{username}:{req.conversation_id}", req.message, task_id,
        )

        # 启动后台任务（任务结束自动释放并发槽位）
        await _launch_agent_task(
            task_id=task_id, username=username, conversation_id=req.conversation_id,
            user_query=req.message,
            persona_id=req.persona_id, file_ids=req.file_ids, lang=req.lang,
            user_perms=task_user_perms(current_user),
            release_global_slot=slot_reserved,
            release_user_slot=user_slot_reserved,
        )
        slot_reserved = False
        user_slot_reserved = False
    except Exception:
        await release_idempotency_claim(f"{username}:{req.conversation_id}", req.message)
        if slot_reserved:
            await _release_task_slot()
        if quota_reserved:
            # 仅在成功预留后归还，覆盖槽位满、建任务失败等所有未产出路径。
            await rollback_used_questions(username)
        if daily_reserved:
            from ..middleware.rate_limit import rollback_daily_request
            await rollback_daily_request(username, today)
        if user_slot_reserved:
            await _release_user_task_slot(username)
        raise

    return {"task_id": task_id, "idempotent": False}


async def wait_task_result(task_id: str, task: dict, wait: int) -> dict:
    """查询/等待任务结果；wait=0 立即返回，wait>0 长轮询最多 60 秒（P0 #7）"""
    status = task["status"]
    # 已终结的状态直接返回（含 timeout，P2 C1）
    if status in ("completed", "cancelled", "error", "timeout"):
        result = await read_accumulated_result(task_id) or task.get("result", "")
        return {"status": status, "content": result, "conversation_id": _task_conversation_id(task)}

    max_wait = min(wait, 60) if wait and wait > 0 else 0
    if max_wait and status in ("pending", "generating"):
        deadline = time.time() + max_wait
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            task = await get_task(task_id)
            if not task:
                return {"status": "error", "content": "task not found"}
            st = task["status"]
            if st in ("completed", "cancelled", "error", "timeout"):  # P2 C1：timeout 视为终态
                result = await read_accumulated_result(task_id) or task.get("result", "")
                return {"status": st, "content": result, "conversation_id": _task_conversation_id(task)}
            # 还处于 generating，返回当前进度
            partial = await read_accumulated_result(task_id)
            if partial:
                return {"status": "generating", "content": partial, "conversation_id": _task_conversation_id(task)}

    # 超时或仍在生成中，返回进度
    partial = await read_accumulated_result(task_id) or ""
    return {"status": status, "content": partial, "conversation_id": _task_conversation_id(task)}


async def cancel_agent_task(task_id: str, task: dict) -> dict:
    """取消生成任务（置取消信号 + 标记 cancelled）"""
    # timeout 也是终态（2026-09-07 审查 P2）：漏判会对已超时任务再置 cancelled
    if task["status"] in ("completed", "cancelled", "error", "timeout"):
        return {"status": task["status"], "message": "任务已终结，无需取消"}
    partial = await read_accumulated_result(task_id)
    if not await request_cancel(task_id):
        # 2026-09-12 修复（外部复核 P1）：标记写失败时明确报错，
        # 不让取消被当作成功而任务继续执行
        raise HTTPException(status_code=503, detail="取消服务暂不可用，请稍后重试")
    changed = await cancel_task_if_active(task_id, partial)
    if not changed:
        latest = await get_task(task_id) or {}
        return {
            "status": latest.get("status", "unknown"),
            "message": "任务已终结，无需取消",
        }
    return {"status": "cancelled", "message": "已取消"}


async def _persist_resumed_task(task_id: str, task: dict) -> tuple:
    """复制旧任务的 persona/file/lang 到新任务，返回启动参数。"""
    persona_id = task.get("persona_id") or ""
    file_ids_raw = task.get("file_ids") or ""
    file_ids = json.loads(file_ids_raw) if file_ids_raw else []
    lang = task.get("lang") or "zh"
    await _persist_task_fields(task_id, persona_id, file_ids, lang)
    return persona_id, file_ids, lang


async def resume_agent_task(task_id: str, task: dict, username: str,
                            user_perms: list | tuple | None = (),
                            role: str = "user",
                            quota_user: dict | None = None) -> dict:
    """重新生成（创建新任务，丢弃旧草稿）；仅已取消的任务可恢复

    user_perms 默认仅公开（fail-closed，不可变 () 而非 []/None，理由见
    runner.run_agent_task）；由路由层传 task_user_perms(current_user)。
    role 供 resume 前的配额复查（2026-09-07 审查 P2）。
    """
    if task["status"] != "cancelled":
        return {"error": "只有已取消的任务才能恢复", "status": task["status"]}

    resume_scope = f"resume:{task_id}"
    existing = await claim_idempotency(resume_scope, task["user_message"])
    if existing == CLAIM_IN_PROGRESS:
        # 与 create 同口径（2026-09-14 审计 P1）：占位超时拒绝而非并发再建
        raise HTTPException(status_code=409, detail="相同请求正在创建中，请稍候重试")
    if existing:
        return {"task_id": existing, "idempotent": True, "previous_task_id": task_id}

    slot_reserved = False
    user_slot_reserved = False
    quota_reserved = False
    daily_reserved = False
    from ..core.day_window import quota_day; today = quota_day()
    try:
        # 恢复也是一次新 LLM 任务：试用额度与日配额复查（原 create 有、resume 漏）
        daily_reserved = await _task_quota_gate(username, role)
        quota_view = quota_user or {
            "username": username, "role": role, "quota_limited": False,
        }
        # 三态（2026-09-14 审计 P1）：额度耗尽 402；DB 故障 503
        try:
            _reserved = await reserve_used_questions(quota_view)
        except QuotaDependencyError:
            raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后再试")
        if not _reserved:
            raise HTTPException(status_code=402, detail="免费额度已用完，请绑定手机号后继续使用")
        quota_reserved = True
        slot_state = await _try_reserve_task_slot()
        if slot_state is False:
            raise HTTPException(status_code=429, detail="系统任务已满，请稍后再试")
        slot_reserved = slot_state is True
        user_slot_state = await _try_reserve_user_task_slot(username)
        if user_slot_state is False:
            raise HTTPException(status_code=429, detail="当前账号任务过多，请稍后再试")
        user_slot_reserved = user_slot_state is True
        new_task_id = await create_task(_task_conversation_id(task), task["user_message"],
                                        owner=username)
        persona_id, file_ids, lang = await _persist_resumed_task(new_task_id, task)

        # 2026-09-12 清欠 P2：幂等落地必须先于 launch——launch 的 _wrapped 结束时会
        # 释放槽位，若 save 抛异常再走 except 释放即同槽位双释放（超限放行）；
        # 且先落幂等映射可让并发 resume 在新任务启动前就复用
        await save_idempotent(resume_scope, task["user_message"], new_task_id)
        await _launch_agent_task(
            task_id=new_task_id, username=username, conversation_id=_task_conversation_id(task),
            user_query=task["user_message"],
            persona_id=persona_id, file_ids=file_ids, lang=lang,
            user_perms=user_perms,
            release_global_slot=slot_reserved,
            release_user_slot=user_slot_reserved,
        )
        slot_reserved = False
        user_slot_reserved = False
    except Exception:
        await release_idempotency_claim(resume_scope, task["user_message"])
        if slot_reserved:
            await _release_task_slot()
        if quota_reserved:
            await rollback_used_questions(username)
        if daily_reserved:
            from ..middleware.rate_limit import rollback_daily_request
            await rollback_daily_request(username, today)
        if user_slot_reserved:
            await _release_user_task_slot(username)
        raise

    return {"task_id": new_task_id, "previous_task_id": task_id}
