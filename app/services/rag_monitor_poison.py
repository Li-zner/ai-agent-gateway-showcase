"""RAG 监测毒行终态（2026-09-19 审查 03-rag F-P2-1）。

`_fetch_pending` 用 LEFT JOIN 反选待处理行：某行落库失败 → 行级 savepoint 回滚
→ 处理状态没写进去 → 下一轮（默认 300s）又取到同一行、又失败、又刷一条 warning。
原实现没有重试上限，确定性失败的行会把追账队列永久钉死，并每轮吃掉一批额度。

这里给失败一个上限：连续 N 次失败后写 `status='poison'` 终态标记，该行退出待处理
队列，队列恢复单调前进。`RULESET_VERSION` 递增时 `ruleset_version >= $2` 不再成立，
毒行会自动回到队列再跑一轮——规则升版正是重跑历史坏行的合理时机，不需要额外清理脚本。
"""
from __future__ import annotations

import os

from ..core.logging import setup_logging

logger = setup_logging()

# 默认 3 轮（≈15 分钟）后判终态：既够消化偶发抖动，也把刷屏上限锁死。
_POISON_AFTER_DEFAULT = 3

# (source, source_id) -> 连续失败次数
# ponytail: 计数在进程内存，worker 重启即清零，毒行最多多试 N 次才落终态；
#           要跨进程精确计数需给 rag_monitor_processing 加 retry_count 列（走迁移）。
_fail_counts: dict[tuple, int] = {}


def poison_after() -> int:
    """读取失败上限（每次读 env：毒行是低频路径，省一份 import 期常量陷阱）。"""
    try:
        return max(1, int(os.getenv("RAG_MONITOR_POISON_AFTER",
                                    str(_POISON_AFTER_DEFAULT))))
    except (TypeError, ValueError):
        return _POISON_AFTER_DEFAULT


def note_success(source: str, source_id: int) -> None:
    """行成功落库后清零计数：偶发抖动不能跨轮累积成终态。"""
    _fail_counts.pop((source, source_id), None)


async def note_failure(conn, source: str, source_id: int,
                       exc: Exception) -> None:
    """记一次行级落库失败；未达上限留待下轮重试，达到上限写 poison 终态。

    调用点在外层事务里、行 savepoint 之外：外层事务仍可用，poison 标记随本轮提交。
    """
    key = (source, source_id)
    limit = poison_after()
    attempts = _fail_counts.get(key, 0) + 1
    if attempts < limit:
        _fail_counts[key] = attempts
    logger.warning(f"RAG 监测 {source} 侧行 {source_id} 落库失败"
                   f"（第 {attempts}/{limit} 次）: {type(exc).__name__}: {exc}")
    if attempts < limit:
        return
    try:
        await _mark_poison(conn, source, source_id)
    except Exception as mark_exc:
        # 连终态标记都写不进（多半是连接已失效）：保持原行为，下轮再试。
        # 2026-09-28 批1 长尾 P2：计数此刻**不能**清——清了下一轮从 1 重爬，
        # 毒行反而无限重试；计数停在 limit-1，下轮再次到顶、再次尝试标记
        logger.warning(f"RAG 监测 poison 标记写入失败（下轮重试）: "
                       f"{type(mark_exc).__name__}: {mark_exc}")
        return
    # 标记已确定性落库，才清计数（原实现 pop 在标记之前，标记失败即归零重爬）
    _fail_counts.pop(key, None)
    logger.error(f"RAG 监测：行 {source}#{source_id} 连续 {attempts} 次落库失败，"
                 f"已标记 status='poison' 退出队列；规则集升版后会自动重试，"
                 f"请按上一条 warning 的异常定位该行。")


async def _mark_poison(conn, source: str, source_id: int) -> None:
    """写毒行终态标记：反选 JOIN 因此不再取该行。

    与 `_record_processed` 的区别是这里**不**加版本下限守卫——本轮就是要用当前
    规则版本压掉重试；下次版本递增时该行仍会回到队列。
    """
    from .rag_monitor import RULESET_VERSION  # 函数级导入：避免与 rag_monitor 循环依赖
    await conn.execute(
        "INSERT INTO rag_monitor_processing "
        "(source, source_id, ruleset_version, status, verdict_count, "
        "processed_at, updated_at) "
        "VALUES ($1,$2,$3,'poison',0,now(),now()) "
        "ON CONFLICT (source, source_id) DO UPDATE SET "
        "ruleset_version=EXCLUDED.ruleset_version, status='poison', "
        "verdict_count=0, processed_at=EXCLUDED.processed_at, "
        "updated_at=now()",
        source, source_id, RULESET_VERSION)
