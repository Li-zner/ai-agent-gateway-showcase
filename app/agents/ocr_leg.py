"""OCR 腿 — 引擎探测、置信度尾注、倾角校正与两条 OCR 降级通道。

从 `document_parser.py` 拆出（2026-10-15 业主点单「拆成独立模块」）：解析器文件被
deskew＋警示分级顶到 670 行，越过「文件 ≤600 行」硬门禁，而这条门禁没有豁免通道。
切线是**引擎 vs 解析**：这里只管"给一张图，出文本＋置信度尾注"，PDF/Word/TXT 的
页数上限、zip 炸弹防护、截断标注都留在解析器里。

拆分为纯搬动，判据字面量与警示文案一字未改（两条降级腿的版面模式站点数由
`tests/unit/test_20261015_ocr_deskew_leg.py` 扫本文件源码钉住，注释里也别留那个字面量，
否则站点数会多数一处）。打桩要点：monkeypatch 绑的是**名字被查找的那个模块**，
所以要钉引擎状态就往本模块打（`ol._HAS_RAPID = False`），
对 `document_parser` 赋值在拆分后不再有效果。
"""
import io
import threading
# 只用到 run，且按模块内别名导入：探测测试替换 ol._subprocess_run 即可，
# 不必去 patch 全局 subprocess 模块（会影响同进程其他测试）
from subprocess import run as _subprocess_run
from ..core.logging import setup_logging

logger = setup_logging()

# OCR 引擎串行化锁：RapidOCR 内部 ONNX Runtime 不支持并发调用（P1 #43）
_OCR_LOCK = threading.Lock()

# ============================================================
# 图片 OCR 引擎（RapidOCR 优先，Tesseract 降级）
# ============================================================
# PIL 必须优先导入（被 RapidOCR / Tesseract / _ocr_image_bytes 共用）
try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# RapidOCR（深度学习，中文优秀，基于 ONNX Runtime）
_HAS_RAPID = False
if _HAS_PIL:
    try:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID_ENGINE = RapidOCR()
        _HAS_RAPID = True
        logger.info("RapidOCR 引擎已就绪")
    except Exception as e:
        logger.warning(f"RapidOCR 加载失败（将使用 Tesseract 降级）: {e}")

# Tesseract（传统 OCR，作为降级）
# 探测结果的进程内缓存：None=尚未探测（2026-09-22 审阅 P2 改成惰性），
# True/False=已探测。测试可把它置回 None 重新走探测分支。
_HAS_TESSERACT: bool | None = None
# 探测子进程硬超时：tesseract 二进制在但卡住（缺字体/挂在校验上）时，
# 没有超时的探测会把调用方线程一直占住
_TESSERACT_PROBE_TIMEOUT_S = 5.0


def _tesseract_ready() -> bool:
    """判据是"系统里有 tesseract 二进制"，不是"能 import pytesseract"。

    云端 lite 镜像只装 curl（deploy/Dockerfile.lite:15）却照样装了 requirements.txt
    里的 pytesseract 包，而 RapidOCR 两个镜像都没有——按 import 判定会让每张图都
    抛 TesseractNotFoundError 再被吞成空串。

    09-22 审阅 P2：探测不再走 pytesseract.get_tesseract_version()，它内部
    起子进程但不接受 timeout 参数；这里直接跑 `tesseract --version` 并带
    timeout，二进制挂起时最多占住 5 秒而不是永久卡死。
    """
    try:
        import pytesseract
        cmd = getattr(pytesseract, "tesseract_cmd", "tesseract")
        proc = _subprocess_run([cmd, "--version"], capture_output=True, text=True,
                               timeout=_TESSERACT_PROBE_TIMEOUT_S)
        if proc.returncode != 0:
            raise RuntimeError(f"tesseract --version 退出码 {proc.returncode}")
    except Exception as e:
        # 可选依赖：包缺失、二进制缺失、探测超时都返回 False（OCR 走 RapidOCR
        # 或未安装提示）——探测失败绝不能变成解析失败
        logger.info(f"Tesseract 引擎不可用（OCR 跳过该腿）: {type(e).__name__}")
        return False
    return True


