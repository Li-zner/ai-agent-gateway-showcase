# -*- coding: utf-8 -*-
"""清洗覆盖率闸（SOP 批次 A / A1）：条号连续性检测 + 候选与落库条数对账。

为什么要有这个闸：民法典清洗曾因中文数字正则漏「千」后「零」位，第 1040 条起整编
静默跳过，应有 1260 条只入库 861 条，而检索评测指标完全正常——评测集没覆盖缺失
区段，指标看不见语料少了三成。条数对账与条号连续性是这类「静默少一段」的唯一抓手，
所以闸口独立成模块，脚本与中控台共用一把尺，不再各写各的 count 打印。

只报不补：缺号一律如实报告，不自动补号也不自动放行——缺号可能是原文本就没有
（如附则之后无条文），补号会造出假条文。

切分状态机也住这里（10-14 融入第 2 步）：`Dockerfile:76-83` 按文件名白名单 COPY `scripts/`，
建库脚本 import 不到清洗 CLI，所以「入库侧与清洗侧同一把尺」只能靠闸包里的这一份函数落地。
"""
import hashlib
import re
from typing import Iterable

from ...agents.article_normalizer import _chinese_to_arabic  # 复用同一套中文数字换算（先例 kb_expand.py:14）
# 表格折行的三个字面量与造行那把尺同源（`type_layer.flatten_table` / `struct_legs`）：
# 这里再写一份「表格表头：」就是第二把尺，一边改一边不改会得到"造得出、认不出"。
# `type_layer` 不 import 本模块，所以这条 import 不构成环。
from .type_layer import _COLUMN_SEP, _SHEET_TAG, _TABLE_HEAD_PREFIX

# 行首条号字符集必须四处一致（含千/零/两）：本闸、kb_expand.py:52、rag_change_sets.py:29、
# 用户输入侧 article_normalizer.py:108。
# 这条一致性就是 861 事故的直接教训：老脚本用的 [一二三四五六七八九十百] 少了千和零，
# 第一千零四条这类行首匹配不上，整段被当正文吞掉。契约测试 test_article_head_charset_parity 钉住。
_ARTICLE_HEAD_RE = re.compile(r"^第([一二三四五六七八九十百千零两0-9]{1,12})条")

# 候选条目里判定「像一条条文」的最小长度：与 scripts/build_kb_final.py:72 同口径，
# 短于这个值的行首条号多为目录、交叉引用或排版残留，不计入覆盖率。
_MIN_ARTICLE_CHARS = 10


class CoverageMismatch(RuntimeError):
    """清洗覆盖率不达标。

    携带结构化字段而不是只拼一句中文：调用方要按 missing / actual / expected 出报告，
    解析异常文本取数是本仓 10-07 已批评过的形态。
    """

    def __init__(self, *, stage: str, expected: int, actual: int,
                 missing: Iterable[int] = (), detail: str = ""):
        self.stage = stage
        self.expected = expected
        self.actual = actual
        self.missing = sorted(set(missing))
        self.detail = detail
        msg = (f"清洗覆盖率不达标[{stage}]: 期望 {expected} 条，实际 {actual} 条")
        if self.missing:
            msg += f"，缺号 {len(self.missing)} 个（首个 {self.missing[0]}）"
        if detail:
            msg += f"；{detail}"
        super().__init__(msg)


def _to_article_int(token: str) -> int | None:
    """条号字符串转整数：阿拉伯直接转，中文走共用换算，转不出返回 None（不猜号）。"""
    if token.isdigit():
        return int(token)
    return _chinese_to_arabic(token) or None


def article_number(line: str) -> int | None:
    """行首条号：是条文行返回号数，标题/正文引用行返回 None（一个判据，切分与对账共用）。"""
    matched = _ARTICLE_HEAD_RE.match((line or "").strip())
    if not matched:
        return None
    return _to_article_int(matched.group(1)) or None


