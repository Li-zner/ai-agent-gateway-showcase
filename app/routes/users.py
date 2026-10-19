"""用户信息 / 消息评分 / 人格切换 / 统计

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
含 GitHub 试用额度展示、回复评分防重（唯一索引兜底）、人格切换、请求统计（聚合下推 PG）。
"""
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException

from ..core.logging import setup_logging
from ..core.db import get_pool
from ..middleware.auth import get_current_user

logger = setup_logging()

router = APIRouter()


class RateMessageRequest(BaseModel):
    """消息评分请求（文本字段限长：入库字段，防超长串滥用存储）"""
    rating: int = Field(..., ge=1, le=5)
    session_id: str = Field(default="", max_length=128)
    user_message: str = Field(default="", max_length=4096)
    assistant_message: str = Field(default="", max_length=4096)


class SwitchPersonaRequest(BaseModel):
    """切换人格请求"""
    persona_id: str = Field(..., min_length=1)


@router.get("/api/user/profile")
async def get_user_profile(current_user: dict = Depends(get_current_user)):
    """获取当前用户详细信息"""
    username = current_user["username"]
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "SELECT username, phone, role, display_name, email, avatar_url, extra, "
            "quota_limited, used_requests, created_at, permissions "
            "FROM users WHERE username=$1", username
        )
    if not row:
        raise HTTPException(404, "用户不存在")
    # GitHub 试用额度（前端展示剩余提问次数）
    from ..core.quota import remaining_questions
    q_limited = bool(row["quota_limited"]) if row["quota_limited"] is not None else False
    return {
        "username": row["username"],
        "phone": row["phone"] or "",
        "role": row["role"] or "user",
        "permissions": list(row["permissions"] or []),
            "display_name": row["display_name"] or "",
            "email": row["email"] or "",
            "avatar_url": row["avatar_url"] or "",
            "extra": row["extra"] if isinstance(row["extra"], dict) else {},
            "quota_limited": q_limited,
            "used_requests": row["used_requests"] or 0,
            "remaining_questions": remaining_questions({
                "role": row["role"] or "user",
                "quota_limited": q_limited,
                "used_requests": row["used_requests"] or 0,
            }),
            "created_at": row["created_at"].isoformat() if row["created_at"] else ""
        }


@router.post("/api/message/rate")
async def rate_message(payload: RateMessageRequest, current_user: dict = Depends(get_current_user)):
    """对 AI 回复评分（1-5 星），存储到 message_ratings 表。同一回复不可重复评分（A24）。"""
    rating = payload.rating
    session_id = payload.session_id
    user_message = payload.user_message
    assistant_message = payload.assistant_message
    username = current_user["username"]
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        # 同一回复（同用户+会话+问题+回答）已评过分 → 拒绝重复评分
        # Bug #10 修复：预查只是快路径（友好提示）；真正防重靠唯一索引
        # uq_message_ratings_dedup 兜底，并发后到者命中唯一冲突，捕获后返回已有评分。
        # 用 IS NOT DISTINCT FROM 与索引的 COALESCE(NULL→'') 语义保持一致。
        existing = await conn.fetchval(
            "SELECT rating FROM message_ratings "
            "WHERE user_id=$1 AND session_id IS NOT DISTINCT FROM $2 "
            "AND user_message IS NOT DISTINCT FROM $3 AND assistant_message IS NOT DISTINCT FROM $4 "
            "ORDER BY created_at DESC LIMIT 1",
            username, session_id, user_message, assistant_message
        )
        if existing is not None:
            logger.info(f"重复评分拦截: username={username}, 已评={existing}")
            return {"message": "已评分，感谢你的评价！", "rating": existing, "already_rated": True}
        from asyncpg.exceptions import UniqueViolationError
        try:
            await conn.execute(
                "INSERT INTO message_ratings (user_id, session_id, user_message, assistant_message, rating) "
                "VALUES ($1, $2, $3, $4, $5)",
                username, session_id, user_message, assistant_message, rating
            )
        except UniqueViolationError:
            # 并发同内容评分后到者：索引拦下重复插入，返回已有评分
            existing = await conn.fetchval(
                "SELECT rating FROM message_ratings "
                "WHERE user_id=$1 AND session_id IS NOT DISTINCT FROM $2 "
                "AND user_message IS NOT DISTINCT FROM $3 AND assistant_message IS NOT DISTINCT FROM $4 "
                "ORDER BY created_at DESC LIMIT 1",
                username, session_id, user_message, assistant_message
            )
            logger.info(f"并发重复评分拦截: username={username}, 已评={existing}")
            return {"message": "已评分，感谢你的评价！", "rating": existing, "already_rated": True}
    logger.info(f"消息评分: username={username}, rating={rating}")
    return {"message": "感谢你的评价！", "rating": rating}


