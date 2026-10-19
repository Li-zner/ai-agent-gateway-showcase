"""清洗阶梯第 2 步：按文档类型的结构层（SOP-06「清洗四步阶梯」）。

第 1 步那套趟只认「行」，看不见文档对象的内部结构：docx 的表格文字存在独立的 `tables`
集合里、页码在 section 的页眉页脚部件里，正文段落流一条都读不到（本仓 Bug日志 第 60 条）。
本模块把**正文流之外的结构件取出来**折算成行，或者给出「这一趟为什么注定空」的原因。

阶梯的分工到这儿为止：第 3 步（成条）之后不再认文档类型，所有类型差异必须在这里吸收完。
"""
import asyncio
import re
from functools import partial

# 本 CLI 能直接读正文流的类型；PDF 与图片要经过现链 parser（含 OCR），此处只出路由判定
TEXT_EXTS = (".docx", ".txt", ".md")
# 10-18 业主定的清洗范围补齐的两族：这两族都不是"字节流即正文"，得先把结构解成行
XLSX_EXTS = (".xlsx",)
JSON_EXTS = (".json",)
# 要先解结构才成行的两族（腿在 `struct_legs.xlsx_line_stream`／`json_line_stream`，不是按字节解码）
STRUCT_EXTS = XLSX_EXTS + JSON_EXTS
# 普查与路由认的"在清洗范围内"＝能直读的 + 能解结构的（csv 由业主点名出局）
WASHABLE_EXTS = TEXT_EXTS + STRUCT_EXTS
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
# 表格行占「正文行 + 表格行」的比例超过这个值，就认为版面复杂到「行首判据不可靠」，
# 交模型兜底那一档。取 0.5 的理由：结构行里过半来自表格时，正文段落已是少数派，
# 按条号切分会把整张表切成碎条。
TABLE_DENSE_RATIO = 0.5

# 折叠形态的两个字面量：`flatten_table` 用它产出行，`is_table_line` 用它认行，必须同一处定义。
# 分成两份字面量就是第二把尺，改一边另一边不动会得到"折得出来、认不出来"。
_COLUMN_SEP = "｜"
_TABLE_HEAD_PREFIX = "表格表头："
# 多 sheet 的 xlsx 在每张表前另出一行 `表格表头：工作表：<表名>`，让行流里能回到原表。
# 造这行的是 `struct_legs.xlsx_line_stream`、认它的是 `coverage.split_table_rows`——
# 两边各写一份「工作表：」就是第二把尺，改一边会得到"造得出、认不出"（同 `_TABLE_HEAD_PREFIX` 的理由）。
_SHEET_TAG = "工作表："

# 需要现链 parser 才有文本的类型：PDF 有文本层时也统一走这条趟（由置信度字段区分是不是 OCR 出来的）
OCR_EXTS = (".pdf",) + IMAGE_EXTS
# 现链 parser 在 PDF 每页前加的页码标记，与"内容截断"的两处标注（清洗侧不许把截断当洗干净）
_PAGE_MARK_RE = re.compile(r"^-{3}\s*第\s*\d+\s*页\s*-{3}$")
# 现链 OCR 的仪器外壳：`app/agents/ocr_leg._ocr_image` 给每页取回的文打一行首前缀
# `[图片 OCR (第N页[-图M)]: 正文`。这串是仪器读数不是正文，更要命的是一贴就在行首，
# 行级条号状态机从此看不见「第X条」的行首形状 ⇒ 整份判 no_article（Bug日志 67）。
_OCR_WRAP_RE = re.compile(r"^\s*\[图片 OCR \(第(\d+)页(?:-图\d+)?\)\]?: ?")
_TRUNC_MARKS = ("[文档过长已截断", "[文档过大已截断")

