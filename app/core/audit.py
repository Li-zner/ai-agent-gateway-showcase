"""
审计日志 — 记录关键操作（登录/注册/检索/支付/admin），供追溯与合规
"""
import json

from ..core.db import get_pool
from ..core.logging import setup_logging
from ..core.concurrency import spawn

logger = setup_logging()

# 审计动作常量
ACT_LOGIN = "login"
ACT_REGISTER = "register"
ACT_SEARCH = "kb_search"
ACT_CHAT = "chat"
ACT_PAYMENT = "payment"
ACT_ADMIN = "admin_op"
ACT_PHONE_BIND = "phone_bind"
ACT_RAG_ASK = "rag_ask"
ACT_RAG_DATA_ASK = "rag_data_ask"
ACT_RAG_ACTION_PROPOSE = "rag_action_propose"
ACT_RAG_CONTENT_VIEW = "rag_content_view"
ACT_DBAGENT_TASK = "dbagent_task"
ACT_DBAGENT_WRITE_APPROVAL = "dbagent_write_approval"
ACT_DBAGENT_PLAN_APPROVAL = "dbagent_plan_approval"
ACT_DBAGENT_CHANGE_ROLLBACK = "dbagent_change_rollback"


async def audit(user_id: str, action: str, detail: dict | None = None) -> None:
    """写入审计日志（异步，失败不影响主流程；detail 自动脱敏敏感键）"""
    # 脱敏：审计中不落明文敏感字段；phone 按 3+4 掩码保留排障可辨识度。
    # 2026-09-12 清欠 P2（09-11 审查）：精确匹配改子串匹配（refresh_token/
    # access_key/admin_password 等变体键此前漏脱敏），并递归覆盖嵌套 detail。
    sensitive_markers = ("password", "token", "code", "secret", "api_key", "credential")

    def _scrub(value, key=""):
        if isinstance(value, dict):
            return {k: _scrub(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            # 列表中的对象也必须递归脱敏，否则 detail.items[].password
            # 会绕过字典级检查直接进入审计表。
            return [_scrub(item, key) for item in value]
        key_l = str(key).lower()
        if any(m in key_l for m in sensitive_markers):
            return "***"
        if key_l == "phone":
            if isinstance(value, str) and len(value) >= 7:
                return value[:3] + "****" + value[-4:]
            return "***"
        return value

    detail = _scrub(dict(detail or {}))

    async def _write():
        try:
            pool = await get_pool()
            async with pool.acquire(timeout=5) as conn:
                await conn.execute(
                    "INSERT INTO audit_logs (user_id, action, detail) VALUES ($1, $2, $3)",
                    user_id, action, json.dumps(detail, ensure_ascii=False),
                )
        except Exception as e:
            logger.warning(f"审计写入失败（不影响主流程）: {e}")

    # spawn 持强引用：裸 create_task 的后台任务可被 GC 中途回收（2026-09-07 审查 P2）
    spawn(_write(), name=f"audit:{action}")


async def query_audit(user_id: str = "", action: str = "", limit: int = 100) -> list:
    """查询审计日志（admin 用）"""
    pool = await get_pool()
    clauses, args = [], []
    if user_id:
        clauses.append("user_id = $1")
        args.append(user_id)
    if action:
        clauses.append(f"action = ${len(args) + 1}")
        args.append(action)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    async with pool.acquire(timeout=5) as conn:
        rows = await conn.fetch(
            f"SELECT id, user_id, action, detail, created_at FROM audit_logs "
            f"{where} ORDER BY id DESC LIMIT ${len(args)}",
            *args,
        )
        return [
            {"id": r["id"], "user_id": r["user_id"], "action": r["action"],
             "detail": r["detail"], "created_at": str(r["created_at"])}
            for r in rows
        ]
