"""retrieval_v2 —— 民法典检索主干的第二份实现（2026-10-13 业主点单：重构另起一份，原代码不碰）

为什么要第二份：现网这条链的行为分散在 tools.py / kb_rerank.py / kb_expand.py /
kb_ce_band.py / kb_embedding.py 五个模块里，靠 4 处 `# noqa: F401` re-export 维持旧
import 路径；跨阶段状态走 contextvar（_LAST_EMBEDDING / _LAST_LEGS），改写重试轮的
contextvar 覆盖曾直接造成 RAG-3 监测误判（tools.py:399-410）。本模块把主干收成
「一份配置 + 每腿一个函数 + 一条显式数据流的管线」，签名与返回结构对齐
tools.search_knowledge，可与现网并存、可 A/B。

刻意保留的复用（不是重写一切）：RRF、条号直钉、重排候选挑选、CE 打分与融合、
查询改写、权限 SQL 片段、embedding 客户端全部直接调用既有实现，本模块只重组排序
与数据流。权限片段走 tools._perm_clause 单一来源，防的正是 861 事故那类"同一把尺
抄两份、各自漂"。

首版不复刻的三条腿：邻接扩召、章内余弦扩召、高分 CE 保护带——三者在现网**默认
关闭**（KB_NEIGHBOR_ENABLED / KB_CHAPTER_EXPAND / CE_BAND_ENABLED 缺省 0），线上
行为等价；要接进 v2 需先各自复验再补，见 tests/run_retrieval_ab.py 的对拍口径。
纯逻辑层（融合、候选挑选、blend、出口记账）的等价已由本机差分对拍钉住
（tests/unit/test_20261013_retrieval_v2.py 的 ScriptedConn 段：同一份假 DB 喂两条
链，含容器 190 条打不到的改写重试路径）；容器那份 A/B 从此只剩数据层要验
（真实 SQL、真实切分与真实分数分布）。

**观测面（2026-10-14 接装；本段原先写的是「v2 一样都没有、只能当 A/B 件」）**：
现网链上的 `record_span` 四段埋点（recall / recall_retry / fusion / rerank）、
`record_retrieval` 落 `retrieval_traces`（向量腿故障标志是 RC-1b 单腿故障判定的
输入）、以及 `diagnostics_enabled()` 的漏斗快照，v2 全部走**同一份既有实现**
（rag_request_trace.record_span、retrieval_trace.record），只把数据源从 contextvar
换成显式传参的 `Legs`：`Legs.trace_tuple()` 复刻现网「重试轮覆盖首轮」的记账口径，
监控侧读到的数字在两版之间才可比。接装后与主链路的剩余差量只剩上面那三条默认
关闭的扩召/保护带——真要顶掉主链路，要么接上它们，要么确认线上恒关。

一处行为差异（有意为之，且可关）：词面第三条腿。现网 _keyword_fill 经代码自述
**恒不产出结果**（tools.py:241-258 写明两处原因：配额被 trgm 饿死 + 拿整句去
ILIKE）。v2 把它改成独立一腿，并把"中文片段切分"补上（无词典滑窗近似，见
_cnf_fragments 的 ponytail 注）；`Cfg.keyword_leg=False` 即回到现网语义，A/B 能
把这腿的净收益单独量出来。

用法（conn 可注入，便于离线自测；传 None 则与现网同样从池里取）：
    from app.agents.retrieval_v2 import search_v2, Cfg
    res = await search_v2(None, "离婚时孩子抚养权怎么判", top_k=5, permissions=[])
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..core.config import EMBEDDING_MODEL
from ..core.logging import setup_logging
from .retrieval_diagnostics import chunk_keys, diagnostics_enabled
from .kb_rerank import (REWRITE_TRIGGER_TRGM_SIM, REWRITE_TRIGGER_VEC_SIM,
                        _blend_rrf_ce, _rerank_local, _rewrite_query_for_recall,
                        _select_rerank_candidates)

if TYPE_CHECKING:
    # 只给类型标注用：本模块运行期不 import asyncpg，本机没有 PG 也能导入做纯函数单测。
    import asyncpg

logger = setup_logging()


@dataclass(frozen=True)
class Cfg:
    """检索的全部旋钮，一次读取、显式传参；不再有"模块常量在导入时定死"的隐式全局。

    缺省值与现网同源（tools.py:289-310 的四个 KB_* 环境变量 + kb_rerank 的两个改写
    阈值），env 键名也保持一致，这样中控台与 .env 的既有配置对两份实现都有效。
    """

    recall_limit: int = 15
    rerank_top: int = 15
    rrf_k: int = 60
    w_trgm: float = 0.4
    w_vector: float = 1.0
    w_keyword: float = 0.4
    blend: float = 0.6
    content_cap: int = 500
    kw_width: int = 4
    kw_cap: int = 6
    rewrite_trgm_sim: float = REWRITE_TRIGGER_TRGM_SIM
    rewrite_vec_sim: float = REWRITE_TRIGGER_VEC_SIM
    keyword_leg: bool = True
    ivfflat_probes: int = 1000

    @classmethod
    def from_env(cls) -> "Cfg":
        """按现网同名 env 键装配；未设置的键取上面的缺省值。"""
        g = os.getenv
        return cls(
            recall_limit=int(g("KB_RECALL_LIMIT", cls.recall_limit)),
            rerank_top=int(g("KB_RERANK_TOP", cls.rerank_top)),
            w_trgm=float(g("KB_RRF_TRGM_WEIGHT", cls.w_trgm)),
            w_keyword=float(g("KB_RRF_KEYWORD_WEIGHT", cls.w_keyword)),
            blend=float(g("KB_RERANK_BLEND", cls.blend)),
            keyword_leg=g("KB_KEYWORD_LEG", "1") == "1",
        )


@dataclass
class Legs:
    """一次召回的产出物：各腿结果 + 各腿最高分 + 向量腿故障标志。

    现网用 contextvar 传这些信息（_LAST_LEGS / _LAST_EMBEDDING），重试轮会覆盖；
    v2 走返回值，管线里谁用谁接，故障标志也是数据而不是"隐式约定"。
    重试轮挂在 `retry` 上而不是并进各腿列表——`_rrf_merge` 按列表内位次计分，
    首尾相接会让重试轮位次被首轮长度顶后、权重凭空变小。
    """

    trgm: list
    vector: list
    keyword: list
    max_trgm: float
    max_vec: float
    vec_failed: bool
    embedding: list | None
    retry: "Legs | None" = None

    def ranked(self, cfg: Cfg) -> tuple[list, tuple]:
        """返回参与 RRF 的（列表们, 权重们）。

        口径与现网一致：每轮每腿各算一条独立列表，重试轮并列追加（tools.py:417-418
        的 `weights=(RRF_TRGM_WEIGHT, 1.0, RRF_TRGM_WEIGHT, 1.0)`）；词面腿关掉时
        首轮只剩两路，与现网等价。
        """
        pairs = [(self.trgm, cfg.w_trgm), (self.vector, cfg.w_vector)]
        if cfg.keyword_leg and self.keyword:
            pairs.append((self.keyword, cfg.w_keyword))
        nxt = self.retry
        if nxt is not None:
            pairs += [(nxt.trgm, cfg.w_trgm), (nxt.vector, cfg.w_vector)]
            if cfg.keyword_leg and nxt.keyword:
                pairs.append((nxt.keyword, cfg.w_keyword))
        return [lst for lst, _ in pairs], tuple(w for _, w in pairs)

    def trace_tuple(self) -> tuple:
        """给 retrieval_traces / 指标用的五元组（计数, 计数, max_trgm, max_vec, vec_failed）。

        口径抄现网：`_LAST_LEGS` 在重试轮被整体覆盖，所以出口记账的是**最后一轮**。
        这是 RAG-3（tools.py:399-403）的处置结果，不是笔误——v2 若改成"首轮"或
        "两轮取大"，中控台的空召回率、低置信计数与 RC-1b 判定就跟现网不可比。
        """
        last = self.retry if self.retry is not None else self
        return (len(last.trgm), len(last.vector), last.max_trgm, last.max_vec,
                last.vec_failed)


_CN_RUN_RE = re.compile(r"[一-鿿]{2,}")


def cnf_fragments(text: str, width: int = 4, cap: int = 6) -> list[str]:
    """把中文查询切成可 ILIKE 的片段：先按非汉字断开成段，长段用定长滑窗取样。

    为什么不用整句：现网 _keyword_fill 就是拿标点切出的**整个句子**去 ILIKE，而中文
    没有词间空格，"楼上漏水把我家泡了"这种整句在法条原文里不存在，所以那条腿恒空。
    ponytail: 定长滑窗是无词典近似，切不出词边界、片段数随句长线性增长；升级路径是
    接分词（jieba 之类）或库内既有 synonym 表，届时只换本函数，管线不动。
    """
    out: list[str] = []
    for seg in _CN_RUN_RE.findall(text or ""):
        if len(seg) <= width:
            out.append(seg)
        else:
            out.extend(seg[i:i + width] for i in range(0, len(seg) - width + 1, 2))
    seen: set[str] = set()
    kept: list[str] = []
    for frag in out:
        if frag not in seen:
            seen.add(frag)
            kept.append(frag)
        if len(kept) >= cap:
            break
    return kept


def _like_escape(kw: str) -> str:
    """ILIKE 通配符转义（同现网 _keyword_fill 的口径：% _ \\ 三个字符）。"""
    return kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row(item: "asyncpg.Record | dict") -> dict:
    """DB 行 → 候选 dict：字段集合与现网一致，下游拼装/引用溯源无需分支。"""
    return {"chunk_key": item["chunk_key"], "heading": item["heading"],
            "content": item["content"], "similarity": round(item["sim"], 4) if item["sim"] else 0,
            "source_doc": item["source_doc"] or item["source"]}


async def recall_trgm(conn: asyncpg.Connection, query: str, cfg: Cfg,
                      permissions: list | None) -> list:
    """词面腿 A：pg_trgm similarity 序（与现网同一打分口径，similarity 而非 word_similarity）。"""
    from .tools import _perm_clause
    args = [query, cfg.recall_limit]
    if permissions:
        args.append(permissions)
    rows = await conn.fetch(
        "SELECT chunk_key, source, heading, content, source_doc, "
        "similarity(content, $1) as sim "
        "FROM knowledge_chunks WHERE source = 'civil_code' AND valid "
        + _perm_clause(permissions) +
        "ORDER BY sim DESC LIMIT $2", *args)
    return [{**_row(r), "content": r["content"][:cfg.content_cap]} for r in rows]


async def recall_vector(conn: asyncpg.Connection, embedding: list, cfg: Cfg,
                        permissions: list | None) -> list:
    """语义腿：pgvector 余弦。embedding 为空直接返回 []（调用方据此标 vec_failed）。"""
    if not embedding:
        return []
    from .tools import _perm_clause
    args = [json.dumps(embedding), cfg.recall_limit, EMBEDDING_MODEL]
    if permissions:
        args.append(permissions)
    # probes 拉满 = 全簇扫描：civil 通道仅千余行，ivfflat 默认 probes=1 的近似结果是
    # 负收益（现网 tools.py:195-199 的实测结论，金条位次随 LIMIT 漂移）。
    # ponytail: 行数过 5 万需重建 lists 并重估这里。
    async with conn.transaction():
        await conn.execute(f"SET LOCAL ivfflat.probes = {int(cfg.ivfflat_probes)}")
        rows = await conn.fetch(
            "SELECT chunk_key, source, heading, content, source_doc, "
            "1 - (embedding <=> $1::vector) AS sim "
            "FROM knowledge_chunks WHERE source = 'civil_code' "
            "AND valid AND embedding IS NOT NULL "
            "AND embedding_model = $3 "
            + _perm_clause(permissions, 4) +
            "ORDER BY embedding <=> $1::vector LIMIT $2", *args)
    return [{**_row(r), "content": r["content"][:cfg.content_cap]} for r in rows]


async def recall_keyword(conn: asyncpg.Connection, query: str, cfg: Cfg,
                         permissions: list | None) -> list:
    """词面腿 B：中文片段 ILIKE 补充，独立成一腿（现网是同池补位，实测恒不产出）。

    片段来自 cnf_fragments；每片段一条参数化 ILIKE，命中按片段顺序入池、按 chunk_key
    去重、总量截到 recall_limit。相似度给固定 0.5：这腿只表"词面出现过"，是弱信号，
    所以 RRF 里权重低于向量腿。
    """
    from .tools import _perm_clause
    out: list[dict] = []
    seen: set[str] = set()
    for frag in cnf_fragments(query, cfg.kw_width, cfg.kw_cap):
        args = [f"%{_like_escape(frag)}%", cfg.recall_limit]
        if permissions:
            args.append(permissions)
        rows = await conn.fetch(
            "SELECT chunk_key, source, heading, content, source_doc, 0.5 as sim "
            "FROM knowledge_chunks WHERE source = 'civil_code' AND valid "
            + _perm_clause(permissions) +
            "AND content ILIKE $1 ESCAPE '\\' LIMIT $2", *args)
        for r in rows:
            if r["chunk_key"] in seen or len(out) >= cfg.recall_limit:
                continue
            seen.add(r["chunk_key"])
            out.append({**_row(r), "content": r["content"][:cfg.content_cap]})
        if len(out) >= cfg.recall_limit:
            break
    return out


async def recall_all(conn: asyncpg.Connection, search_query: str, cfg: Cfg,
                     permissions: list | None) -> Legs:
    """一次并行取三腿。embedding 与 trgm 无依赖关系，先并行启动再串词面/向量腿。

    词面腿关时连查询都不发（`Cfg.keyword_leg=False` 的结果本来就不进融合，
    发出去只是白付最多 kw_cap 次 ILIKE）。
    """
    from .kb_embedding import _generate_embedding
    trgm, embedding = await asyncio.gather(
        recall_trgm(conn, search_query, cfg, permissions),
        _generate_embedding(search_query),
    )
    keyword = (await recall_keyword(conn, search_query, cfg, permissions)
               if cfg.keyword_leg else [])
    vector = await recall_vector(conn, embedding or [], cfg, permissions)
    return Legs(trgm=trgm, vector=vector, keyword=keyword,
                max_trgm=max((c["similarity"] for c in trgm), default=0.0),
                max_vec=max((c["similarity"] for c in vector), default=0.0),
                vec_failed=not embedding, embedding=embedding or [])


def is_low_conf(legs: Legs, cfg: Cfg) -> bool:
    """双路都低置信才算低置信（阈值语义与现网 _is_low_confidence 一致：严格小于）。"""
    return legs.max_trgm < cfg.rewrite_trgm_sim and legs.max_vec < cfg.rewrite_vec_sim


def fuse(legs: Legs, search_query: str, cfg: Cfg,
         diagnostics: dict | None = None) -> list:
    """RRF 融合 + 条号直钉提升。融合与提升都是现网既有实现的复用。

    rrf 快照落在**提升前**：现网 tools.py:429 的 diagnostics["rrf"] 取自 `_recall_and_fuse`
    里的 merged，而 `_promote_pinned` 在调用方 `_expand_and_guard` 才跑。放提升后会与
    现网同分并列的次序不可比（10-14 CC065 定点探针实测到：成员集合一致、次序不同）。
    """
    from .kb_expand import _promote_pinned
    from .tools import _rrf_merge
    ranked, weights = legs.ranked(cfg)
    merged = _rrf_merge(*ranked, k=cfg.rrf_k, weights=weights)
    if diagnostics is not None:
        diagnostics["rrf"] = chunk_keys(merged)
    return _promote_pinned(merged, search_query)


async def rerank(query: str, merged: list, legs: Legs, cfg: Cfg,
                 diagnostics: dict | None = None) -> tuple[list, bool]:
    """挑重排候选 → CE 打分 → 与 RRF 序线性融合。CE 不可用时返回 (候选, False) 保 RRF 序。

    候选挑选用的召回列表与 RRF 同源（`legs.ranked`），不再另列一遍三腿——
    现网 `_select_rerank_candidates(merged, recall_lists, ...)` 传的也是含重试轮的那组。
    """
    from ..services.rag_request_trace import record_span
    pool = _select_rerank_candidates(merged, legs.ranked(cfg)[0], cfg.rerank_top)
    if diagnostics is not None:
        diagnostics["rerank_candidates"] = chunk_keys(pool)
    if not pool:
        return [], False
    started = time.perf_counter()
    reranked = await _rerank_local(query, pool)
    record_span(
        "rerank",
        latency_ms=round((time.perf_counter() - started) * 1000),
        result_count=len(reranked or []),
        attributes={"candidate_count": len(pool), "rerank_top": cfg.rerank_top},
    )
    if not reranked:
        return pool, False
    if cfg.blend > 0:
        reranked = _blend_rrf_ce(pool, reranked, cfg.blend)
    return reranked, True


async def prepare_query(query: str) -> tuple[str, dict | None]:
    """检索前的三步：法律映射纠正闸 → 口语→术语 → 法条编号归一化。

    全部复用现网实现（law_mapping / _map_colloquial_to_legal / normalize_article_ref），
    本函数只决定顺序。返回 (要检索的查询, 拦截 payload)；payload 非 None 表示映射闸
    拦下（该问题不属民法典调整范围），调用方把 payload 原样返回即可，不执行检索。
    """
    from .article_normalizer import normalize_article_ref
    from .tools import _law_mapping_check, _map_colloquial_to_legal
    blocked = _law_mapping_check(query)
    if blocked:
        return "", blocked
    return normalize_article_ref(_map_colloquial_to_legal(query)), None


def _unimplemented_flags() -> tuple[str, ...]:
    """返回"现网有、v2 没实现，且当前被 env 打开"的开关名。

    邻接扩召/章内余弦扩召/高分 CE 保护带三段在现网默认关闭，所以 v2 首版不接是等价的；
    等价的前提是"关着"。有人把 KB_NEIGHBOR_ENABLED 之类打开而入口仍指 v2，就会拿到
    一份少了扩召的结果，而 method 标签与现网一模一样——静默分叉最难查，所以这里显式认出来。
    """
    from .kb_ce_band import CE_BAND_ENABLED
    from .kb_expand import CHAPTER_EXPAND_ENABLED, NEIGHBOR_ENABLED
    return tuple(name for name, on in (
        ("KB_NEIGHBOR_ENABLED", NEIGHBOR_ENABLED),
        ("KB_CHAPTER_EXPAND", CHAPTER_EXPAND_ENABLED),
        ("KB_CE_BAND_ENABLED", CE_BAND_ENABLED),
    ) if on)


_WARNED_FLAGS: set[str] = set()


def _warn_missing_expansions() -> None:
    """未实现的扩召每键只告警一次：这是进程级配置状态，每条查询刷一行会淹掉日志。"""
    fresh = [k for k in _unimplemented_flags() if k not in _WARNED_FLAGS]
    if fresh:
        _WARNED_FLAGS.update(fresh)
        logger.warning(f"retrieval_v2 未实现 {'/'.join(fresh)}（现网这三段默认关闭）："
                       f"开关已打开，两份实现的结果会分叉——对拍前先关掉，"
                       f"或把对应行为接进 v2 再复验")


def _recall_span(legs: Legs, cfg: Cfg, started: float, stage: str) -> None:
    """把一轮召回打成 span。stage 名与属性键照现网（tools.py:376-385），
    中控台的阶段瀑布不用改读取端就能同时吃两份实现；v2 只多一个 kw_hits
    （词面腿在现网恒不产出，那边没有这个观测量）。
    """
    from ..services.rag_request_trace import record_span
    record_span(
        stage,
        latency_ms=round((time.perf_counter() - started) * 1000),
        result_count=len(legs.trgm) + len(legs.vector),
        attributes={"trgm_hits": len(legs.trgm), "vec_hits": len(legs.vector),
                    "vec_failed": legs.vec_failed, "kw_hits": len(legs.keyword),
                    "recall_limit": cfg.recall_limit},
    )


def _diag_legs(diagnostics: dict | None, legs: Legs, suffix: str = "") -> None:
    """各腿候选快照进诊断字典；键名同现网（首轮 trgm/vector，重试轮加 _retry 后缀）。

    keyword{suffix} 暂时没有读取端：漏斗归因脚本的 recall_union 只并 trgm/vector
    （tests/run_retrieval_eval.py:124-133），多出来的这两个键目前只给人对拍时看。
    """
    if diagnostics is None:
        return
    diagnostics[f"trgm{suffix}"] = chunk_keys(legs.trgm)
    diagnostics[f"vector{suffix}"] = chunk_keys(legs.vector)
    diagnostics[f"keyword{suffix}"] = chunk_keys(legs.keyword)


def _trace(query: str, res: dict, legs: Legs, latency_ms: float) -> None:
    """出口记账：落 retrieval_traces + 打指标，用现网同一实现。

    与现网唯一的写法差别是 legs 由参数传入（现网从 _LAST_LEGS contextvar 取）——
    ReAct 链路把检索放进 create_task 子任务，子任务对 contextvar 的重新绑定父任务
    看不见，那份口径本身是可疑的；显式传参不需要再赌上下文可见性。
    """
    from .retrieval_trace import record
    record(query, res, legs.trace_tuple(), latency_ms)


async def _search_pipeline(conn: asyncpg.Connection, query: str, search_query: str,
                           top_k: int, permissions: list | None, username: str,
                           cfg: Cfg, retried: bool,
                           diagnostics: dict | None) -> tuple[dict, Legs]:
    """三腿召回 →（低置信改写重试）→ RRF → 重排融合 → top_k，返回 (结果, 最终 Legs)。

    四段 span 与漏斗快照都在本函数里打；把 Legs 一并返回是给出口记账用的，
    现网这一步靠 _LAST_LEGS 传，这里走返回值。
    """
    from ..services.rag_request_trace import record_span
    started = time.perf_counter()
    legs = await recall_all(conn, search_query, cfg, permissions)
    _recall_span(legs, cfg, started, "recall")
    _diag_legs(diagnostics, legs)
    # 基名逐字对齐现网 tools.py:471（"trgm+vector"），监控与中控台按 method 词表读数，
    # 两份实现不该各造一套；"+kw" 是 v2 独有的词面腿标记段。
    method = "trgm+vector" + ("+kw" if cfg.keyword_leg else "")
    if retried and is_low_conf(legs, cfg):
        rewritten = await _rewrite_query_for_recall(query, username=username)
        if rewritten:
            # 重试轮挂到 legs.retry 并列参融（不是拼接进同一条列表）；低置信判定
            # 仍按首轮分数，与现网一致。
            logger.info(f"检索低置信（trgm={legs.max_trgm:.2f}, vec={legs.max_vec:.2f}），"
                        f"触发改写重试: {search_query[:40]}... → {rewritten[:40]}...")
            started = time.perf_counter()
            again = await recall_all(conn, rewritten, cfg, permissions)
            _recall_span(again, cfg, started, "recall_retry")
            _diag_legs(diagnostics, again, "_retry")
            legs = replace(legs, retry=again)
            method += "+rewrite_retry"
    started = time.perf_counter()
    merged = fuse(legs, search_query, cfg, diagnostics)
    record_span(
        "fusion",
        latency_ms=round((time.perf_counter() - started) * 1000),
        result_count=len(merged),
        attributes={"rewrite_retry": legs.retry is not None,
                    "rrf_trgm_weight": cfg.w_trgm},
    )
    diag = {}
    if diagnostics is not None:
        diag = {"diagnostics": diagnostics}
    if not merged:
        return {"results": [], "method": method, **diag}, legs
    # 重排窗走运行期覆盖（同现网 tools.py:467）：中控台能把 RERANK_TOP 热改成 5~20 里的
    # 任意值，只读 env 的 v2 会跟现网分叉；get_rerank_top 在无 Redis 时自己回退到 cfg 值。
    from ..services.rag_runtime_config import get_rerank_top
    cfg = replace(cfg, rerank_top=await get_rerank_top(cfg.rerank_top))
    final, hit_ce = await rerank(query, merged, legs, cfg, diagnostics)
    if diagnostics is not None:
        diagnostics["final"] = chunk_keys(final[:top_k])
    return {"results": final[:top_k],
            "method": method + ("+rrf+local_rerank" if hit_ce else "+rrf"), **diag}, legs


async def search_v2(conn: asyncpg.Connection, query: str, top_k: int = 5,
                    permissions: list | None = None,
                    username: str = "", cfg: Cfg | None = None,
                    retried: bool = True) -> dict:
    """v2 主干入口：装配配置 → 预处理闸 → 检索管线 → 出口记账。

    返回结构与 tools.search_knowledge 对齐（results / method，KB_DIAGNOSTICS_ENABLED=1
    时多一个 diagnostics），调用方可直接换。permissions 语义同现网：None 不过滤、
    [] 仅公开、非空为公开+命中权限组。
    """
    cfg = cfg or Cfg.from_env()
    # 召回窗跟随 top_k 放大，与现网 tools._search_two_legs 的 max(RECALL_LIMIT, top_k) 同口径。
    # 10-13 首轮容器 A/B 抓出来的偏差就在这儿：v2 恒用 15，评测传 top_k=20 时现网每腿取 20 条，
    # CC076 的 6~10 位金条被缩小的窗挤掉。
    cfg = replace(cfg, recall_limit=max(cfg.recall_limit, top_k))
    search_query, blocked = await prepare_query(query)
    if blocked:
        # 映射闸拦下时不记账，与现网一致（tools.py:531-533 在 _t0 之前就 return 了）
        return blocked
    _warn_missing_expansions()
    if conn is None:
        from ..core.db import get_pool
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            return await search_v2(conn, query, top_k, permissions, username, cfg, retried)

    started = time.perf_counter()
    # 口径差一条，对拍延迟时要认：现网的计时含 pool.acquire 等待，这里从取到连接之后起算。
    res, legs = await _search_pipeline(conn, query, search_query, top_k, permissions,
                                       username, cfg, retried,
                                       {} if diagnostics_enabled() else None)
    _trace(query, res, legs, (time.perf_counter() - started) * 1000)
    return res