# 版心外重复行（规章页眉／机关署名／政报页脚这一族）的判据，Bug日志 68＋69 共用这一把尺。
# 四个数都是 10-16 在 5 份真公开件上量出来的，不是拍的：
#   带 2 行 —— 文本层规章的页眉实测就在每页第 1、2 行（生态环境部规章／生态环境部发布）；
#   ≥2 页 —— 单页出现的不算重复（首页红头只有一页，不该被删）；
#   4~24 字 —— 下界挡住上海那份规章里「年」「月」「日」三个单字行（跨页重复但是正文碎片），
#              上界挡住整句正文偶然同形的情况。
_EDGE_BAND = 2
_EDGE_MIN_PAGES = 2
_EDGE_MIN_CHARS = 4
_EDGE_MAX_CHARS = 24

# OCR 置信度现在长这样：每段 OCR 文本末尾一行尾注（现链 `app/agents/ocr_leg.py` 的 `_conf_note` 的产出形状，
# 2026-10-15 从 document_parser 拆出）。
# 第 2 步的要求是「置信度提成列」，所以先把尾注从正文里剥出来变成字段，而不是让它混进语料。
_CONF_NOTE_RE = re.compile(r"\n?\(OCR 置信度: (0?\.\d+|1\.0+|\d+(?:\.\d+)?)\)"
                           r"(?:\n?\(注意：置信度较低[^)]*\))?")

# 「这份要回原文人工核」的唯一门槛——放在闸包而不是调用方，是为了不让它长成第二把尺。
# 数值来自 10-15 带真值实测（Tesseract 降级腿）：样本量三批累计 34 张首份指标
# ＋ `--psm` 对拍 204 次 ＋ deskew 三臂 102 次；分档单调 —— <0.70 平均 CER 0.6929、
# 0.70~0.85 0.1528、0.85~0.95 0.0220；门槛以下基本没有可用输出，所以拿它当复核触发线。
# 此前各处凭直觉拍的 0.6 作废：10-15 起现网 `ocr_leg._conf_note`（同日从 document_parser 拆出）
# 的警示线也 import
# 本常量，清洗侧与用户侧共用一把尺（deskew 接上后歪页 conf 由 0.66/0.75 抬到 0.87，
# 即"该拦的照样拦，被修好的不再白拦"）。
OCR_REVIEW_CONF = 0.85

# 第二档线只决定**文案强度**，不决定拦不拦：清洗侧人工复核触发线仍然只有上面那一条。
# 0.70 同样不是直觉值——它与 `OCR_REVIEW_CONF` 出自同一批 204 次对拍的分档：
# <0.70 平均 CER **0.6929**（整段基本读不成句）、0.70~0.85 **0.1528**（零星错字为主）。
# 两档文案都必须以 `(注意：置信度较低` 开头：上面 `_CONF_NOTE_RE` 就是靠这个**前缀**把警示句
# 连同数字一起从正文里剥掉的（括号内随便改，前缀一改警示句就会漏进语料——10-15 量尺同族第三次坑）。
OCR_UNREADABLE_CONF = 0.70


def flatten_table(rows: list[list[str]]) -> list[str]:
    """一张表折成「带表头上下文」的行——表头行单独一行，数据行每行都带上列名。

    为什么不是 `" | ".join(cells)`：那样出来的行没有列名，进了语料就是「张三 ｜ 28」这种
    无上下文碎片，检索侧既读不懂也回不到原表（SOP-06 阶梯第 2 步明确禁止逐格散进正文）。
    一行仍只出一行，不并表、不拆列。
    """
    rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in rows]
    if not rows:
        return []
    # 表头必须先固定成首行、再过滤其余空行：反过来的话，首行为空的表（合并格、分隔空行常见）
    # 会把第一条数据行当成表头，整张表的列名就此错位。
    header, body = rows[0], [r for r in rows[1:] if any(r)]
    named = any(header)
    lines = [_TABLE_HEAD_PREFIX + _COLUMN_SEP.join(h for h in header if h)] if named else []
    for row in body:
        pairs = []
        for i, cell in enumerate(row):
            if not cell:
                continue
            name = header[i] if i < len(header) and header[i] else f"列{i + 1}"
            pairs.append(f"{name}：{cell}")
        if pairs:
            lines.append(_COLUMN_SEP.join(pairs))
    return lines


