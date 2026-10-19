"""
GitHub 试用额度 — 累计提问次数限制（绑定手机号后解除）

设计：
- GitHub 登录用户（phone 为空）置 quota_limited=TRUE，可免费提问 GITHUB_QUESTION_LIMIT 次（默认 20，全局共享，跨所有助手）。
- used_requests 用数据库侧原子自增（防并发丢更新）；累计带 WHERE quota_limited=TRUE，
  使调用方可无条件调用——非受限用户是 no-op，绑定手机解除限制后晚到的累计不污染账号。
"""
from ..core.config import GITHUB_QUESTION_LIMIT
from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.logging import setup_logging

logger = setup_logging()

# auth.get_cached_user 用户信息缓存键前缀（单源定义于此，middleware/routes 统一引用）
USER_INFO_CACHE_PREFIX = "user:info:"


class QuotaDependencyError(Exception):
    """试用额度依赖（数据库）故障——与"额度耗尽"必须区分（2026-09-14 审计 P1）。

    原实现 DB 异常与额度耗尽共用 False，调用方一律映射 402"免费额度已用完"，
    数据库故障被误报成业务额度问题。调用方捕获本异常应返回 503（fail-closed
    语义不变：仍然拒绝请求，只是状态码如实反映"服务故障"而非"额度用尽"）。
    """


async def invalidate_user_cache(username: str) -> None:
    """失效 auth.get_cached_user 的用户信息缓存（TTL 5 分钟）

    used_requests / quota_limited 变更后必须调用：否则额度判断在缓存窗口内
    读到旧计数，GitHub 试用 20 次的上限在窗口内形同虚设（P1 修复）。
    """
    try:
        r = await get_redis()
        await r.delete(f"{USER_INFO_CACHE_PREFIX}{username}")
    except Exception as e:
        logger.debug(f"用户缓存失效失败（TTL 自愈）: {e}")


def remaining_questions(user: dict) -> int:
    """受限用户剩余可提问次数；非受限用户返回 -1（表示无限制，前端已约定，C6 保留该语义）"""
    if user.get("role") == "admin" or not user.get("quota_limited"):
        return -1
    return max(0, GITHUB_QUESTION_LIMIT - (user.get("used_requests") or 0))


# inc_used_questions 已删（2026-09-29 app 扫描）：reserve_used_questions 准入预留
# 模式落地后全仓零调用方，完成后累计的旧口径成死码——删除优于保留（ponytail）。


async def reserve_used_questions(user: dict) -> bool:
    """在调用 LLM 前原子预留一次试用额度。

    与旧的“完成后 inc”不同，数据库条件更新在准入时执行，剩余 1 次额度时
    并发请求只能有一个成功，断连也不会漏计。admin/非受限用户直接放行。
    返回：True=已预留；False=额度耗尽（402）；DB 异常抛 QuotaDependencyError
    （调用方映射 503，2026-09-14 审计 P1 三态化）。
    """
    if user.get("role") == "admin" or not user.get("quota_limited"):
        return True
    username = user["username"]
    try:
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            row = await conn.fetchval(
                "UPDATE users SET used_requests = used_requests + 1, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE username = $1 AND quota_limited = TRUE AND role != 'admin' "
                "AND used_requests < $2 RETURNING used_requests",
                username, GITHUB_QUESTION_LIMIT,
            )
        if row is not None:
            await invalidate_user_cache(username)
            return True
        return False
    except QuotaDependencyError:
        raise
    except Exception as e:
        logger.warning(f"试用额度预留失败（DB 故障，与额度耗尽区分）: {e}")
        raise QuotaDependencyError(f"quota dependency unavailable: {type(e).__name__}") from e


async def rollback_used_questions(username: str) -> None:
    """归还一次已预留的试用额度（2026-09-12：槽位满 429 等未产出场景回滚）。

    条件与 reserve_used_questions 对称；失败仅告警（下次预留以 DB 当前值为准，
    不会放大额度）。
    """
    try:
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            await conn.execute(
                "UPDATE users SET used_requests = GREATEST(used_requests - 1, 0), "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE username = $1 AND quota_limited = TRUE AND role != 'admin' "
                "AND used_requests > 0",
                username,
            )
        await invalidate_user_cache(username)
    except Exception as e:
        logger.warning(f"试用额度回滚失败: {e}")
