"""民法典检索的候选池扩展子系统 —— 从 tools.py 拆出（2026-09-14，防熵检查点）。

tools.py 在检索质量五阶段迭代后达 775 行（硬限 600），按 web_search/project_kb
同例整块搬迁。本模块只做「扩召与保位」：条号直钉提升、±N 邻接条文伴生召回
（默认关）、章内余弦扩召（默认关）、重排窗保护带。排序与融合仍归 tools.py。
"""

import json
import os
import re
import threading

from ..core.logging import setup_logging
from .article_normalizer import _chinese_to_arabic

logger = setup_logging()

# 条号 pattern：中文数字条号（查询归一化后均为中文数字形态）
_PIN_RE = re.compile(r"第[一二三四五六七八九十百千零两]{1,12}条")


def _promote_pinned(merged: list, search_query: str) -> list:
    """把查询点名的条号对应块提升到融合序最前（2026-09-12 新增）。

    条号进入查询有两条路：用户原话（"第533条"归一化后）或映射表注入。它命中的
    trgm 榜首是**确定性信号**，但 RRF 里 trgm 权重 0.4 时 vector 路任意前 13 名
    都压过 trgm 第 1 名——CC094 实测金条从 trgm 第 1/2 位被挤到融合第 16/17 位、
    进不了重排窗。提升只保证"进窗"，最终排序仍由 CE×RRF 融合（质量闸门）决定。
    ponytail: O(候选×pin数)，候选池 ≤30 忽略不计。
    """
    pins = tuple(set(_PIN_RE.findall(search_query)))
    if not pins or not merged:
        return merged
    hit = [c for c in merged if c["content"].startswith(pins)]
    if not hit:
        return merged
    rest = [c for c in merged if not c["content"].startswith(pins)]
    return hit + rest


# ===== 邻接条文聚合（2026-09-12 通宵迭代，默认关闭）=====
# 动因（tests/analysis_neighbor_bucket.py 实测）：train 12 条 recall@5<1 用例中 9 条
# 缺失金条是已召回条文的 ±1~2 邻居。train 实测 top3±1+guard9 有效性（0.9368→0.9447），
# **但 holdout 全面回退（0.8407→0.8296 / MRR 0.8495→0.8272）——邻接插入在 holdout
# 查询上挤掉的已排名金条多于救回的**，故默认关闭。train 增益 holdout 不复现 =
# 过拟合信号，这是 §六 train/holdout 纪律的活案例。打开需先扩验收集再重验。
NEIGHBOR_ENABLED = os.getenv("KB_NEIGHBOR_ENABLED", "0") == "1"
NEIGHBOR_TOP = int(os.getenv("KB_NEIGHBOR_TOP", "3"))    # 对融合前几名扩邻接
NEIGHBOR_SPAN = int(os.getenv("KB_NEIGHBOR_SPAN", "1"))  # ±span 邻域
# 强先验保护带宽度：RRF 前 G 名不因邻接插入出局（CC141 的金条原在第 9 位）
NEIGHBOR_GUARD = int(os.getenv("KB_NEIGHBOR_GUARD", "9"))
_ART_HEAD_RE = re.compile(r"^第([一二三四五六七八九十百千零两]+)条")


_NEIGHBOR_LOCK = threading.Lock()
_NEIGHBOR_INDEX: dict[int, str] | None = None  # 条号 → chunk_key（进程级缓存）


def _permission_clause(permissions: list | None, param_index: int = 2) -> str:
    """扩召权限过滤片段；语义与 tools._perm_clause 保持一致。

    本模块不能反向导入 tools.py（tools 已导入本模块），因此保留同款片段。
    邻接查询权限参数固定 $2（默认）；章扩召因新增 embedding_model 占 $2，
    权限顺延 $3。
    """
    if permissions is None:
        return ""
    if permissions:
        return f" AND (COALESCE(permission, '{{}}') = '{{}}' OR permission && ${param_index}) "
    return " AND COALESCE(permission, '{}') = '{}' "


async def _article_index(conn) -> dict[int, str]:
    """条号→chunk_key 索引，进程内缓存一次。

    2026-09-12 实测：邻接批查用 content LIKE 时 p95 飙到 8.5s（E2~E4），索引化后
    邻接查找降为内存字典 + chunk_key 批取。1260 行、加载一次 ~10ms。
    ponytail: 知识库重建后进程需重启才能看到新条号（本表基本只增不改条号）。
    """
    global _NEIGHBOR_INDEX
    if _NEIGHBOR_INDEX is not None:
        return _NEIGHBOR_INDEX
    with _NEIGHBOR_LOCK:
        if _NEIGHBOR_INDEX is not None:
            return _NEIGHBOR_INDEX
        rows = await conn.fetch(
            "SELECT content, chunk_key FROM knowledge_chunks "
            "WHERE source = 'civil_code' AND valid "
            "AND content ~ '^第[一二三四五六七八九十百千零两]+条'")
        idx: dict[int, str] = {}
        for r in rows:
            m = _ART_HEAD_RE.match(r["content"])
            if m:
                a = _chinese_to_arabic(m.group(1))
                if a and a not in idx:  # 同条号多块时取首块（实测无此情况，兜底）
                    idx[a] = r["chunk_key"]
        _NEIGHBOR_INDEX = idx
        return idx