def is_table_line(text: str) -> bool:
    """这一行是不是 `flatten_table` 折出来的表格行（只认折出来的形状，不回看文档类型）。

    存在的理由是一件具体的事：表头行以「额」这类非句末字符收尾，空白趟的软换行判据会把紧跟其后的
    第一条数据行并进它，折出「表格表头：项目｜金额项目：违约金」这种粘连串——每行自带列名的折叠
    形态就此废掉一半。所以结构行要和条文头同等对待：谁都不并谁。
    """
    s = (text or "").strip()
    return s.startswith(_TABLE_HEAD_PREFIX) or _COLUMN_SEP in s


def docx_table_rows(path: str) -> list[list[list[str]]]:
    """读 docx 里每张表的原始单元格（只读，坏文件返回空表而不是抛错）。"""
    try:
        from docx import Document
        from docx.table import Table
        from docx.oxml.ns import qn
        doc = Document(path)
        out = []
        for tbl in (child for child in doc.element.body.iterchildren()
                    if child.tag == qn("w:tbl")):
            out.append([[cell.text for cell in row.cells] for row in Table(tbl, doc).rows])
        return out
    except Exception:  # noqa: silent-except 豁免：类型层探测件，坏了要让调用方按"无表"走，不能红整批
        return []


def docx_line_stream(path: str, *, with_tables: bool = False) -> list[str]:
    """按**文档顺序**把段落与表格折成一条行流。

    `Document().paragraphs` 只给段落，表格会被整张丢掉且不留痕迹；这里遍历 body 子元素，
    段落照旧、表格走 `flatten_table`。`with_tables=False` 时输出与只读段落逐字节相同——
    既有批次基线（普查的 272-2127-4 那几个数）不能因为新加一趟就悄悄移动。
    """
    from docx import Document
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn
    doc = Document(path)
    lines: list[str] = []
    tables = docx_table_rows(path) if with_tables else []
    tbl_index = 0
    for child in doc.element.body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            lines.append(Paragraph(child, doc).text)
        elif tag == qn("w:tbl") and with_tables:
            rows = tables[tbl_index] if tbl_index < len(tables) else []
            tbl_index += 1
            lines.extend(flatten_table(rows))
    return lines


def image_count(path: str) -> int:
    """docx 内嵌图数量（只数 inline 形状）；非 docx 返回 0，由 profile 用扩展名另判。"""
    if not path.lower().endswith(".docx"):
        return 0
    try:
        from docx import Document
        return len(Document(path).inline_shapes)
    except Exception:  # noqa: silent-except 豁免：同 docx_table_rows，装饰性探测不阻断整批
        return 0


# 表格只数前 N 页：第 0 阶实测 21 份真件限 30 页共 8.4 秒（10-16），全页扫长件会把秒级普查拖成分钟级。
# ponytail: 天花板 = 表格只出现在第 30 页之后的 PDF 会读成 0 张表。升级路径 = 与第 70 条的确定性预算入口一起做全页扫描。
PDF_TABLE_SCAN_PAGES = 30


def _pdf_stats_from_doc(doc) -> dict:
    """对**已打开**的 PDF 文档对象取版面读数。拆成纯函数是为了本机没有 fitz 也能测（喂假件即可）。"""
    pages = list(doc)
    tables = []
    for page in pages[:PDF_TABLE_SCAN_PAGES]:
        tables.extend(page.find_tables().tables or [])
    return {"page_count": len(pages),
            "text_chars": sum(len((page.get_text() or "").strip()) for page in pages),
            "images": sum(len(page.get_images(full=True)) for page in pages),
            "table_count": len(tables),
            "table_rows": sum(table.row_count for table in tables)}


def pdf_struct_stats(path: str) -> dict | None:
    """PDF 的版面读数：页数、文本层字数、内嵌图数、表格数与表格行数。

    存在的理由是第 0 阶量出的假零（Bug日志 71）：画像原先只对 docx 读表格与图，PDF 那两列一律报 0，
    读起来是"这份没表"，实际是"这份没读"。**读不出就返回 None**（引擎没装、文件损坏、fitz 换 API），
    由调用方把键整块撤掉——与清洗侧 OCR 四键「没跑过的趟不许有读数」同一口径。
    """
    try:
        import fitz
        doc = fitz.open(path)
        try:
            return _pdf_stats_from_doc(doc)
        finally:
            doc.close()
    except Exception:  # noqa: silent-except 豁免：引擎缺失与文件损坏都归"这份没读"，由 None 说话，不许红整批普查
        return None