def _tesseract_available() -> bool:
    """首次使用时探测并进程内缓存（P2：探测此前发生在 import 期）。

    import 期探测的代价是"每个 worker 启动都要等一次子进程"：tesseract 挂起时
    滚动发布直接卡死在导入语句上，一个坏节点拖慢整个服务。缓存含失败结果，
    否则每张图都要重等一次探测超时（镜像不会在运行中途装包，结果不随请求变）。
    """
    global _HAS_TESSERACT
    if _HAS_TESSERACT is None:
        _HAS_TESSERACT = _tesseract_ready() if _HAS_PIL else False
    return _HAS_TESSERACT


def _conf_note(confs: list[float]) -> str:
    """平均置信度尾注（两条引擎共用，conf 统一 0-1）。

    之前主通道（PDF 内嵌图/Word 插图/整页渲染）把 RapidOCR 返回的 conf 直接
    丢弃，只有独立图片入口带——外部评审"OCR 只能出 text，置信度做不到"即指此。
    低于复核线时附明确警示，让 LLM 与用户都知道这段文字不可尽信。
    复核线**不在此处写死**：唯一判据是清洗侧闸包的 `type_layer.OCR_REVIEW_CONF`
    （10-15 带真值实测定的，<0.85 那档平均 CER 0.3861、整字全对 0/14），
    两条线共用一个数，免得长成第二把尺（原 0.6 是直觉值，已作废）。
    **文案分两档（10-15 晚，业主点单「警示文案分级要不要做 → 做」）**：警示线上调到 0.85 后，
    34 张真样张里带警示的从 2 张涨到 12 张（`warn_volume_check.txt`），而净增那 10 条有 8 条 CER<0.10
    ——同一句「可能识别不准确」把「零星错字」和「整段读不成句」混成一种语气，是**提示精度问题不是判据问题**。
    分档线取闸包 `OCR_UNREADABLE_CONF=0.70`（与 0.85 同批 204 次对拍量出来的：`<0.70` 平均 CER 0.6929、
    `0.70~0.85` 0.1528）。**"要不要人工复核"仍然只看 `OCR_REVIEW_CONF` 一条线**，本函数只改说话的分贝。
    两档都必须以 `(注意：置信度较低` 起头——清洗侧 `_CONF_NOTE_RE` 靠这个前缀把警示句剥掉，
    前缀一改警示句就会混进语料。
    """
    if not confs:
        return ""
    from app.services.kbclean.type_layer import OCR_REVIEW_CONF, OCR_UNREADABLE_CONF
    avg = sum(confs) / len(confs)
    note = f"\n(OCR 置信度: {avg:.2f})"
    if avg < OCR_UNREADABLE_CONF:
        note += (f"\n(注意：置信度较低，低于 {OCR_UNREADABLE_CONF:.2f}，"
                 f"这段大概率读不成句，请回原文逐字核对，不要据此作答)")
    elif avg < OCR_REVIEW_CONF:
        note += (f"\n(注意：置信度较低，未达复核线 {OCR_REVIEW_CONF:.2f}，"
                 f"可能有零星错字，条号与金额等关键字段请核对原文)")
    return note


# ---------- OCR 前的倾角校正（deskew，2026-10-15 三臂对照）----------
# 依据：倾斜是降级腿唯一救不回来的退化——`--psm` 六档最优（psm3）也只把 5° 页从 0.5463
# 挪到 0.5048，而识别前按行投影方差把版面转正能到 0.0194，且其余 9 组（含正图）逐位不变。
# 只用 Pillow，不引新依赖；估角本身 24~114 ms/张（实测整批耗时 +4%）。
_SKEW_MIN_DEG = 0.5     # 低于此角不转：正图重采样一次只添边缘噪声，不添收益
_SKEW_MAX_DEG = 8.0     # 搜索上限；真实纸件倾角罕见超 8°，超出视为估错，按不转处理
_SKEW_GAIN_MIN = 1.15   # 峰值方差 / 0°方差 的最小增益：分栏、表格这类版面本来就不规则，
                        # 峰值可能只是噪声——增益不显著就不转（本轮唯一没量过的版面，保守拦）