# 章/节/编/分编标题判据。数词字符类字面量与 _ARTICLE_HEAD_RE 逐字符一致（同一把尺，漂移会被
# test_article_head_charset_parity_with_retrieval_side 扫出来）；级别组是**完整级别**的交替式，
# 「分编」整体一级，不靠数词类里塞「分」——那样「第五分编」与「第五编」会在级别上混淆。
_SECTION_HEAD_RE = re.compile(r"^第([一二三四五六七八九十百千零两0-9]{1,12})(分编|章|节|编)")


def section_heading(line: str) -> str | None:
    """行首是章/节/编/分编标题时返回级别字（"章"/"节"/"编"/"分编"），否则返回 None。

    与 `scripts/build_kb_final.py` 旧写法的差处在两处（Bug日志 58/59）：旧写法只认「编/章」两级，
    且用 `len(line) < 10` 当长度启发式——于是「第四节　宣告失踪和宣告死亡」这种节级、
    「第四节　个体工商户和农村承包经营户」这种 17 字长标题都掉进正文分支，被拼到**上一条条文**末尾；
    而「第二分编　典型合同」数词与「编」之间多一个「分」，两头都不认，短的被旧编级分支
    当「编」用掉、顶掉编级上下文。本判据四级都认，也不看长度。
    「部分编」这种写法**没进判据**：真语料 16 行候选全是「第X分编」，实测没有的形状不写进尺子。
    判据立在闸包而不是质检脚本里：58/59 的修法要用它改切分，散进调用方就长出第三份副本。
    """
    matched = _SECTION_HEAD_RE.match((line or "").strip())
    return matched.group(2) if matched else None


# heading 里出现的级别槽，按层级顺序拼；「节」不在其中（见 split_articles 口径 2）。
_HEADING_SLOTS = ("编", "分编", "章")


def split_articles(lines: Iterable[str], *, source: str, key_prefix: str | None = None,
                   min_chars: int = _MIN_ARTICLE_CHARS) -> list[dict]:
    """编/分编/章/节 → 条 的切分状态机（唯一一份，入库侧与清洗侧共用）。

    四条口径，都是被实测逼出来的：
    1. 行首判据一律走本模块 `section_heading()` / `article_number()`，调用方零字符类——
       Bug日志 54 的 861 事故正是建库脚本自带一把缺「千/零」的尺，行首认不出就当正文吞掉。
    2. heading 按 编 / 分编 / 章 拼；**节级标题认得出但不进 heading**（与旧清洗侧同口径），
       这样改动面就等于 `plan` 差分量出来的那份，不会多出"整级标题进 heading"的意外。
    3. 只有行首带条号的片段才成块：文档名与裸标题不成块，且同名标题内容 md5 同键，
       落库时会互相覆盖。
    4. `key_prefix` 缺省 `f"{source}_"`；民法典现网存量键是 `civil_<md5>`，由
       `rag_change_sets.py` 的 `_SOURCE_PREFIXES` 认这个前缀，建库侧必须显式传 `"civil_"`。
    """
    prefix = key_prefix or f"{source}_"
    chunks: list[dict] = []
    slots = {"编": "", "分编": "", "章": ""}
    arts: list[str] = []
    cur = ""

    def _close() -> None:
        """收尾在写的条文：行首带条号才收进待flush队列，标题与文档名一律丢弃。"""
        head = cur.split("\n", 1)[0] if cur else ""
        if article_number(head):
            arts.append(cur.strip())

    def _flush() -> None:
        heading = " / ".join(v for v in (slots[k] for k in _HEADING_SLOTS) if v)
        for art in arts:
            if len(art) < min_chars:
                continue
            chunks.append({"chunk_key": prefix + hashlib.md5(art.encode("utf-8")).hexdigest()[:16],
                           "source": source, "heading": heading, "content": art})
        arts.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        level = section_heading(line)
        if level:
            _close()
            cur = ""
            if level != "节":
                _flush()
                if level == "编":
                    slots.update({"编": line, "分编": "", "章": ""})
                elif level == "分编":
                    slots.update({"分编": line, "章": ""})
                else:
                    slots["章"] = line
        elif article_number(line):
            _close()
            cur = line
        else:
            cur += "\n" + line if cur else line
    _close()
    _flush()
    return chunks


