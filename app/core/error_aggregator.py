"""轻量级异常聚合（自建，替代 Sentry 免费额度）

- 捕获未处理异常，按类型聚合计数到 Redis + Prometheus
- 保留最近 N 条样本，供管理端查看（/api/admin/errors）
- 只记录、不阻断：写入失败不影响主流程
"""
from ..core.redis import get_redis, incr_arm_ttl
from ..core.metrics import app_exceptions_total
from ..core.logging import setup_logging
from ..core.safety_filter import sanitize_error_text

logger = setup_logging()

_RECENT_KEY = "errors:recent"
_RECENT_LIMIT = 100
_COUNT_PREFIX = "errors:count:"


async def record_error(request, exc: Exception) -> str:
    """记录一个未处理异常；返回错误类型标识（如 'HTTPException'）"""
    err_type = type(exc).__name__
    # Prometheus 指标（供告警/看板）
    app_exceptions_total.labels(type=err_type).inc()
    # Redis 聚合（计数 + 最近样本）
    try:
        r = await get_redis()
        key = f"{_COUNT_PREFIX}{err_type}"
        # 计数与 TTL 一次原子下发（2026-10-08 第四轮 D1）：原先 INCR 与 EXPIRE 分两条
        # 命令，中间被杀会留下无 TTL 的类型计数键，7 天窗口从此不再重开
        await incr_arm_ttl(r, key, 7 * 86400)  # 计数保留 7 天
        # 原文 str(exc) 可能带完整 URL（高德 key 在参数里），入 Redis 前统一脱敏
        # （与任务状态/tool_result 的出口同一sanitize；2026-09-07 审查 P2）
        detail = sanitize_error_text(str(exc), fallback="(详情已脱敏)")[:200]
        await r.lpush(
            _RECENT_KEY,
            f"{err_type}|{request.method} {request.url.path}|{detail}",
        )
        await r.ltrim(_RECENT_KEY, 0, _RECENT_LIMIT - 1)
        await r.expire(_RECENT_KEY, 7 * 86400)  # P3 修复：样本列表原先无 TTL
    except Exception as e:
        logger.warning(f"异常聚合写入失败（不影响主流程）: {e}")
    return err_type


async def get_error_summary() -> dict:
    """聚合概览：各类型计数 + 最近 20 条样本"""
    r = await get_redis()
    counts = {}
    try:
        keys = [k async for k in r.scan_iter(match=f"{_COUNT_PREFIX}*")]
        if keys:
            vals = await r.mget(keys)
            for k, v in zip(keys, vals):
                counts[str(k)[len(_COUNT_PREFIX):]] = int(v or 0)
    except Exception as e:
        logger.warning(f"异常聚合读取失败: {e}")
    recent = []
    try:
        recent = await r.lrange(_RECENT_KEY, 0, 19)
    except Exception as e:
        logger.warning(f"异常聚合样本读取失败: {e}")
    return {"counts": counts, "recent": recent}