_SKEW_COARSE_STEP = 1.0  # 粗搜步进（±8°）
_SKEW_FINE_STEP = 0.25   # 细搜步进：残余 0.25° 会留下 CER ~0.05 量级代价（rot_02 实测）
_SKEW_PROBE_LONG_SIDE = 600  # 估角用的降采样长边；角度精度由步进决定，像素够用的下限
# ponytail: 行投影判据只在「单栏密排 + 合成倾斜(rotate(θ))」两类样张上量过（34 张，10-15）。
# 分栏/表格/透视/弯曲纸件零实测覆盖，靠 _SKEW_GAIN_MIN 把不显著的峰值一律判成不转（宁可不转
# 也不转歪）。升级路径＝真 B 档手机拍纸件 + 多栏版面样张各补一批，再决定是否按版面分档。


def _skew_profile_var(ink: Image.Image, angle: float) -> float:
    """把二值墨迹图转正 angle 度后取每行墨迹均值，返回行投影方差（行距越水平方差越大）。"""
    rot = ink.rotate(angle, resample=Image.BILINEAR, expand=True, fillcolor=0)
    prof = rot.resize((1, rot.height), Image.BOX)  # 横向 BOX 下采样＝逐行求均值
    vals = list(prof.getdata())
    n = len(vals) or 1
    mean = sum(vals) / n
    return sum((v - mean) ** 2 for v in vals) / n


def _estimate_skew(img: Image.Image) -> tuple[float, float]:
    """估版面倾角，返回 (转正角度, 峰值/0° 的方差增益)；转正角度为 PIL rotate 的逆时针读数。

    粗搜 ±8°/步进 1°，再在峰值邻域 ±1°/步进 0.25° 细搜；极值中点自适应二值化，
    使淡墨样张（fg≈128/白底）不被固定阈值整页判成背景。
    """
    gray = img.convert("L")
    long_side = max(gray.size)
    if long_side > _SKEW_PROBE_LONG_SIDE:
        k = _SKEW_PROBE_LONG_SIDE / long_side
        gray = gray.resize((max(1, int(gray.width * k)), max(1, int(gray.height * k))),
                           Image.BILINEAR)
    lo, hi = gray.getextrema()
    mid = (lo + hi) // 2
    ink = gray.point(lambda p: 255 if p < mid else 0)  # 墨迹=255，背景=0
    var0 = _skew_profile_var(ink, 0.0)
    best_deg, best_var = 0.0, var0
    coarse = [x * _SKEW_COARSE_STEP for x in range(-8, 9) if x]
    for deg in coarse:
        v = _skew_profile_var(ink, deg)
        if v > best_var:
            best_deg, best_var = deg, v
    fine = [best_deg + x * _SKEW_FINE_STEP for x in range(-4, 5)]
    for deg in fine:
        v = _skew_profile_var(ink, deg)
        if v > best_var:
            best_deg, best_var = deg, v
    # best_deg 就是「再转多少度才水平」，即转正方向；调用方直接按它 rotate
    return best_deg, (best_var / var0 if var0 else 1.0)