def split_table_rows(lines: Iterable[str], *, source: str, key_prefix: str | None = None,
                     min_chars: int = _MIN_ARTICLE_CHARS) -> list[dict]:
    """表格族的切分：**一行即一条**（SOP-06 阶梯第 2 阶的表格趟，10-18 业主点单）。

    为什么单开一条而不改 `split_articles`：那台状态机只认行首「第X条」，表格折出来的行
    （`列名：值｜列名：值`）一条都不认 ⇒ 真件 `tests/evaluation_report.xlsx` 改前实测
    出块 0、`dropped_lines=114`（整张表全丢，而召回闸根本看不到它——普查按 `status=="ok"` 挑，
    表格件是 `no_article`）。可每行都自带列名、本来就是自描述的一条。
    **号位为什么不放首列值**：现装六个号位消费者（候选去重键、差分对齐键、
    quality 报表、repair 漂移守卫、`line_census` 的 `article_lines`、strip 报表）都从行首
    「第X条」parse 出 **int**，把 `TR001` 这种用例 ID 塞进号位会让六把尺齐读 `None`——
    正是 Bug日志 71/74 那一族「把量不到显示成没有」。所以业主选的「首列值当条号」落在
    `heading`（检索可见、能回到原表），缺首列才退 `行N`；伪造一个 `第N条` 写进正文否掉，
    那是往语料里写字面量。
    口径：`表格表头：` 前缀的行只当上下文（工作表标记行给 sheet 名、列名行给列名清单），
    按构造不进语料；`chunk_key`、heading 的 ` / ` 拼法、`min_chars` 都与 `split_articles` 同源，
    两条腿出的是同一种块形状。
    """
    prefix = key_prefix or f"{source}_"
    chunks: list[dict] = []
    sheet = ""
    cols: list[str] = []
    row_no = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_TABLE_HEAD_PREFIX):
            body = line[len(_TABLE_HEAD_PREFIX):]
            if body.startswith(_SHEET_TAG):
                sheet, cols, row_no = body, [], 0
            else:
                cols = [c.strip() for c in body.split(_COLUMN_SEP) if c.strip()]
                row_no = 0
            continue
        row_no += 1
        if len(line) < min_chars:
            continue
        # 首列只认「这一行确实以第 0 列开头」：`flatten_table` 会跳过空格子，
        # 首列空行的第一个段其实是第二列，硬当首列值就把身份安错了人——那种行退行号。
        pair = line.split(_COLUMN_SEP, 1)[0]
        identity = pair if cols and pair.startswith(cols[0] + "：") else f"行{row_no}"
        chunks.append({"chunk_key": prefix + hashlib.md5(line.encode("utf-8")).hexdigest()[:16],
                       "source": source,
                       "heading": " / ".join(v for v in (sheet, identity) if v),
                       "content": line})
    return chunks


def find_glued_headings(chunks: list[dict]) -> dict:
    """确定性缺陷普查（埋点，零上游成本）：有多少条文正文里粘着章/节/编标题行。

    这是 SOP 里「探针发现的形状回写成代码判据」那一格：C1 的 LLM 判定负责发现形状，
    本函数负责此后每次都用同一把尺量它，不花钱、可重复、每次批次留痕。
    只看条文首行之后的行——首行按构造就是条文行首（`article_number` 已筛过）。
    """
    affected = []
    for chunk in chunks:
        lines = [ln for ln in (chunk.get("content") or "").splitlines() if ln.strip()]
        glued = [ln.strip() for ln in lines[1:] if section_heading(ln)]
        if glued:
            affected.append({"article_no": article_number(lines[0]) if lines else None,
                             "chunk_key": chunk.get("chunk_key", ""),
                             "headings": glued})
    total = len(chunks)
    return {
        "chunks": total,
        "affected": len(affected),
        "rate": round(len(affected) / total, 4) if total else 0.0,
        "first_article": affected[0]["article_no"] if affected else None,
        "articles": [a["article_no"] for a in affected],
        "detail": affected,
    }


