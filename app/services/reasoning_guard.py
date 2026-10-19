"""思考过程元数据过滤：防止 AI 把系统提示词/内部设定/负面信号泄露给用户

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import re
from typing import List

from ..core.logging import setup_logging
from ..core.safety_filter import has_control_token

logger = setup_logging()

# ============================================================
# 思考过程元数据过滤：防止 AI 把系统提示词/内部设定/负面信号泄露给用户
# ============================================================
# 强指纹：命中任一即判定该思考段泄露 → 整段不展示
# （系统提示词引用、人设设定、系统限制、工具失败等负面信号）
_REASONING_LEAK_PATTERNS = [
    # ---- 系统提示词 / 指令引用 ----
    "system prompt", "system_prompt", "systemPrompt",
    "根据我的系统提示词", "我的系统提示词", "系统提示词要求", "按照系统提示词",
    "my system prompt", "My system prompt", "my system prompt says",
    "according to my system", "According to my system",
    "the instruction says", "instructions say", "per my instructions",
    "the system prompt says", "system says", "as instructed",
    "my instructions say", "my instructions", "per my system",
    "output language requirement", "always respond in",
    "do not output chinese", "unless the user explicitly asks",
    # ---- 中文指令引用 ----
    "输出语言要求", "请始终使用", "不要输出英文", "不要输出中文",
    "按照指令", "根据指令", "指令要求", "系统设定", "系统限制",
    "系统配置", "根据人设", "按人设", "角色设定", "人设要求",
    # ---- 多 Agent 编排指令复述（模型思考时复述注入指令 → 泄露）----
    "不要提", "不要提及", "禁止提及", "不得提及", "别提",
    "多Agent", "多 Agent", "讨论摘要", "自然口吻", "口吻整合",
    "各领域专家", "专家意见", "专家讨论",
    # ---- 工具调用计划复述（模型思考时写出内部函数名/调用签名 → 泄露）----
    "web_search", "search_knowledge",
    "fetch_weather_async", "call_sub_agent",
    "query_weather", "query_hotel", "query_route", "query_food",
    # ---- 负面信号：工具失败 / 异常 ----
    "工具调用失败", "调用失败", "工具执行失败", "工具超时", "工具报错",
    "tool call failed", "tool failed", "tool execution failed",
    "tool timeout", "tool error", "call failed",
    "重试失败", "重试仍失败", "请求失败", "请求超时",
    "failed to", "error occurred", "an error occurred",
    # ---- 内部实现细节 ----
    "response_format", "json_object", "llm_semaphore",
    "tools_enabled", "knowledge_base", "max_tokens", "temperature",
    "## 核心原则", "## 可用工具", "## 默认值",
    "防幻觉", "最小工具调用", "路由识别",
    "【输出格式】", "只输出JSON",
    # ---- 注入指令复述（system/user 注入段的标志性短语被思考复述 → 泄露）----
    "请以用户最新的消息为准", "用户上传了以下文件", "【提示：", "引用要求",
    "共享上下文",
    # ---- 约束复述（2026-09-05 实测泄露：模型逐条复述输出约束/文件模板规则）----
    "emoji", "中文（简体）", "约束检查", "特定模板", "文件上传场景",
    "重申身份", "身份设定", "意图识别", "功能定位", "语言风格",
    # ---- 供应商/基础设施指纹（2026-09 qwen 切换：模型身份与端点不得外露）----
    "qwen", "deepseek", "通义", "千问", "百炼",
    "dashscope", "aliyuncs", "compatible-mode",
    "enable_thinking", "reasoning_content", "api_key", "api_base",
]

# 整段抑制：即使正常思考也可能提到工具名，但这些是确凿的系统设定引用
_REASONING_SUPPRESS_PATTERNS = [
    "system prompt", "system_prompt", "systemPrompt",
    "根据我的系统提示词", "我的系统提示词", "系统提示词要求", "按照系统提示词",
    "my system prompt", "according to my system",
    "the instruction says", "instructions say", "per my instructions",
    "as instructed", "my instructions say",
    "output language requirement", "always respond in", "do not output chinese",
    "输出语言要求", "请始终使用", "不要输出英文", "不要输出中文",
    "系统设定", "系统限制", "系统配置", "根据人设", "按人设", "角色设定", "人设要求",
    "不要提", "不要提及", "禁止提及", "不得提及", "别提",
    "多Agent", "多 Agent", "讨论摘要", "自然口吻", "口吻整合",
    "工具调用失败", "调用失败", "工具执行失败", "工具超时", "工具报错",
    "tool call failed", "tool failed", "tool execution failed", "tool timeout",
    "重试失败", "重试仍失败",
    # ---- 注入指令复述（同上，强指纹）----
    "请以用户最新的消息为准", "用户上传了以下文件", "【提示：", "引用要求",
    "共享上下文",
    # ---- 供应商/基础设施指纹（模型身份/端点/配置项，出现即认为思考被污染）----
    "qwen", "deepseek", "通义", "千问", "百炼",
    "dashscope", "aliyuncs", "compatible-mode",
    "enable_thinking", "reasoning_content", "api_key", "api_base",
]

# Key 形状（DeepSeek/百炼/OpenRouter 等均为 sk- 前缀）：思考里出现即视为泄露
# 注：不能用 \b——中文汉字在 Python re 里算 \w，"是sk-xxx" 会因无词边界漏检
_KEY_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}")

# 预编译正则：流式思考每 Chunk 上百次过滤，避免逐模式 in 扫描（P1 #14）
_REASONING_LEAK_RE = re.compile(
    "|".join(re.escape(p) for p in _REASONING_LEAK_PATTERNS), re.IGNORECASE
)
_REASONING_SUPPRESS_RE = re.compile(
    "|".join(re.escape(p) for p in _REASONING_SUPPRESS_PATTERNS), re.IGNORECASE
)


def reasoning_leaked(text: str) -> bool:
    """检测思考内容是否泄露了系统设定/负面信号/模型身份/Key（整段抑制）"""
    if not text:
        return False
    return _REASONING_SUPPRESS_RE.search(text) is not None or _KEY_SHAPE_RE.search(text) is not None


def _keep_lines(lines: List[str]) -> str:
    """逐行弱指纹剔除：命中泄露词的非空行丢弃，其余连同换行原样保留

    保留行尾换行（旧实现 .strip() 会把块间换行吃掉，行与行在展示端粘连）。
    """
    return "".join(
        line + "\n" for line in lines
        if not (line.strip() and _REASONING_LEAK_RE.search(line.strip()))
    )


class ReasoningStreamGuard:
    """流式思考过滤（跨块安全）：按「完整行」检测放行，强指纹命中即整流抑制

    ponytail: 2026-09-10 凌晨真实泄露复盘——旧实现逐 delta 过滤，指纹被块边界
    切开（如 query_ho|tel）即整体绕过，实测 478 字符提示词复述漏出 477 字符。
    现改为行缓冲：不见换行不放行，指纹无论怎么切都必须在完整行上现形；行尾
    残余由 flush() 在流尾冲刷（stream_llm_throttled 统一调用）。
    已知代价：整段无换行的超长单行会缓冲到流尾才处理（思考展示本就是辅助信息，
    思考模型正常输出带换行，可接受）。
    """
    _OVERLAP = 24

    def __init__(self, persona_id: str = ""):
        self._persona_id = persona_id
        self._buf = ""    # 已收未判的原始文本（行缓冲，含未完成的尾行）
        self._carry = ""  # buf 尾部 _OVERLAP 字符（强指纹跨块拼接检测窗）
        self._dead = False  # 强指纹命中后的整流抑制开关

    def _suppress_hit(self, text: str) -> bool:
        """强指纹检测：系统设定/失败信号/模型身份/Key 形状/控制标记，命中即整流抑制"""
        return (
            _REASONING_SUPPRESS_RE.search(text) is not None
            or _KEY_SHAPE_RE.search(text) is not None
            or has_control_token(text)
        )

    def feed(self, chunk: str) -> str:
        """输入一个思考增量，返回可安全展示的「完整行」文本（可能为空串）"""
        if self._dead or not chunk:
            return ""
        # 强指纹跨块预判：任意 ≤(OVERLAP+1) 字符的指纹无论在块内还是跨块都落窗
        if self._suppress_hit(self._carry + chunk):
            self._dead = True
            logger.warning("思考内容命中强泄露指纹（含跨块拼接），后续思考整流抑制")
            return ""
        self._buf += chunk
        self._carry = self._buf[-self._OVERLAP:]
        if "\n" not in self._buf:
            return ""  # 尾行未完成：继续缓冲（这是旧版泄露的根源场景）
        *lines, self._buf = self._buf.split("\n")
        return _keep_lines(lines)

    def flush(self) -> str:
        """流结束冲刷：缓冲中的最后一行（无换行尾）补检后放行；调用后守卫作废

        尾行按原文返回（不补换行），保持流内容保真。
        """
        if self._dead or not self._buf:
            return ""
        tail, self._buf = self._buf, ""
        stripped = tail.strip()
        if self._suppress_hit(tail):
            self._dead = True
            logger.warning("流尾冲刷命中强泄露指纹，该行不展示")
            return ""
        if stripped and _REASONING_LEAK_RE.search(stripped):
            return ""
        return tail