# section 上的六套部件：默认／首页／偶页 各有页眉与页脚（python-docx 0.8.8+ 才把这层暴露成对象）
_PAGE_PARTS = ("header", "footer", "first_page_header", "first_page_footer",
               "even_page_header", "even_page_footer")


def page_part_lines(path: str) -> dict:
    """读出 docx 页眉／页脚部件里的行，按**同一把噪声尺**分类，只出对账读数。

    为什么"读而不灌"（Bug日志 60 剩余半格的修法方向）：真语料实测页脚里只有「－1－」这种页码
    （`.scratch/probe_header_footer.txt`），把它接进正文流＝往条文末尾塞页码，清洗趟再把它删掉
    只是原地绕圈。所以这条腿要买的是**把"看不见"变成"有账"**：部件行进分母、页码类按噪声计数、
    剩下的**非页码行显式告警**——那才是"正文写在页眉里"的真丢失形状，原先整条链一个字都报不出。

    诚实边界两条，别引用成"页眉页脚已经全读进来了"：
    ① 域码页码（PAGE field）在部分文档里 `paragraph.text` 取不到，`part_lines` 会偏小；
    ② 部件里的**表格只计数不读入**（`part_tables`），要读得先定"页眉表算谁的条文"这条判据。
    判噪声走 `noise.noise_kind`，本函数**不另写字符类**；`noise` 顶层 import 本模块的
    `is_table_line`，所以这里**懒载**——反向顶层 import 会成环。
    非 docx 一律 `read=False` 且不带读数：没读过的格式不许有部件计数（与表格那条纪律同一口径）。
    """
    if not path.lower().endswith(".docx"):
        return {"read": False, "part_parts": 0, "part_lines": 0, "part_noise": 0,
                "part_substantive": 0, "part_tables": 0, "reason": "非 docx，无 section 部件可读"}
    from app.services.kbclean.noise import noise_kind
    try:
        from docx import Document
        doc = Document(path)
    except Exception as exc:  # noqa: silent-except 豁免：读坏文件要让调用方按"没读到"报，不能红整批
        return {"read": False, "part_parts": 0, "part_lines": 0, "part_noise": 0,
                "part_substantive": 0, "part_tables": 0,
                "reason": f"部件读取失败：{type(exc).__name__}"}
    noise_lines: list[str] = []
    substantive: list[str] = []
    parts_hit = 0
    tables = 0
    for section in doc.sections:
        for kind in _PAGE_PARTS:
            part = getattr(section, kind, None)
            if part is None:
                continue
            tables += len(getattr(part, "tables", None) or [])
            texts = [text.strip() for para in part.paragraphs if (text := para.text.strip())]
            if not texts:
                continue
            parts_hit += 1
            for text in texts:
                (noise_lines if noise_kind(text) else substantive).append(text)
    total = len(noise_lines) + len(substantive)
    return {"read": True, "part_parts": parts_hit, "part_lines": total,
            "part_noise": len(noise_lines), "part_substantive": len(substantive),
            "part_tables": tables,
            "noise_samples": noise_lines[:3], "substantive_samples": substantive[:3],
            "reason": (f"部件 {parts_hit} 处／{total} 行：页码类 {len(noise_lines)} 行按噪声处理，"
                       f"非页码 {len(substantive)} 行未入语料"
                       + (f"，另有部件表格 {tables} 张只计数未读入" if tables else ""))}


