"""答案侧追踪：把「输入→检索→输出」链的输出侧事实落库。

职责切分与 retrieval_trace.py 同哲学（2026-09-13 监测 MVP）：
- 只记录、不阻断；写库走 spawn() 后台任务
- 只存客观事实（回答文本、程序解析的引用编号、检索返回量快照），不存判断——
  引用幻觉/无依据作答等结论由 ragclosure 规则引擎消费本表得出
- 检索侧事实经检索事实容器带入（见 _retrieval_store）：容器挂在请求 trace
  对象上按引用跨 asyncio 任务共享，ReAct 子任务里的检索快照父任务可见；
  语义缓存命中等不走检索的路径容器为空，照记但检索字段为 NULL，
  诊断时跳过检索相关规则
"""
from __future__ import annotations

import contextvars
import json
import os
import re

from ..core.logging import setup_logging
from .rag_request_trace import get_request_uid, record_span

logger = setup_logging()

_ENABLED = os.getenv("ANSWER_TRACE_ENABLED", "1") == "1"

# 与 ragclosure/rules.py 的 _CITATION_RE 同语义：答案中的 [n] 引用编号
_CITATION_RE = re.compile(r"\[(\d{1,3})\]")

_LAST_RETRIEVAL: contextvars.ContextVar = contextvars.ContextVar(
    "kb_last_retrieval", default=None)


def _retrieval_store() -> dict:
    """检索事实容器：有请求上下文走跨任务共享 dict，否则回退 contextvar。

    contextvar 回退只为无请求上下文的调用方（单测、脚本）保留同任务内
    的存续语义；线上请求一律有 begin_request 建立的共享容器。
    """
    from .rag_request_trace import request_retrieval_state
    shared = request_retrieval_state()
    if shared is not None:
        return shared
    cur = _LAST_RETRIEVAL.get()
    if cur is None:
        cur = {}
        _LAST_RETRIEVAL.set(cur)
    return cur


def last_retrieval_snapshot() -> dict | None:
    """当前检索事实快照的浅拷贝；空返回 None（诊断工具与测试用）。"""
    store = _retrieval_store()
    return dict(store) if store else None


def reset_last_retrieval() -> None:
    """清空检索事实快照（无请求上下文时的测试/工具重置入口）。"""
    from .rag_request_trace import request_retrieval_state
    shared = request_retrieval_state()
    if shared is not None:
        shared.clear()
    else:
        _LAST_RETRIEVAL.set(None)
# 内容级幻觉引用检测（chat_fast_paths.find_ungrounded_citations）的结果通道：
# 该检测需要全部检索依据文本，只能在回答生成处算——经此带给 answer trace 落表，
# 监测引擎即可在线统计幻觉引用率（此前结果只进 logger.warning，无统计落点）
_UNGROUNDED: contextvars.ContextVar = contextvars.ContextVar(
    "kb_ungrounded_citations", default=None)


def set_ungrounded_citations(citations: list) -> None:
    """回答生成处调用：记录内容级幻觉引用检测结果（本次快照，下次覆盖）。"""
    values = list(citations or [])[:20]
    _UNGROUNDED.set(values)
    record_span(
        "grounding",
        status="degraded" if values else "ok",
        result_count=len(values),
        attributes={"ungrounded_citations": values},
    )