async def _expand_neighbors(conn, merged: list,
                            permissions: list | None = None) -> list:
    """对融合序头部候选的相邻条文作伴生召回（已在池中的跳过）。

    只扩前 KB_NEIGHBOR_TOP 名、±KB_NEIGHBOR_SPAN 条，伴生块插在触发者之后——
    进得了重排窗才有意义，缀在池尾等于白算。条号→块经进程级索引（_article_index）
    内存解析，chunk_key 批取。
    """
    if not merged:
        return merged
    index = await _article_index(conn)
    parsed: list[tuple[int, int]] = []  # (pool 位置, 条号)
    in_pool: set[int] = set()
    for i, c in enumerate(merged):
        m = _ART_HEAD_RE.match(c["content"])
        if m:
            a = _chinese_to_arabic(m.group(1))
            if a:
                in_pool.add(a)
                if i < NEIGHBOR_TOP:
                    parsed.append((i, a))
    targets: dict[int, int] = {}  # 邻居条号 → 插入位置
    for i, a in parsed:
        for d in range(1, NEIGHBOR_SPAN + 1):
            for t in (a - d, a + d):
                if 1 <= t <= 1260 and t not in in_pool and t not in targets:
                    targets[t] = i
    keys = [index[t] for t in targets if t in index]
    if not keys:
        return merged
    args = [keys]
    if permissions:
        args.append(permissions)
    rows = await conn.fetch(
        "SELECT chunk_key, heading, content FROM knowledge_chunks "
        "WHERE chunk_key = ANY($1::text[]) AND valid"
        + _permission_clause(permissions), *args)
    by_art = {}
    for r in rows:
        m = _ART_HEAD_RE.match(r["content"])
        if m:
            by_art[_chinese_to_arabic(m.group(1))] = r
    if not by_art:
        return merged
    out: list = []
    for i, c in enumerate(merged):
        out.append(c)
        if i >= NEIGHBOR_TOP:
            continue
        a = next((a for pos, a in parsed if pos == i), None)
        if a is None:
            continue
        for d in range(1, NEIGHBOR_SPAN + 1):
            for t in (a - d, a + d):
                if t in by_art:
                    r = by_art.pop(t)
                    out.append({"chunk_key": r["chunk_key"], "heading": r["heading"],
                                "content": r["content"][:500], "similarity": 0.0})
    return out


# ===== 章内余弦扩召（2026-09-13）=====
# 动因（tests/analysis_linkage.py 实测）：缺失金条与 top5 命中同章的比例 train 11/11、
# holdout 20/23——法条的"纵横交错"是编/章制度结构（条号互引为 0），章内成员从未
# 参与排序竞争。本章扩召 = top1 命中定章 → 章内成员向量与查询向量算余弦 → 择优
# KB_CHAPTER_EXPAND_K 条入池，让 CE×RRF 公平裁决。区别于 ±1 几何邻居：余弦是
# 语义择优，不是位置相邻。
CHAPTER_EXPAND_ENABLED = os.getenv("KB_CHAPTER_EXPAND", "0") == "1"
CHAPTER_EXPAND_K = int(os.getenv("KB_CHAPTER_EXPAND_K", "3"))


def _cosine(a: list, b: list) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(x * x for x in b) ** 0.5
    return num / (da * db) if da and db else 0.0


async def _expand_chapter(conn, merged: list, query_embedding: list | None,
                          permissions: list | None = None) -> list:
    """top1 命中所在章的成员，按与查询向量的余弦择优 KB_CHAPTER_EXPAND_K 条入池。

    成员 = heading 完全相同的其他块（heading 形如 "第五编　婚姻家庭 / 第五章　收养"，
    末段即章）。已入池成员跳过；查询向量缺失（embedding 挂了）不扩。
    """
    if not merged or not query_embedding:
        return merged
    first = _ART_HEAD_RE.match(merged[0]["content"])
    if not first:
        return merged
    if not merged[0].get("heading"):
        return merged
    # embedding_model 过滤（2026-09-28 kb 链审查 KB-R1）：向量召回路一直带
    # `AND embedding_model = $N` 防跨模型空间比余弦，本章扩召漏了同一道闸——
    # 库内新旧模型块并存（换 embedding 模型的过渡期）时，与异模型块的余弦
    # 是跨向量空间的噪声分，择优入池的全是垃圾。
    from ..core.config import EMBEDDING_MODEL
    args = [merged[0]["heading"], EMBEDDING_MODEL]
    if permissions:
        args.append(permissions)
    rows = await conn.fetch(
        "SELECT chunk_key, heading, content, embedding FROM knowledge_chunks "
        "WHERE source = 'civil_code' AND valid AND heading = $1 "
        "AND embedding IS NOT NULL AND embedding_model = $2 "
        + _permission_clause(permissions, 3), *args)
    in_pool = {c["chunk_key"] for c in merged}
    q = query_embedding
    scored = []
    for r in rows:
        if r["chunk_key"] in in_pool or r["heading"] != merged[0]["heading"]:
            continue  # 池内已有成员与越权行（防御）不重复
        emb = json.loads(r["embedding"]) if isinstance(r["embedding"], str) else None
        if emb:
            scored.append((_cosine(q, emb), r))
    scored.sort(key=lambda x: -x[0])
    picked = [r for _, r in scored[:CHAPTER_EXPAND_K]]
    if not picked:
        return merged
    out = [{"chunk_key": r["chunk_key"], "heading": r["heading"],
            "content": r["content"][:500], "similarity": 0.0} for r in picked]
    return merged[:1] + out + merged[1:]


def _apply_neighbor_guard(expanded: list, guard: list, top_n: int) -> list:
    """重排窗 = 邻接扩展后的前 top_n ∪ 保护带中未入窗者。

    保护带（RRF 前 NEIGHBOR_GUARD 名）若被邻接挤到窗外，以窗尾身份补回——
    保住进窗资格；融合排序仍由 CE×RRF 决定（CC141 教训的修正）。
    """
    window = expanded[:top_n]
    seen = {c["chunk_key"] for c in window}
    window += [c for c in guard if c["chunk_key"] not in seen]
    return window