def profile_of(path: str, lines: list[str]) -> dict:
    """采集版面画像：扩展名、正文行数、表格数与行数、图片数。纯计数，零上游调用。

    路由必须读这些**版面特征**，不能只读扩展名——只按扩展名分两路在 SOP-06 里明确不算
    做过第 2 步（同一份 docx，纯文字的和塞了三十张表的，该走的趟不一样）。
    docx 的正文行数自己重算段落流：`lines` 在 `--with-tables` 下已含表格折行，
    再与 `table_rows` 一起进分母会重复计，同一份文档的档位会随开关漂移。

    10-16 第 0 阶补的口径（Bug日志 71）：结构读数只在**真读过**时才存在——docx 走 python-docx，
    PDF 走 `pdf_struct_stats`（fitz），其余格式（txt/md/单张图片）没有这层结构可言，
    三个键整块缺席、`struct_read` 为假。以前对 PDF 硬填 0，报表就把"没读"显示成"没有"。
    10-18 补 xlsx/json：这两族的结构就是**折出来的那些行**（表头行＋带键名的数据行），所以从 `lines`
    里数，不再二次开文件；txt/md 那支的"整块缺席"口径不变。
    **`decide_route` 的档位判据一字未动**：PDF 在按扩展名那一支就先返回，
    表格密度档要不要为它打开属判据变更，得单独点单。
    """
    ext = "." + path.lower().rsplit(".", 1)[-1] if "." in path else ""
    tables = docx_table_rows(path) if ext == ".docx" else []
    source_lines = docx_line_stream(path) if ext == ".docx" else lines
    nonempty = sum(1 for line in source_lines if line.strip())
    profile = {"ext": ext,
               "nonempty_lines": nonempty,
               # OCR 型只有真取到文才算"这趟跑过"；取不到文时是 0，路由继续报"未接/没洗"而不是空成功
               "ocr_lines": nonempty if ext in OCR_EXTS else 0}
    if ext == ".docx":
        profile.update(struct_read=True, table_count=len(tables),
                       table_rows=sum(len(t) for t in tables), images=image_count(path))
    elif ext == ".pdf":
        stats = pdf_struct_stats(path)
        if stats:
            profile.update(struct_read=True, **stats)
    elif ext in STRUCT_EXTS:
        # 这两族折出来的行**就是**全部结构，所以不再二次开文件（xlsx 再 iter_rows 一遍是线性翻倍）：
        # 表头行由 `_TABLE_HEAD_PREFIX` 认，结构行由 `is_table_line` 认，都是折行那侧现成的尺。
        folded = [line for line in lines if line.strip()]
        profile.update(struct_read=True, images=0,
                       text_chars=sum(len(line) for line in folded),
                       table_count=sum(1 for line in folded if line.startswith(_TABLE_HEAD_PREFIX)),
                       table_rows=sum(1 for line in folded if is_table_line(line)))
    return profile


def decide_route(profile: dict) -> dict:
    """按版面画像决定这一份文档该走哪条趟，返回 `{"route": ..., "reason": ...}`。

    四档：`rule` 行首判据可靠 / `ocr` 没有文本层，先解析再谈清洗 /
    `llm` 表格密度高，规则判据不可靠、交模型兜底（仍是先报后删）/ `reject` 类型不认。
    `reason` 必填：报表与面板要能把「为什么这趟是空的」显示出来，不许显示成"已洗干净"。
    """
    ext = profile.get("ext", "")
    taken = profile.get("ocr_lines", 0)
    err = profile.get("ocr_error")
    if err:
        # 跑过 OCR 但引擎报错：不能退回「未接」那句，那会把故障说成没开工，也不能说成取到了文
        return {"route": "ocr", "reason": f"OCR 趟已跑但取件失败：{err}"}
    if taken:
        # 已经走过 OCR 趟取到文：档位仍是 ocr（取件方式不变），但原因必须改口，
        # 否则报表会把「取过文、置信度 0.83」显示成「未接、不洗」
        conf = profile.get("ocr_conf")
        return {"route": "ocr", "reason": f"OCR 趟已取文 {taken} 行，最低置信度 {conf}"}
    if ext in IMAGE_EXTS:
        return {"route": "ocr", "reason": f"{ext} 无文本层，需先 OCR"}
    if ext == ".pdf":
        # 开关关 ≠ 没接：这条趟已接现链 parser，只是本次没开 --with-ocr 所以没取文，如实说
        return {"route": "ocr", "reason": "OCR 趟已接现链 parser，本次未开 --with-ocr ⇒ 未取文、本趟不洗"}
    if ext not in WASHABLE_EXTS:
        return {"route": "reject", "reason": f"扩展名 {ext or '(无)'} 不在清洗范围"}
    if ext in XLSX_EXTS:
        # 表格密度那支的尺（table_rows/结构行）对 xlsx 恒等于 1，绕不过判据就直接说清：
        # 整份都是表，没有正文段落流，行首「第X条」这种判据在它身上不成立
        return {"route": "llm", "reason": "xlsx 全部行都来自表格，行首判据不可靠 ⇒ 第 2 阶表格趟的目标"}
    if ext in JSON_EXTS:
        return {"route": "rule", "reason": "json 已折成带键名的行，无版面结构件"}
    nonempty = profile.get("nonempty_lines", 0)
    table_rows = profile.get("table_rows", 0)
    structure_lines = nonempty + table_rows
    if structure_lines and table_rows / structure_lines > TABLE_DENSE_RATIO:
        return {"route": "llm", "reason": f"表格行占比高于 {TABLE_DENSE_RATIO}，行首判据不可靠"}
    if ext == ".docx" and profile.get("images", 0):
        return {"route": "rule", "reason": "含内嵌图但正文有文本层，图另走 OCR，正文不挡规则趟"}
    return {"route": "rule", "reason": "正文流即可覆盖，无结构件"}