def _deskew(img: Image.Image) -> Image.Image:
    """识别前把版面转正；三道守卫任一不满足就原样返回（不重采样）。

    估角与判据一律走 logger，不进 OCR 返回串——串格式是清洗侧剥离正则与量尺的依赖。
    """
    try:
        deg, gain = _estimate_skew(img)
    except Exception as e:  # 估角失败绝不能拖垮整条 OCR 腿
        logger.warning(f"倾角估算失败，按不转处理: {e}")
        return img
    if abs(deg) < _SKEW_MIN_DEG or abs(deg) > _SKEW_MAX_DEG or gain < _SKEW_GAIN_MIN:
        logger.info(f"OCR 倾角校正跳过: 拟转 {deg:+.2f}° 增益 {gain:.2f}")
        return img
    out = img.rotate(deg, resample=Image.BICUBIC, expand=True,
                     fillcolor=img.getpixel((0, 0)))  # 角点即背景色（mixed 类样张背景非纯白）
    logger.info(f"OCR 倾角校正: 已转 {deg:+.2f}°（增益 {gain:.2f}）")
    return out


def _join_ocr_line(words: list[str]) -> str:
    """把同一行内的 OCR 词条拼回一行：中文之间不加空格，西文词之间保留空格。

    为什么必须在这儿判而不是让下游去空格：Tesseract 的 `image_to_data` 出的是**词条**，
    chi_sim 基本一字一条；旧写法用空格把这些词条拼在一起，于是「第一条」变成「第 一 条」，
    清洗链的条号尺按连续字符匹配就认不出条文（Bug日志 67）。空格本来是西文的词边界，
    中文的不是——所以只有两侧都是字母或数字时才补这一个空格。
    """
    out = ""
    for word in words:
        if not out:
            out = word
            continue
        gap = " " if (out[-1].isascii() and out[-1].isalnum()
                     and word[0].isascii() and word[0].isalnum()) else ""
        out += gap + word
    return out


def _ocr_image(img: Image.Image, label: str = "") -> str:
    """对 PIL Image 执行 OCR，RapidOCR 优先；输出文本 + 置信度尾注"""
    if not _HAS_RAPID and not _tesseract_available():
        return ""
    try:
        with _OCR_LOCK:  # RapidOCR/ONNX 不支持并发，串行化（P1 #43）
            if _HAS_RAPID:
                # RapidOCR：直接处理 PIL Image
                result, elapse = _RAPID_ENGINE(img)
                if not result:
                    return ""
                texts, confs = [], []
                for _box, text, conf in result:
                    t = text.strip()
                    if t:
                        texts.append(t)
                        confs.append(float(conf))
            else:
                # Tesseract 降级（pytesseract 只在探测函数里 import 过，
                # 这里是模块级未绑定名字，必须就地导入）。
                # image_to_data 替代 image_to_string：后者只出行文，拿不到逐词 conf。
                import pytesseract
                from pytesseract import Output
                img = _deskew(img)  # 先转正再走原预处理，顺序与 10-15 三臂对照一致
                if img.mode != 'L':
                    img = img.convert('L')
                from PIL import ImageEnhance
                enhancer = ImageEnhance.Contrast(img)
                img = enhancer.enhance(1.5)
                data = pytesseract.image_to_data(
                    img, lang='chi_sim+eng', config='--psm 6',
                    output_type=Output.DICT)  # Bug 63：实装 0.3.13 的参数名是 output_type，`output=` 早被移除
                texts, confs = [], []
                buf: list[str] = []
                cur_line = None
                # 按引擎给的行坐标归行：`image_to_data` 出的是词条，同一视觉行的字共享
                # (block_num, par_num, line_num)。旧写法不管坐标、逐词条 append，最后拼成
                # 整页一行——行级状态机因此既看不到「第X条」的行首形状，也数不出页数。
                # 坐标列缺失时退化成"全部同一行"而不是 KeyError：本函数的宽 except 会把
                # 形状错误吞成空文本，看起来就像"引擎没认出字"（Bug日志 63 那一族的反向坑）。
                n_words = len(data["text"])
                coords = zip(data.get("block_num", [0] * n_words),
                             data.get("par_num", [0] * n_words),
                             data.get("line_num", [0] * n_words))
                for word, conf, (blk, par, ln) in zip(data["text"], data["conf"], coords):
                    w = word.strip()
                    if not w:
                        continue
                    if cur_line is not None and (blk, par, ln) != cur_line and buf:
                        texts.append(_join_ocr_line(buf))
                        buf = []
                    cur_line = (blk, par, ln)
                    buf.append(w)
                    try:
                        v = float(conf)
                    except (TypeError, ValueError):
                        continue
                    if v >= 0:  # -1 表示该格非文字（版面标记），不入均值
                        confs.append(v / 100)
                if buf:
                    texts.append(_join_ocr_line(buf))
            if not texts:
                return ""
            tag = f" ({label})" if label else ""
            body = "\n".join(texts)  # 一行一换行：旧写法用空格拼，整页被展平成一行（Bug日志 67）
            return f"\n[图片 OCR{tag}]: {body}{_conf_note(confs)}"
    except Exception as e:
        logger.warning(f"OCR 失败 {label}: {e}")
        return ""