def set_last_retrieval(facts: dict) -> None:
    """检索出口调用（retrieval_trace.record 内）：快照本次检索的客观事实。

    同一回答内 LLM 可能多次调用检索（多轮 tool call）：已有内容时**合并**而非
    覆盖——keys 累计去重、returned_count 累加、latency 累加，method 保留最后一次。
    否则 answer trace 只关联最后一轮检索，前面的检索依据全部丢失。
    keys 上限 50 条防异常多轮膨胀。
    容器按引用跨 asyncio 任务共享（见 _retrieval_store）：ReAct 子任务里写入的
    快照，父任务 finalize 的 record_answer 读得到。
    """
    store = _retrieval_store()
    if not store:
        retrieval_uids = [_uid_text(facts.get("retrieval_uid"))] \
            if facts.get("retrieval_uid") else []
        store.update({
            "query": facts.get("query", ""),
            "persona": facts.get("persona", "civil_code"),
            "returned_count": int(facts.get("returned_count") or 0),
            "keys": list(facts.get("keys") or [])[:50],
            "method": facts.get("method", ""),
            "latency_ms": facts.get("latency_ms") or 0,
            "rounds": 1,
            "retrieval_uid": facts.get("retrieval_uid"),
            "retrieval_uids": retrieval_uids,
        })
        return
    merged_keys = list(dict.fromkeys((store.get("keys") or [])
                                     + list(facts.get("keys") or [])))[:50]
    old_uids = [_uid_text(x) for x in (store.get("retrieval_uids") or [])]
    if store.get("retrieval_uid"):
        old_uids.append(_uid_text(store["retrieval_uid"]))
    new_uid = facts.get("retrieval_uid")
    if new_uid:
        old_uids.append(_uid_text(new_uid))
    store.update({
        "keys": merged_keys,
        "returned_count": int(store.get("returned_count") or 0)
                          + int(facts.get("returned_count") or 0),
        "method": facts.get("method", store.get("method", "")),
        "latency_ms": (store.get("latency_ms") or 0) + (facts.get("latency_ms") or 0),
        "rounds": int(store.get("rounds") or 1) + 1,
        "retrieval_uids": [x for x in dict.fromkeys(old_uids) if x],
    })
    if not store.get("retrieval_uid") and facts.get("retrieval_uid"):
        # 首轮无 uid 时补记：其 query 与 answer 行同源，是 join 基准
        store["retrieval_uid"] = facts.get("retrieval_uid")


def parse_cited_numbers(answer: str) -> list[int]:
    """解析答案中的引用编号 [n]，去重升序。纯函数，供诊断规则消费。"""
    return sorted({int(m) for m in _CITATION_RE.findall(answer or "")})


def _uid_text(value) -> str:
    """UUID 列表进入 JSONB 前统一转字符串，避免 json.dumps 失败。"""
    return str(value) if value else ""


def record_answer(query: str, answer: str, username: str = "",
                  persona: str = "civil_code") -> None:
    """答案完成时调用（finalize_answer）：落库答案侧事实。失败一律静默。"""
    if not _ENABLED:
        return
    request_uid = get_request_uid()
    if not request_uid:
        try:
            from ..core.metrics import rag_request_trace_missing_total
            rag_request_trace_missing_total.labels(kind="answer").inc()
        except Exception:  # noqa: silent-except — 指标失败不影响回答
            pass
    try:
        from ..core.concurrency import spawn
        spawn(_persist(query, answer, username, persona, request_uid),
              name="answer-trace")
    except Exception as e:
        logger.warning(f"答案 trace 任务创建失败（不影响主流程）: {type(e).__name__}: {e}")


async def _persist(query: str, answer: str, username: str, persona: str,
                   request_uid: str = "") -> None:
    try:
        from ..core.db import get_pool
        from .rag_request_trace import _username_hash

        facts = last_retrieval_snapshot() or {}
        text = (answer or "")[:2000]
        cited = parse_cited_numbers(text)
        ungrounded = _UNGROUNDED.get()
        pool = await get_pool()
        # 2026-09-30 校准修复（B1）：5s 硬编码 → DB_ACQUIRE_TIMEOUT（原先该
        # env 对 finalize 热路径不生效；压测实测 max 等待 389ms，差异无实际影响）
        from ..core.config import DB_ACQUIRE_TIMEOUT
        async with pool.acquire(timeout=DB_ACQUIRE_TIMEOUT) as conn:
            await conn.execute(
                "INSERT INTO answer_traces (persona, username, query, answer, "
                "answer_len, cited_numbers, retrieval_returned_count, "
                "retrieval_keys, retrieval_method, retrieval_latency_ms, "
                "retrieval_uid, ungrounded_citations, request_uid, retrieval_uids) "
                "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8::jsonb,$9,$10,$11,$12::jsonb,"
                "$13,$14::jsonb)",
                persona, _username_hash(username) or None, query[:1000], text,
                len(answer or ""),
                json.dumps(cited),
                facts.get("returned_count"),
                json.dumps(facts.get("keys") or []),
                facts.get("method") or None,
                facts.get("latency_ms"),
                facts.get("retrieval_uid"),
                json.dumps(ungrounded) if ungrounded is not None else None,
                request_uid or None,
                json.dumps(facts.get("retrieval_uids") or []),
            )
    except Exception as e:
        logger.warning(f"答案 trace 落库失败（不影响主流程）: {type(e).__name__}: {e}")