# 分堆名（SOP-06 阶梯"分堆"第 1 阶；判据来自 10-16 第 0 阶 22 份真件实测，不是拍脑袋的类目）
BUCKETS = ("scanned", "layered", "image", "table", "textstream", "docx", "struct_unread")


def bucket_of(profile: dict) -> list[str]:
    """版面画像 → 这份件属于哪些堆。**只分堆、不清洗，也不改 `decide_route` 的档位。**

    堆之间**故意允许交叉**：表格与内嵌图是同一份件的两个属性（真件里 `sx` 既每页一图又有 10 页表），
    硬做成互斥就会有两份件被塞进错的堆。返回清单，调用方按清单逐堆记账。
    `route` 四档不能当分堆键——第 0 阶实测 21 份 PDF 全落 `ocr`，按它分等于没分。
    ponytail: 天花板 = 每页塞两张图的扫描型双层件会被判进 `image` 而不是 `layered`（判据是张数==页数）；
    升级路径 = 改用图片面积占版面比，代价是要读每页的版面盒。
    """
    ext = profile.get("ext", "")
    if ext == ".docx":
        # 表格是与格式**交叉**的一维：只塞进 `docx` 堆，第 2 阶的表格趟就永远看不见带表的 docx
        return ["docx"] + (["table"] if profile.get("table_count") else [])
    if ext in TEXT_EXTS:
        # txt/md 的格式本身没有内嵌图与表格可言（markdown 管道符表格不属这层尺，属第 2 阶之后的事）
        return ["textstream"]
    if ext in XLSX_EXTS:
        # 整份就是表：进 table 堆，让第 2 阶的表格趟点名得到它（后面那套 text_chars/images 判据是 PDF 的）
        return ["table"]
    if ext in JSON_EXTS:
        return ["textstream"]
    if not profile.get("struct_read"):
        # 该读结构却没读到的才叫 struct_unread：单张图片、以及 fitz 缺失或文件读不动的 PDF
        return ["struct_unread"]
    images = profile.get("images", 0)
    buckets: list[str] = []
    if not profile.get("text_chars"):
        # 没有文本层＝整页都是图，那份图就是正文本身，已由 OCR 趟取走，不该再进 image 堆重复一次
        buckets.append("scanned")
    elif images and images == profile.get("page_count"):
        buckets.append("layered")
    elif images:
        buckets.append("image")
    if profile.get("table_count"):
        buckets.append("table")
    return buckets or ["textstream"]


