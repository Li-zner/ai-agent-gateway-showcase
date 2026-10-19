"""中控台 DB Agent 端点：任务 / 审批 / 账本回滚 / 子账号能力管理。

2026-10-03 起按能力授权（capabilities.py）：dbagent 路由从 rag_admin_router
拆出独立挂载（不再整体要求 admin），每个端点声明所需能力——子账号由管理员
在 permissions 允许清单里勾选开通；权限管理端点本身仅 admin。
授权语义：approve 请求即"人已看过命令与它将影响的行"，审计记录决定者。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core.audit import (
    ACT_ADMIN,
    ACT_DBAGENT_CHANGE_ROLLBACK,
    ACT_DBAGENT_PLAN_APPROVAL,
    ACT_DBAGENT_TASK,
    ACT_DBAGENT_WRITE_APPROVAL,
    audit,
)
from ..core.capabilities import (
    CAPABILITIES,
    DEFAULT_SUB_CAPS,
    require_cap,
)
from ..core.db import get_pool
from ..core.password import hash_password
from ..middleware.auth import get_current_user
from ..services.dbagent import changes as dbagent_changes
from ..services.dbagent import memory as dbagent_memory
from ..services.dbagent import pools, tasks

dbagent_router = APIRouter(tags=["admin"])

QUESTION_MIN_CHARS = 4
QUESTION_MAX_CHARS = 500
CONTEXT_SUMMARY_MAX_CHARS = 1200  # 会话上下文摘要上限（前端截带，后端再兜底）


class DbAgentTaskBody(BaseModel):
    """任务提交体：一段自然语言的任务或问题。

    propose_only：影子期提案模式（写门批准后只登记改动日志交人工，不执行）；
    缺省跟随部署级 env DBAGENT_PROPOSE_ONLY。
    session_id：会话记忆（2026-10-05 四层结构，memory.py）——给定时任务走
    服务端记忆（最近 5 轮全量 + 滚动摘要 + 压缩块），context_summary 失效；
    会话之间记忆隔离。
    allow_cross：跨会话记忆开关（UI 控制，随提交同步到会话）——开启后
    recall_memory 工具可检索同 actor 的其他会话，null = 沿用会话既有设置；
    仅提示词上下文，不构成任何授权。
    context_summary：无会话时的兼容路径（评测/脚本），前端拼上一轮摘要。
    """

    question: str = Field(min_length=QUESTION_MIN_CHARS, max_length=QUESTION_MAX_CHARS)
    propose_only: bool = False
    context_summary: str = Field(default="", max_length=CONTEXT_SUMMARY_MAX_CHARS)
    session_id: str = Field(default="", max_length=64)
    allow_cross: bool | None = None


class DbAgentApprovalBody(BaseModel):
    """写提案审批体：approve 必须是显式布尔（防空体默认放行）。"""

    approve: bool


def _require_admin(current_user: dict) -> str:
    """管理员双重校验，返回操作者名（沿用 actions 端点口径）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return current_user.get("username", "")


def _owner_scope(current_user: dict) -> str | None:
    """任务读接口的归属过滤参数（业主 2026-10-08 拍板，与 /dbagent/changes 同口径）：
    最高管理员 → None 看全量；其他持 dbagent.view 的账号 → 本人名只看自己。"""
    if current_user.get("role") == "admin":
        return None
    return current_user.get("username", "")


@dbagent_router.post("/dbagent/tasks")
async def create_dbagent_task(body: DbAgentTaskBody,
                              current_user: dict = Depends(require_cap("dbagent.ask"))) -> dict:
    """提交一个 DB Agent 任务（异步跑，前端轮询本资源看进度）。"""
    actor = current_user.get("username", "")
    try:
        snapshot = await tasks.start_task(body.question.strip(), actor,
                                          propose_only=body.propose_only,
                                          context_summary=body.context_summary.strip(),
                                          session_id=body.session_id.strip(),
                                          allow_cross=body.allow_cross)
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    await audit(actor, ACT_DBAGENT_TASK,
                {"task_id": snapshot["id"], "question_length": len(body.question)})
    return snapshot


@dbagent_router.get("/dbagent/tasks")
async def list_dbagent_tasks(
        current_user: dict = Depends(require_cap("dbagent.view"))) -> list[dict]:
    """最近任务列表（快照，含状态与是否有待审批提案）。

    归属口径（业主 2026-10-08 拍板，与 /dbagent/changes 一致）：最高管理员看全部，
    其他持权人员只看自己发起的任务。"""
    return await tasks.list_tasks(actor=_owner_scope(current_user))