def extract_article_numbers(lines: Iterable[str]) -> list[int]:
    """从若干行文本抽行首条号，返回去重升序列表；非条文行忽略。"""
    numbers = {article_number(line) for line in lines}
    return sorted(n for n in numbers if n)


def find_missing_numbers(numbers: Iterable[int], *, first: int = 1,
                         last: int | None = None) -> list[int]:
    """返回 [first, last] 区间内缺失的条号；last 缺省取观测到的最大条号。

    区间上限必须显式可传：只按观测最大值算，尾部整段缺失会被当成"本来就没有"，
    而 861 事故缺的正是尾部（第 1040 条之后）。
    """
    seen = {n for n in numbers if n}
    if not seen and last is None:
        return []
    upper = last if last is not None else max(seen) if seen else first - 1
    if upper < first:
        return []
    return [n for n in range(first, upper + 1) if n not in seen]


def _iter_head_lines(content: str) -> list[str]:
    """取条目内容的首行做条号判定；正文里的"第X条"是引用，不算本条编号。"""
    text = (content or "").strip()
    return [text.split("\n", 1)[0]] if text else []


def check_candidates(chunks: list[dict], *, expected_total: int | None = None,
                     first: int = 1, last: int | None = None) -> dict:
    """阶段2 出口闸（不碰库）：条数、重复键、条号连续性三项，返回 passed 与逐条原因。

    chunks 每项需含 chunk_key 与 content；expected_total 是候选条数口径（如民法典 1260）。
    断档检测只在显式传 last（条号区间上限）时才做：拿观测最大值当上限等于默认
    「后面本来就没有」，而 861 事故缺的正是尾部整段——上限没有出处时宁可只报条数差。
    """
    keys = [c.get("chunk_key", "") for c in chunks]
    dup_keys = sorted({k for k in keys if k and keys.count(k) > 1})
    article_lines = [ln for c in chunks if len(c.get("content", "")) >= _MIN_ARTICLE_CHARS
                     for ln in _iter_head_lines(c.get("content", ""))]
    numbers = extract_article_numbers(article_lines)
    missing = find_missing_numbers(numbers, first=first, last=last) if last is not None else []
    reasons = []
    if expected_total is not None and len(chunks) != expected_total:
        reasons.append(f"候选条数 {len(chunks)} != 应有 {expected_total}")
    if dup_keys:
        reasons.append(f"重复 chunk_key {len(dup_keys)} 个")
    if missing:
        reasons.append(f"条号断档 {len(missing)} 个，首个缺 {missing[0]}")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "count": len(chunks),
        "expected_total": expected_total,
        "numbers_found": len(numbers),
        "missing_numbers": missing,
        "duplicate_keys": dup_keys,
    }


async def assert_stored_count(conn, *, source: str, expected: int) -> dict:
    """落库后对账：库内该 source 条数必须等于候选条数，不等即抛 CoverageMismatch。

    `source` 只作绑定参数进 $1，不拼 SQL 字符串（本仓 SQL 参数化红线）。
    与 check_candidates 配对使用：前者管内存里的候选，后者管库里的实际，
    中间那段 INSERT / ON CONFLICT 少写或多写都只在这里才暴露。
    """
    actual = await conn.fetchval(
        "SELECT count(*) FROM knowledge_chunks WHERE source = $1", source)
    actual_int = int(actual or 0)
    if actual_int != expected:
        raise CoverageMismatch(stage="stored_count", expected=expected,
                               actual=actual_int,
                               detail=f"source={source} 落库条数与候选不一致")
    return {"source": source, "expected": expected, "actual": actual_int}
