"""页面噪声与空白的行级判据：形状只认在这里，别处一律 import（防第二把尺）。

作用边界（重要，别当成"什么都洗得动"）：本文件只处理 `read_lines`／`docx_line_stream` 给出的
**正文流**（docx 那条在 `with_tables=True` 时已含表格折行，10-14 续六）。
页眉/页脚/页码在 section 的独立 XML part，**不在这条流上**，所以对 docx 正文流"页码趟"注定删 0 行
——那是结构使然，不是漏洗（`agent_docs/Bug日志.md` 第 60 条）。
10-15 起这些部件由 `type_layer.page_part_lines` **读出来对账**（判噪声用本文件的 `noise_kind`，
不另写形状），但**不灌进这条流、也不进语料**：真语料实测页脚里只有「－1－」这种页码，
灌进来就是往条文末尾塞页码。部件里读出非噪声行时，调用方必须显式告警而不是静默丢失。
展示层（CLI／面板）要把"这趟为什么是空的"显示出来，不许显示成"已洗干净"。
"""
import re

from app.services.kbclean.coverage import article_number, section_heading
# 结构行判据取自类型层同一把尺（type_layer 顶层不 import 本模块，无环）
from app.services.kbclean.type_layer import is_table_line

# 只有整行是这个形状才算页面噪声，正文里含汉字的行不会被误删。
# 装饰字符类必须含全角减号 U+FF0D 与全角下划线：真语料的页码就是「－1－」这种写法（10-13 实测）。
_NOISE_CHAR = r"\s\-–—―─━－_＿~·.。・*＊="
_PAGE_NUM = re.compile(rf"^[{_NOISE_CHAR}]*\d+[{_NOISE_CHAR}]*$")
_PAGE_MARK = re.compile(rf"^(第|全|共)?\s*\d+\s*页(\s*[/／共]\s*\d+\s*页)?[{_NOISE_CHAR}]*$")
_RULE_ONLY = re.compile(rf"^[{_NOISE_CHAR}]+$")
# 上一行以这些字符收尾说明语义已断完，不该再并下一行
_SENT_END = "。！？；：…」』）】"
# 零宽与软连字符：肉眼看不见，但会污染向量嵌入与词面检索
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def noise_kind(text: str) -> str:
    """返回噪声类别；不是噪声返回空串。

    这是本仓**唯一**的"页面噪声行"判据：正文流里的页码走它，docx 页眉页脚部件里的页码也走它
    （`type_layer.page_part_lines`），别处不许再写一份字符类——两处各写会漂成"正文里的页码洗掉了、
    部件里的页码当正文收进"。
    """
    if not text:
        return ""
    if _PAGE_MARK.match(text):
        return "页码标记"
    if _PAGE_NUM.match(text):
        return "纯数字行"
    if _RULE_ONLY.match(text):
        return "分隔线"
    return ""


def starts_entry(text: str) -> bool:
    """这一行是不是一个新条文的开头或一个标题（走闸包同一把尺，不另写字符类）。"""
    return bool(article_number(text) or section_heading(text))


def strip_page_noise(lines: list[str]) -> dict:
    """趟 A：删整行的页面噪声（页码、纯数字、分隔线）。只删行，不改任何一行的内容。"""
    kept: list[str] = []
    removed: list[dict] = []
    for idx, raw in enumerate(lines, start=1):
        kind = noise_kind(raw.strip())
        if kind:
            removed.append({"line_no": idx, "text": raw.strip()[:40], "rule": kind})
            continue
        kept.append(raw)
    return {"kept": kept, "removed": removed}


def tidy_whitespace(lines: list[str]) -> dict:
    """趟 B 之一：连续空行压成一行 + PDF 式软换行并入上一行。

    合并要同时满足三件：上一行不是条文头也不是标题、当前行同样不是、且上一行末尾语义未断完。
    少一件都不并——docx 每行本就是一个段落，误并把两个段落接起来，等于凭空造出一条长条文。
    """
    kept: list[str] = []
    dropped: list[dict] = []
    merged: list[dict] = []
    prev_entry = False
    prev_blank = False
    for idx, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            if prev_blank:
                dropped.append({"line_no": idx, "text": "", "rule": "连续空行"})
                continue
            prev_blank = True
            prev_entry = False
            kept.append("")
            continue
        prev_blank = False
        # 结构行（表格折行）与条文头同等对待：表头行末不是句末标点，不认它就会被并进正文，
        # 每行自带列名的折叠形态就此粘成一坨（判据在 `type_layer.is_table_line`，不在这里重写形状）
        entry = starts_entry(text) or is_table_line(text)
        head = kept[-1].strip() if kept else ""
        if head and not entry and not prev_entry and head[-1] not in _SENT_END:
            merged.append({"line_no": idx, "text": text[:40], "into": head[:24]})
            kept[-1] = head + text
            continue
        kept.append(raw)
        prev_entry = entry
    return {"kept": kept, "removed": dropped, "merged": merged}


def unify_chars(lines: list[str]) -> tuple[list[str], int]:
    """趟 B 之二：去零宽字符、全角数字转半角。**不做**全角空格转半角（10-14 业主选 C 档定案）。

    为什么留着全角空格：真语料里它是条文行首缩进，几乎每行都有，转掉它 `chunk_key`（内容 md5）
    会全库换面，而 `chunk_key` 是历史请求回查块内容的唯一凭据（`rag_request_content.py` 查不到就静默跳过），
    换来换去只换来排版差异。零宽字符与全角数字不同——它们肉眼不可见或形状错位，会实打实污染向量嵌入与词面检索。
    这一手是**判据**、不是开关：任何调用方跑出来的行序列必须一样，别在这里加参数造第二把尺。
    """
    out: list[str] = []
    changed = 0
    for raw in lines:
        text = raw.translate(_INVISIBLE).translate(_FULLWIDTH_DIGITS)
        if text != raw:
            changed += 1
        out.append(text)
    return out, changed


# 趟序是判据的一部分：噪声趟必须排在空白趟之前，否则整行页码被并进上一条正文就再也认不出来。
PASS_ORDER = ("noise", "ws")


def run_pass(kind: str, lines: list[str]) -> dict:
    """跑单趟的原始计数，不带任何展示字段：CLI、面板与建库脚本都从这里取数，避免各写一套调用顺序。"""
    if kind == "noise":
        res = strip_page_noise(lines)
        return {"kept": res["kept"], "removed": res["removed"], "merged": [], "chars_changed": 0}
    if kind == "ws":
        unified, chars_changed = unify_chars(lines)
        res = tidy_whitespace(unified)
        return {"kept": res["kept"], "removed": res["removed"], "merged": res["merged"],
                "chars_changed": chars_changed}
    raise ValueError(f"未知清洗趟：{kind}")


def clean_lines(lines: list[str]) -> tuple[list[str], list[dict]]:
    """按 PASS_ORDER 洗完一份行流，返回 (清洗后行, 逐趟计数)。

    只数不判：对账文案与「结构性空趟」那类注记留在展示层，
    本函数保证的是**任何调用方跑出来的行序列一样**——建库脚本直接用它，就再造不出第二把尺。
    """
    recons: list[dict] = []
    current = lines
    for kind in PASS_ORDER:
        res = run_pass(kind, current)
        recons.append({"pass": kind, "input_lines": len(current), "output_lines": len(res["kept"]),
                       "removed_lines": len(res["removed"]), "merged_lines": len(res["merged"]),
                       "chars_changed": res["chars_changed"]})
        current = res["kept"]
    return current, recons