@dbagent_router.get("/dbagent/tasks/{task_id}")
async def get_db_agent_task(task_id: str,
                           current_user: dict = Depends(require_cap("dbagent.view"))) -> dict:
    """轮询任务：steps 时间线 / 报告 / 待审批提案（含 before 预览）。"""
    snapshot = await tasks.get_task(task_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    scope = _owner_scope(current_user)
    if scope is not None and snapshot.get("actor") != scope:
        # 他人任务与不存在同文案 404：归属过滤本身不能泄露"这个 id 有没有任务"
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return snapshot


@dbagent_router.post("/dbagent/tasks/{task_id}/approval")
async def decide_dbagent_write(task_id: str, body: DbAgentApprovalBody,
                               current_user: dict = Depends(require_cap("dbagent.approve"))) -> dict:
    """批准/否决一条挂起的门：写提案（授权即人已核对命令与将影响的行）
    或执行计划（plan mode，授权即人认可拆解后的步骤）。"""
    actor = current_user.get("username", "")
    # 决定前看一眼挂起类型，审计按门分事件（decide 后状态已回 running）
    # 此处刻意不套 _owner_scope：四眼原则要求另一账号能读到他人任务的提案全文
    # （"批准即人已核对命令与它将影响的行"），跨账号动作由 dbagent.approve 能力闸约束
    before = await tasks.get_task(task_id)
    is_plan = bool(before and before.get("status") == "awaiting_plan_approval")
    # 四眼原则（生产评审 B2）：子账号不能批准自己提交的提案/计划——
    # admin 豁免（业主单人模式）；否决不拦（失败方向无害）
    if (body.approve and before is not None
            and actor == before.get("actor")
            and current_user.get("role") != "admin"):
        raise HTTPException(
            status_code=403,
            detail="四眼原则：提交人不能批准自己的写提案/执行计划，"
                   "请由其他持审批能力的账号处理")
    # 重大变更（业主 2026-10-05 拍板：影响 ≥ 全表 15%）：仅最高管理员可批准
    if (body.approve and current_user.get("role") != "admin"
            and ((before or {}).get("proposal") or {}).get("critical")):
        raise HTTPException(
            status_code=403,
            detail="重大变更（影响行数超过全表 15%）仅最高管理员可批准")
    try:
        snapshot = await tasks.decide(task_id, body.approve, actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if is_plan:
        plan = snapshot.get("plan") or {}
        await audit(actor, ACT_DBAGENT_PLAN_APPROVAL, {
            "task_id": task_id,
            "decision": "approved" if body.approve else "rejected",
            "goal": str(plan.get("goal", ""))[:120],
            "step_count": len(plan.get("steps") or [])})
    else:
        proposal = snapshot.get("proposal") or {}
        await audit(actor, ACT_DBAGENT_WRITE_APPROVAL, {
            "task_id": task_id,
            "decision": "approved" if body.approve else "rejected",
            "sql_head": str(proposal.get("sql", ""))[:120],
            "param_count": len(proposal.get("params") or []),
            # 提交人（台账 457）：审计行的 user_id 是审批人，归属过滤要看提交人——
            # 此前这一格是空的，list_recent_writes 无法只给提交人看自己的记录
            "requested_by": str((before or {}).get("actor") or "")})
    return snapshot


def _parse_jsonb(value):
    """asyncpg 默认把 jsonb 返回为 str，解析回对象；坏值原样放过。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


@dbagent_router.get("/dbagent/changes")
async def list_dbagent_changes(
        limit: int = 50,
        current_user: dict = Depends(require_cap("dbagent.view"))) -> dict:
    """改动日志：变更账本最近条目。summary 是人话描述；
    before/after 只带前 5 行供展开核对，全量在库里。

    归属口径（业主 2026-10-05 拍板）：最高管理员看全部并可回滚任何变更；
    其他持权人员只看自己发起的修改日志。"""
    actor = current_user.get("username", "")
    lim = max(1, min(limit, 100))
    cols = ("SELECT change_id, task_id, question, action, target_table, sql_text, "
            "params, before_rows, after_rows, reverse_ops, affected, summary, "
            "status, group_id, approved_by, rolled_back_by, rollback_error, "
            "created_at, rolled_back_at FROM db_change_log ")
    if current_user.get("role") == "admin":
        rows = await pools.fetch_dicts(
            cols + "ORDER BY change_id DESC LIMIT $1", (lim,))
    else:
        rows = await pools.fetch_dicts(
            cols + "WHERE actor = $1 ORDER BY change_id DESC LIMIT $2",
            (actor, lim))
    items = []
    for row in rows:
        item = dict(row)
        for key in ("params", "before_rows", "after_rows", "reverse_ops"):
            item[key] = _parse_jsonb(item.get(key))
        if isinstance(item.get("before_rows"), list):
            item["before_rows"] = item["before_rows"][:5]
        if isinstance(item.get("after_rows"), list):
            item["after_rows"] = item["after_rows"][:5]
        items.append(item)
    return {"items": items}


@dbagent_router.post("/dbagent/changes/{change_id}/rollback")
async def rollback_dbagent_change(change_id: int,
                                  current_user: dict = Depends(require_cap("dbagent.rollback"))) -> dict:
    """回滚一笔已应用的变更（编号即 HG{change_id}）。

    逻辑统一在 services/dbagent/changes.py（与 Agent 的 rollback_change 工具
    同源）：先冲突检查——相关行在变更后又被改过则 409 拒绝并列明细，
    绝不覆盖新数据；通过后逐条执行反向操作（重过写守卫）。
    """
    actor = current_user.get("username", "")
    is_admin = current_user.get("role") == "admin"
    try:
        result = await dbagent_changes.execute_rollback(
            change_id, actor, owner_actor=actor, owner_is_admin=is_admin)
    except dbagent_changes.RollbackError as exc:
        detail = str(exc)
        failed = "失败" in detail
        # 审计统一在 changes.execute_rollback 内落（成功/失败两态）——
        # 此处不再重复记（2026-10-05 深审 A17）
        raise HTTPException(
            status_code=500 if failed else 409, detail=detail)
    return result


@dbagent_router.post("/dbagent/changes/{change_id}/discard")
async def discard_dbagent_change(change_id: int,
                                 current_user: dict = Depends(require_cap("dbagent.rollback"))) -> dict:
    """废弃一笔 proposed 残留（P3-4 残留清理通道）：人工明确不执行的提案
    标 discarded，退出 awaiting_manual 积压（退出验收要求积压清零——
    executed_manual 或 discarded 都算清）。能力闸与回滚同面（同为账本
    生命周期操作，业主能力矩阵不新增键）；逻辑在 changes.discard_proposed。"""
    actor = current_user.get("username", "")
    try:
        result = await dbagent_changes.discard_proposed(
            change_id, actor, owner_actor=actor,
            owner_is_admin=current_user.get("role") == "admin")
    except dbagent_changes.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await audit(actor, ACT_DBAGENT_CHANGE_ROLLBACK, {
        "change_id": change_id, "decision": "discarded",
        "group_id": result["group_id"]})
    return result


@dbagent_router.delete("/dbagent/sessions/{session_id}")
async def purge_dbagent_session(session_id: str,
                                current_user: dict = Depends(get_current_user)) -> dict:
    """管理员硬清除一个会话的服务端记忆（sessions/turns/blocks 全部）。

    业主 2026-10-05 拍板：用户在前端删除会话只删本地视图（不同步服务端），
    服务端归档按 30 天 TTL 自动过期；本端点是唯一立即清除路径，仅 admin。"""
    _require_admin(current_user)
    purged = await dbagent_memory.purge_session(session_id)
    await audit(current_user.get("username", ""), ACT_ADMIN, {
        "op": "dbagent_purge_session", "session_id": session_id,
        "purged": purged})
    return {"session_id": session_id, "purged": purged}


# ---- 子账号能力管理（仅 admin；业主定稿 2026-10-03 的权限约束面）----

class SubAccountBody(BaseModel):
    """创建子账号：初始能力集 + 密码（哈希入库，明文不落任何日志）。"""

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    permissions: list[str] = Field(default_factory=lambda: list(DEFAULT_SUB_CAPS))


class PermsUpdateBody(BaseModel):
    """整包更新一个子账号的能力允许清单。"""

    permissions: list[str] = Field(max_length=32)


class PasswordResetBody(BaseModel):
    """管理员重置子账号口令。"""

    password: str = Field(min_length=8, max_length=128)


class StatusBody(BaseModel):
    """启用/停用子账号。"""

    active: bool


def _valid_caps(perms: list[str]) -> list[str]:
    """未知能力直接 422（管理员别把拼错的 key 静默吞掉）。"""
    unknown = [p for p in perms if p not in CAPABILITIES]
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"未注册的能力: {', '.join(unknown)}")
    return sorted(set(perms))


@dbagent_router.get("/dbagent/perms")
async def list_sub_accounts(
        current_user: dict = Depends(get_current_user)) -> dict:
    """子账号列表与各自能力（admin 专享；admin 自身恒全能力，不列）。"""
    _require_admin(current_user)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT username, permissions, is_active, created_at "
            "FROM users WHERE role <> 'admin' ORDER BY created_at DESC LIMIT 200")
    return {"items": [
        {"username": r["username"], "permissions": list(r["permissions"] or []),
         "is_active": r["is_active"], "created_at": str(r["created_at"])}
        for r in rows], "capabilities": [
            {"key": k, "label": v[0], "desc": v[1]} for k, v in CAPABILITIES.items()]}


@dbagent_router.post("/dbagent/perms")
async def create_sub_account(body: SubAccountBody,
                             current_user: dict = Depends(get_current_user)) -> dict:
    """创建子账号（默认 user 角色 + 指定能力集）。重名 409。"""
    actor = _require_admin(current_user)
    perms = _valid_caps(body.permissions)
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE username = $1", body.username)
        if exists:
            raise HTTPException(status_code=409, detail="用户名已存在")
        await conn.execute(
            "INSERT INTO users (username, hashed_password, role, permissions, is_active) "
            "VALUES ($1, $2, 'user', $3, true)",
            body.username, hash_password(body.password), perms)
    from ..core.quota import invalidate_user_cache
    await invalidate_user_cache(body.username)
    await audit(actor, ACT_ADMIN, {
        "op": "dbagent_create_sub_account", "username": body.username,
        "permissions": perms})
    return {"username": body.username, "permissions": perms}


@dbagent_router.put("/dbagent/perms/{username}")
async def update_sub_account_perms(username: str, body: PermsUpdateBody,
                                   current_user: dict = Depends(get_current_user)) -> dict:
    """整包更新子账号能力（只能改子账号；admin 权限恒全，不可经此修改）。"""
    actor = _require_admin(current_user)
    perms = _valid_caps(body.permissions)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET permissions = $1, updated_at = now() "
            "WHERE username = $2 AND role <> 'admin' RETURNING permissions",
            perms, username)
    if not row:
        raise HTTPException(status_code=404, detail="子账号不存在（admin 不可修改）")
    # 立即失效用户缓存：get_current_user 走 Redis（TTL 5 分钟），不失效的话
    # 新能力要等缓存过期才生效（评测实测开通审批后仍 403）
    from ..core.quota import invalidate_user_cache
    await invalidate_user_cache(username)
    await audit(actor, ACT_ADMIN, {
        "op": "dbagent_update_perms", "username": username,
        "permissions": perms})
    return {"username": username, "permissions": list(row["permissions"] or [])}


async def _reset_sub_cache(username: str) -> None:
    """子账号变更后失效用户缓存（get_current_user 走 Redis TTL 5 分钟）。"""
    from ..core.quota import invalidate_user_cache
    await invalidate_user_cache(username)


@dbagent_router.put("/dbagent/perms/{username}/password")
async def reset_sub_password(username: str, body: PasswordResetBody,
                             current_user: dict = Depends(get_current_user)) -> dict:
    """管理员重置子账号口令。"""
    actor = _require_admin(current_user)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET hashed_password = $1, updated_at = now() "
            "WHERE username = $2 AND role <> 'admin' RETURNING username",
            hash_password(body.password), username)
    if not row:
        raise HTTPException(status_code=404, detail="子账号不存在（admin 不可修改）")
    await _reset_sub_cache(username)
    await audit(actor, ACT_ADMIN, {"op": "dbagent_reset_password", "username": username})
    return {"username": username}


@dbagent_router.put("/dbagent/perms/{username}/status")
async def set_sub_status(username: str, body: StatusBody,
                         current_user: dict = Depends(get_current_user)) -> dict:
    """启用/停用子账号（停用后登录与鉴权立即拒绝）。"""
    actor = _require_admin(current_user)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET is_active = $1, updated_at = now() "
            "WHERE username = $2 AND role <> 'admin' RETURNING is_active",
            body.active, username)
    if not row:
        raise HTTPException(status_code=404, detail="子账号不存在（admin 不可修改）")
    await _reset_sub_cache(username)
    await audit(actor, ACT_ADMIN, {
        "op": "dbagent_set_status", "username": username, "active": body.active})
    return {"username": username, "is_active": body.active}


@dbagent_router.delete("/dbagent/perms/{username}")
async def delete_sub_account(username: str,
                             current_user: dict = Depends(get_current_user)) -> dict:
    """删除子账号（不可恢复；admin 不可删）。"""
    actor = _require_admin(current_user)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM users WHERE username = $1 AND role <> 'admin' "
            "RETURNING username", username)
    if not row:
        raise HTTPException(status_code=404, detail="子账号不存在（admin 不可删）")
    await _reset_sub_cache(username)
    await audit(actor, ACT_ADMIN, {"op": "dbagent_delete_sub", "username": username})
    return {"username": username, "deleted": True}