def split_ocr_confidence(text: str) -> tuple[str, float | None]:
    """把 OCR 置信度从正文尾注提成字段，返回 (干净正文, **最低**置信度或 None)。

    置信度混在正文里进语料会有两个后果：向量嵌入被一串数字污染，以及低置信页没法单独路由
    回退口 A。现链只出尾注不出列（`ocr_leg._conf_note`），所以这一步在清洗侧补。
    一份文档可能有多段 OCR 文本各带一条尾注，所以剥**所有**注记、取**最低**值——
    半页糊的就把整份按糊判，取平均等于让糊页洗进干净页里。
    """
    confs = []
    for raw in _CONF_NOTE_RE.findall(text or ""):
        try:
            confs.append(float(raw))
        except ValueError:
            continue
    clean = _CONF_NOTE_RE.sub("", text or "").rstrip("\n")
    return clean, (min(confs) if confs else None)


def _parser_failure(raw: str) -> str | None:
    """识别「parser 把失败原因当成整份输出」这一族返回值，是则原样返回、否则 None。

    现链有 12 处这样的返回（`[OCR 识别失败: …]`／`[PDF 解析引擎未安装]`／`[无法解码文件内容]`…），
    形状统一是"整份输出就是一对括号"。它们不是文档内容：当成正文进语料，就是把仪器故障读成条文。
    ponytail: 天花板 = 真文档整体被方括号包住且 ≤300 字（会误判成失败）。
    升级路径 = parser 出结构化错误码，清洗侧按码判。
    """
    s = (raw or "").strip()
    return s if s.startswith("[") and s.endswith("]") and len(s) <= 300 else None


def _group_by_page(clean: str) -> tuple[list[list[str]], int]:
    """按现链的两种页边界形状把取件输出切成「一页一组」，仪器读数一律不进组。

    为什么要分组：版心外重复行这把尺的判据是"同一行在多页的页边出现"，平铺行流里页边界
    已经没了（摘壳＋数完页码就丢），下游再想复原只能靠猜。分组只活在这条腿里，
    交给调用方的仍是平铺流——第 3 步成条与闸门的输入形状不变
    （空行一律不进组，旧实现同样滤空行；除了被删的版心外重复行，行序列与旧实现逐字相同）。
    返回的第二个值是 `pages`，口径与旧实现逐字相同：**只数** `--- 第 N 页 ---`。
    OCR 趟的页码住在仪器外壳里，把它提成计数属判据变更（Bug日志 67 的结构性那条），未点单所以不动。
    """
    groups: list[list[str]] = []
    cur: list[str] = []
    pages = 0
    last_ocr_page: str | None = None
    for line in clean.splitlines():
        s = line.strip()
        if not s:
            continue
        if _PAGE_MARK_RE.match(s):
            pages += 1
            if cur:
                groups.append(cur)
                cur = []
            last_ocr_page = None
            continue
        wrap = _OCR_WRAP_RE.match(line)
        if wrap:
            # 同一页可能有多张内嵌图（第N页-图1／图2）：那是同一页，不该另起一组
            if wrap.group(1) != last_ocr_page and cur:
                groups.append(cur)
                cur = []
            last_ocr_page = wrap.group(1)
            line = _OCR_WRAP_RE.sub("", line)  # 先摘仪器外壳，条号才有行首形状（Bug日志 67）
            if not line.strip():
                continue
        if line.strip().startswith(_TRUNC_MARKS):
            # parser 的截断标注同属仪器读数：留在流里就是「把截断当成正文」，
            # 但它照样在 meta.truncated 里如实记着，报表看得见这份没读全
            continue
        cur.append(line)
    if cur:
        groups.append(cur)
    return groups, pages