def _ocr_image_bytes(img_bytes: bytes, ext: str = "png", label: str = "") -> str:
    """对图片字节执行 OCR"""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        return _ocr_image(img, label)
    except Exception as e:
        logger.warning(f"图片字节解析失败 {label}: {e}")
        return ""


def ocr_image_file(filepath: str) -> str:
    """独立图片文件的 OCR 同步核（在解析器的线程池里跑，async 外壳见 `document_parser.parse_image`）。

    与 `_ocr_image` 两条腿口径一致：文本 + 置信度尾注，且**都先过 `_deskew`**
    （Bug 63 的教训就是两处各写一遍、只改一处）。整段从原 `parse_image._ocr()` 闭包搬来，
    引擎状态判据都查本模块的全局名，所以打桩请钉 `ocr_leg`。
    """
    if not _HAS_RAPID and not _tesseract_available():
        return "[OCR 引擎未安装]"
    try:
        if _HAS_RAPID:
            # RapidOCR/ONNX 推理不支持并发，独立图片入口也必须与文档内 OCR
            # 共用同一把锁，否则并发上传会重入原生推理。
            with _OCR_LOCK:
                result, elapse = _RAPID_ENGINE(filepath)
            if not result:
                return "[图片中未识别到文字]"
            texts, confs = [], []
            for _box, text, conf in result:
                t = text.strip()
                if t:
                    texts.append(t)
                    confs.append(float(conf))
            if not texts:
                return "[图片中未识别到文字]"
            # 与 _ocr_image 同一口径：文本 + 置信度尾注（原来是逐行 [conf] 前缀，
            # 与文档内通道两种格式，且前缀噪声对 LLM 消费无益）
            return "\n".join(texts) + _conf_note(confs)
        else:
            # import pytesseract 必须就地做（模块级未绑定名字，原实现此处
            # 直接用 pytesseract 名字，Tesseract 降级腿一跑就 NameError）
            import pytesseract
            from pytesseract import Output
            from PIL import Image, ImageEnhance
            with Image.open(filepath) as raw_img:  # P1 #6：显式关闭文件句柄
                if raw_img.mode != 'L':
                    img = raw_img.convert('L')
                else:
                    img = raw_img
                img = _deskew(img)  # 第二条降级腿同样先转正（Bug 63 教训：两处必须一起改）
                enhancer = ImageEnhance.Contrast(img)
                img = enhancer.enhance(1.5)
                data = pytesseract.image_to_data(
                    img, lang='chi_sim+eng', config='--psm 6',
                    output_type=Output.DICT)  # Bug 63：同上，两处降级腿一起改
            texts, confs = [], []
            for word, conf in zip(data["text"], data["conf"]):
                w = word.strip()
                if not w:
                    continue
                texts.append(w)
                try:
                    v = float(conf)
                except (TypeError, ValueError):
                    continue
                if v >= 0:
                    confs.append(v / 100)
            if not texts:
                return "[图片中未识别到文字]"
            return " ".join(texts) + _conf_note(confs)
    except Exception as e:
        return f"[OCR 识别失败: {e}]"