@router.get("/api/personas")
async def list_personas(current_user: dict = Depends(get_current_user)):
    """列出所有人格（2026-09-07 审查 P2：原先匿名可调，暴露人格列表与配置）"""
    from ..core.persona_manager import get_persona_manager
    pm = get_persona_manager()
    return {
        "personas": pm.list_personas(),
        "current": pm.current_id,
    }


@router.post("/api/persona/switch")
async def switch_persona(payload: SwitchPersonaRequest, current_user: dict = Depends(get_current_user)):
    """切换全局默认人格（A24）。

    收敛 admin（2026-09-07 审查 P2）：switch 改的是全局默认人格，影响所有未显式
    指定 persona 的请求，任何登录用户可改=全局 DoS。前端为本地切换（不调此端点），
    收敛无破坏。
    """
    if current_user.get("role") != "admin":
        raise HTTPException(403, "仅管理员可切换全局默认人格")
    persona_id = payload.persona_id
    from ..core.persona_manager import get_persona_manager
    pm = get_persona_manager()
    if pm.switch(persona_id):
        return {"message": f"已切换到 {pm.current.name}", "current": pm.current_id}
    raise HTTPException(404, f"人格不存在: {persona_id}")


@router.get("/api/stats")
async def get_stats(current_user: dict = Depends(get_current_user)):
    # 收敛 admin（2026-09-10 审查 P2）：全站统计（总请求量/成功率/模型分布/最近样本）
    # 属运营信息面，多租户下不对普通用户开放（与 admin_errors 同口径）
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        # 聚合下推 PG（P2 #5：避免 Python 层拉全量算平均，浪费内存与 CPU）
        agg = await conn.fetchrow(
            "SELECT COUNT(*) as total, "
            "AVG(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_rate, "
            "AVG(CASE WHEN first_token_time IS NOT NULL AND end_time IS NOT NULL "
            "         THEN end_time - start_time END) as avg_latency "
            "FROM requests"
        )
        fallback_usage = await conn.fetchval(
            "SELECT COUNT(*) FROM requests WHERE model_used = 'ollama'"
        )
        sample_rows = await conn.fetch(
            "SELECT id, model_used, status, start_time, first_token_time "
            "FROM requests ORDER BY start_time DESC LIMIT 5"
        )
    total = agg["total"] or 0
    if not total:
        return {"total": 0}
    # samples 脱敏：只返回安全字段，剔除 error（可能含内部堆栈/敏感 SQL，P0 #3）
    samples = [
        {"id": r["id"], "model_used": r["model_used"], "status": r["status"],
         "start_time": r["start_time"], "first_token_time": r["first_token_time"]}
        for r in sample_rows
    ]
    return {
        "total_requests": total,
        "success_rate": f"{(agg['success_rate'] or 0) * 100:.1f}%",
        "avg_latency_seconds": round(float(agg["avg_latency"] or 0), 3),
        "fallback_usage": fallback_usage or 0,
        "samples": samples,
    }