def strip_edge_repeats(pages: list[list[str]]) -> tuple[list[str], dict]:
    """删掉「在每页版心外重复出现的同一行」——规章页眉／机关署名／政报页脚这一族（Bug日志 68＋69）。

    为什么排在摘壳之后、并进正文之前：粘连发生在第 3 步的空白趟（`noise.tidy_whitespace`
    把语义未断完的下一行并上来），页眉一旦在那一步被并进正文就再也认不出边界——与
    `noise.py` 里「噪声趟必须排在空白趟之前」是同一条纪律的两个层次。
    判据是**整行精确同形**（只忽略空白），所以扫描件上只能捞到一部分：OCR 每页把同一句页眉
    读成不同字时它不认（10-16 实测：文本层两份 13/13、7/7 页全中且零误报，52 页扫描政报
    23 组里只中 2 条）。要覆盖扫描件得再引一把模糊尺，那属判据变更、等点单。
    删掉的行原文与命中页数一律留账在 `edge_rules`，别只报一个数——误删要能查回来。
    """
    seen: dict[str, set[int]] = {}
    for idx, lines in enumerate(pages):
        band = dict.fromkeys(lines[:_EDGE_BAND] + lines[-_EDGE_BAND:])
        for line in band:
            key = "".join(line.split())
            if _EDGE_MIN_CHARS <= len(key) <= _EDGE_MAX_CHARS:
                seen.setdefault(key, set()).add(idx)
    hits = {key for key, idxs in seen.items() if len(idxs) >= _EDGE_MIN_PAGES}
    kept: list[str] = []
    rules: dict[str, int] = {}
    dropped = 0
    for lines in pages:
        edge_pos = set(range(min(_EDGE_BAND, len(lines))))
        edge_pos |= set(range(max(0, len(lines) - _EDGE_BAND), len(lines)))
        for pos, line in enumerate(lines):
            key = "".join(line.split())
            if pos in edge_pos and key in hits:
                rules[key] = len(seen[key])
                dropped += 1
                continue
            kept.append(line)
    return kept, {"edge_dropped": dropped,
                  "edge_rules": [{"text": k, "pages": v} for k, v in
                                 sorted(rules.items(), key=lambda kv: -kv[1])[:5]]}


def ocr_stream(path: str, *, runner=None, deadline_s: float | None = None,
               full_text: bool = False) -> tuple[list[str], dict]:
    """阶梯第 2 步的 OCR 趟：无文本层的类型交给现链 parser 取文，页码标记与置信度都不留在正文里。

    `runner` 是注入点。真实 parser 依赖 PyMuPDF 与 OCR 引擎，本机没装也能把这条趟的判据跑完
    （测试打桩走这里），真跑在容器里。懒载是有意的：清洗面板进程只准载判据，不许被 OCR
    依赖链拖起来。
    `deadline_s` 是这份件的取文 wall-clock 预算，默认 `None`＝**这一层不声明**，用 parser 自己的
    那个常量；在这里写死 60 就是造第二把尺（改了常量这里还在跑旧值）。离线清洗按批次显式给数，
    为的是 60 秒把整份件切成半份那件事（Bug日志 74 ①）。
    `full_text` 是第二把刀的刀柄：parser 的输出默认按进 prompt 的 4 万字额度裁剪，离线清洗要的
    是全文（10-18 实测东莞那份 46431 字被切在第 32 页，真值 137 条只剩 104 条）。默认 False＝现网口径。
    """
    if runner is None:
        from app.agents import document_parser as dp  # 懒载，理由见 docstring
        kwargs = {}
        if deadline_s is not None:
            kwargs["deadline_s"] = deadline_s
        if path.lower().endswith(".pdf"):
            # 字数裁剪只在这一腿存在（图片 OCR 单张出不了 4 万字），所以 full_text 不往 parse_image 带
            if full_text:
                kwargs["full_text"] = True
            runner = partial(dp.parse_pdf, **kwargs) if kwargs else dp.parse_pdf
        else:
            runner = dp.parse_image
    raw = asyncio.run(runner(path))
    fail = _parser_failure(raw)
    if fail:
        # 取件失败 ≠ 这份文档没内容：出空流 + 原因，普查据此报"没读进来"，而不是把故障字符串洗成条文
        return [], {"pages": 0, "conf": None, "truncated": False, "engine_error": fail}
    clean, conf = split_ocr_confidence(raw)
    groups, pages = _group_by_page(clean)
    lines, edge = strip_edge_repeats(groups)
    return lines, {"pages": pages, "conf": conf,
                   "truncated": any(mark in raw for mark in _TRUNC_MARKS), **edge}
