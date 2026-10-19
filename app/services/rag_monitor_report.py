"""RAG 监测的「状态瘦身 + 每日巡检报告」——从 rag_monitor.py 拆出（600 行门禁，2026-10-04）。

rag_monitor 通过 `from .rag_monitor_report import ...` 再导出这些名字，调用方
（中控台日报端点、运维问答用的 _slim_status、日报回归测试）导入路径保持不变。
本模块只在函数内懒加载 rag_admin / chat_support / config，与 rag_monitor 单向依赖、无环。
"""
from __future__ import annotations

from ..core.logging import setup_logging

logger = setup_logging()


def _slim_status(status: dict) -> dict:
    """给 LLM 的监测摘要结构化瘦身：字符硬截会把 JSON 截成非法片段。
    明细限 10 条、evidence 截前 3 条、丢 patch 全文（LLM 只需要判断）。
    同时剔除 daily_report（生成时快照）——与实时数据并列会让 LLM 混淆口径。"""
    slim = dict(status)
    slim.pop("daily_report", None)
    slim["recent_verdicts"] = [
        {k: v for k, v in r.items() if k != "patch"}
        | {"evidence": (r.get("evidence") or [])[:3]}
        for r in (status.get("recent_verdicts") or [])[:10]
    ]
    return slim


_DAILY_REPORT_SYSTEM = (
    "你是 RAG 检索系统的每日巡检员。基于监测数据写一份简短晨报（300 字内）："
    "①昨日检索健康状况（量级/空召回/延迟）②诊断结论要点（按严重度）"
    "③是否需要人工介入与具体动作。数据没有的信息不要编造。"
)


async def ensure_daily_report() -> int | None:
    """当天尚无报告则生成（LLM 读实时监测摘要），成功返回 1；已有返回 None。

    报告失败不落行 → 下轮循环自然重试；report_date 唯一保证一天一份。
    报告日期按 PG 时区（UTC）：首个 cycle 在北京时间上午 8 点触发，即「晨报」。
    多实例部署（4 个 gateway 容器同跑本循环）时用 PG 事务级 advisory lock 抢占：
    事务提交/回滚自动释放（会话级锁在连接归还池时不清理——实例关停若恰逢
    unlock 前，锁随连接残留，此后报告永久停摆）。未抢到立即让位。
    """
    from ..core.db import get_pool
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        async with conn.transaction():
            got_lock = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext('rag_daily_report'))")
            if not got_lock:
                return None  # 其他实例正在生成，本轮让位
            return await _ensure_daily_report_locked(conn)


async def _ensure_daily_report_locked(conn) -> int | None:
    existing = await conn.fetchval(
        "SELECT 1 FROM rag_daily_reports WHERE report_date = CURRENT_DATE")
    if existing:
        return None
    from ..routes.rag_admin import _fetch_status
    status = _slim_status(await _fetch_status())
    import json as _json
    context = _json.dumps(status, ensure_ascii=False)
    from ..services.chat_support import post_chat_completion
    from ..core.config import HTTP_TIMEOUT_MEDIUM
    ok, data = await post_chat_completion(
        {"model": None,
         "messages": [{"role": "system", "content": _DAILY_REPORT_SYSTEM},
                      {"role": "user", "content": f"监测数据：\n{context}"}],
         "temperature": 0.2, "max_tokens": 2000},
        timeout=HTTP_TIMEOUT_MEDIUM)
    if not ok:
        raise RuntimeError(f"巡检 LLM 不可用: {str(data)[:120]}")
    report = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not report.strip():
        raise RuntimeError("巡检 LLM 返回空内容")
    await conn.execute(
        "INSERT INTO rag_daily_reports (report_date, report, stats) "
        "VALUES (CURRENT_DATE, $1, $2::jsonb) "
        "ON CONFLICT (report_date) DO UPDATE SET report = EXCLUDED.report, "
        "stats = EXCLUDED.stats, created_at = now()",
        report, _json.dumps(status, ensure_ascii=False))
    logger.info(f"RAG 每日巡检报告已生成（{len(report)} 字）")
    return 1
