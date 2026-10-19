"""对话支撑原语：输出语言指令 / 人格提示词格式化 / 思考展示开关 / DeepSeek Key 轮询

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import asyncio
import os
import uuid
from contextvars import ContextVar
from typing import Dict

from ..core.logging import setup_logging

logger = setup_logging()


def lang_instruction(lang: str = "zh") -> str:
    """根据界面语言生成 LLM 输出语言指令（追加到 system prompt）

    附加"请勿使用 emoji"以抑制模型原生 emoji 输出（产品要求专业、去表情）。
    lang 传语言代码（zh/en）；调用方传 req 对象时取 req.lang（历史兼容）。
    """
    if not isinstance(lang, str):
        # 兼容旧调用：传入带 .lang 属性的 req 对象
        lang = getattr(lang, "lang", "zh") or "zh"
    if lang == 'en':
        return (
            "\n\n【Output Language Requirement】\n"
            "1. Always respond in **English**.\n"
            "2. Render every place, attraction, food, dish, hotel and venue"
            " name in English: use its conventional or established English"
            " name when one exists, otherwise its pinyin. On the first mention"
            " of each such name you may add the Chinese in parentheses, e.g."
            " `Wide Street (宽窄巷子)`; afterwards use the English name only.\n"
            "3. Keep numbers, amounts and dates unchanged.\n"
            "4. Do not output Chinese except the first-mention parenthetical"
            " names allowed in rule 2, and unless the user explicitly asks.\n"
            "5. Do not use any emoji."
        )
    return (
        "\n\n【输出语言要求】\n"
        "1. 请始终使用**中文（简体）**回答用户的所有问题。\n"
        "2. 除非用户明确要求，否则不要输出英文。\n"
        "3. 请勿使用任何 emoji 表情符号。"
    )


# 思考/状态占位文案：经 SSE thought / reasoning_chunk 进前端"思考过程"面板，面向用户。
# 集中在此供各链路取值，避免英文界面漏中文（此前散落在 fast/react/stream_core 硬编码）。
_THINKING_ZH = {
    "recommend_multi": "正在查询多个信息，请稍候...",
    "search_info": "正在查询相关信息...",
    "thinking": "正在思考...",
    "kb": "正在查询知识库...",
    "weather": "正在查询 {city} 天气...",
    "hotel": "正在搜索 {destination} 酒店...",
    "food": "正在搜索 {destination} 美食...",
    "route": "正在规划从 {departure} 到 {destination} 的路线...",
    "analyzing": "正在分析你的问题...",
    "experts": "专家们正在讨论分析...",
    "kb_timeout": "知识库查询超时，正在基于已有知识继续回答...",
    "kb_timeout_civil": "知识库查询超时，本次无法基于民法典依据继续回答。",
    "service_unavailable": "服务暂时不可用，请稍后再试。",
}
_THINKING_EN = {
    "recommend_multi": "Looking up several details, please wait...",
    "search_info": "Looking up relevant information...",
    "thinking": "Thinking...",
    "kb": "Searching the knowledge base...",
    "weather": "Checking the weather in {city}...",
    "hotel": "Searching hotels in {destination}...",
    "food": "Searching food in {destination}...",
    "route": "Planning the route from {departure} to {destination}...",
    "analyzing": "Analyzing your question...",
    "experts": "Specialists are discussing...",
    "kb_timeout": "Knowledge base lookup timed out; continuing with available information...",
    "kb_timeout_civil": "Knowledge base lookup timed out; I can't answer from Civil Code provisions this time.",
    "service_unavailable": "The service is temporarily unavailable. Please try again later.",
}


def thinking_text(key: str, lang: str = "zh", **args) -> str:
    """返回本地化的思考/状态占位文案（面向用户，进"思考过程"面板）。

    lang 传语言代码或带 .lang 的 req 对象；未知 key 回落中文表再回落 key 本身；
    模板缺参数时原样返回，不抛异常（占位文案不该打崩流）。
    """
    if not isinstance(lang, str):
        lang = getattr(lang, "lang", "zh") or "zh"
    table = _THINKING_EN if lang.lower().startswith("en") else _THINKING_ZH
    tpl = table.get(key) or _THINKING_ZH.get(key) or key
    try:
        return tpl.format(**args)
    except (KeyError, IndexError):
        return tpl


# 请求级界面语言：在流式上下文与后台 Agent 任务入口各 set 一次，供无 lang 形参的
# 答案级文案吐点（react_steps 步骤原语、apply_safety_filter 等）就近解析；未登记时
# 回落 zh——与全仓 lang 默认口径一致，绝不因取不到语言而抛错。
_request_lang: ContextVar[str] = ContextVar("request_lang", default="zh")


def _resolve_lang(lang: object = None) -> str:
    """把 lang 归一成语言代码：显式字符串直接用；req 对象取 .lang；None/空 → contextvar → zh。"""
    if lang is not None and not isinstance(lang, str):
        lang = getattr(lang, "lang", "") or ""
    if isinstance(lang, str) and lang:
        return lang
    return _request_lang.get() or "zh"


def set_request_lang(lang: object) -> None:
    """在请求/任务入口登记界面语言，下游无 lang 参数的兜底文案据此切换中/英。"""
    _request_lang.set(_resolve_lang(lang))


def safety_message(lang: object = None, zh_text: str = None) -> str:
    """DFA 安全过滤命中的答案级文案，按界面语言选中/英。

    lang 传语言代码、req 对象或省略（取 contextvar）。中文取调用点过滤器的 safe_message
    （生产=config 中文、测试替身=其 stub，保留自定义词库口径），zh_text 省略时回落核心过滤器；
    英文无对应过滤器实例，覆盖为 SAFETY_FILTER_MESSAGE_EN 配置常量。"""
    if _resolve_lang(lang).lower().startswith("en"):
        from ..core.config import SAFETY_FILTER_MESSAGE_EN
        return SAFETY_FILTER_MESSAGE_EN
    if zh_text is not None:
        return zh_text
    from ..core.safety_filter import get_filter
    return get_filter().safe_message


def fallback_message(lang: object = None) -> str:
    """LLM「服务器繁忙」兜底的答案级文案，按界面语言返回中/英。"""
    from ..core.config import DEEPSEEK_FALLBACK_MESSAGE, DEEPSEEK_FALLBACK_MESSAGE_EN
    return (DEEPSEEK_FALLBACK_MESSAGE_EN
            if _resolve_lang(lang).lower().startswith("en")
            else DEEPSEEK_FALLBACK_MESSAGE)


def empty_answer(lang: object = None) -> str:
    """空回答占位文案，按界面语言返回中/英（英文请求不得吐中文占位）。"""
    from ..core.config import EMPTY_ANSWER_MESSAGE, EMPTY_ANSWER_MESSAGE_EN
    return (EMPTY_ANSWER_MESSAGE_EN
            if _resolve_lang(lang).lower().startswith("en")
            else EMPTY_ANSWER_MESSAGE)


def incomplete_answer(lang: object = None) -> str:
    """达最大步数仍无完整回答的兜底文案，按界面语言返回中/英。"""
    from ..core.config import INCOMPLETE_ANSWER_MESSAGE, INCOMPLETE_ANSWER_MESSAGE_EN
    return (INCOMPLETE_ANSWER_MESSAGE_EN
            if _resolve_lang(lang).lower().startswith("en")
            else INCOMPLETE_ANSWER_MESSAGE)


def content_filtered_note(lang: object = None) -> str:
    """答案被 DFA 终检拦截时追加的「内容已过滤」标记，按界面语言返回中/英。"""
    from ..core.config import CONTENT_FILTERED_NOTE, CONTENT_FILTERED_NOTE_EN
    return (CONTENT_FILTERED_NOTE_EN
            if _resolve_lang(lang).lower().startswith("en")
            else CONTENT_FILTERED_NOTE)


def safe_format_prompt(prompt: str, **kwargs) -> str:
    """安全格式化人格提示词：缺 {today}/{name} 等占位符时不抛 KeyError（P0 #3）

    占位符缺失时返回原始提示词，避免单个坏配置打崩整个请求。
    """
    try:
        return prompt.format(**kwargs)
    except (KeyError, ValueError):
        logger.warning("人格提示词格式化失败（缺少占位符），使用原始提示词")
        return prompt


def hide_reasoning(persona_id: str, persona) -> bool:
    """是否隐藏思考过程：人格配置 show_reasoning=False 时隐藏

    说明：show_reasoning 应作为人格配置布尔字段（P2 #24 完整迁移需改 core/persona_manager），
    当前用 getattr 兼容旧配置；未配置时默认展示思考。
    """
    show = getattr(persona, "show_reasoning", True) if persona else True
    return not show


# ---------- DeepSeek API Key 轮询（Round Robin + 连续失败剔除）----------
# 模块加载时缓存 Key 列表，避免每次请求重复 os.getenv（P1 #12 消除 3 次系统调用）
# 2026-09-05 用户决策：只保留主 Key（KEY_2/KEY_3 与主 Key 上游白名单不同，混用导致 400）
_DEEPSEEK_KEYS = [
    k for k in (os.getenv("DEEPSEEK_API_KEY", ""),) if k
]
_key_fail_count: Dict[str, int] = {}   # key -> 连续失败次数
_key_round_index = 0                   # 轮询游标
_key_lock = asyncio.Lock()             # 保护上面两个共享状态


async def get_deepseek_key() -> str:
    """带权重的 Round Robin 选择 Key，连续失败 >=3 的 Key 暂被剔除（P1 #13）"""
    global _key_round_index   # 函数内 rebind，需声明 global，否则被当作局部变量（UnboundLocalError）
    async with _key_lock:
        if not _DEEPSEEK_KEYS:
            return ""
        healthy = [k for k in _DEEPSEEK_KEYS if _key_fail_count.get(k, 0) < 3]
        if not healthy:
            healthy = _DEEPSEEK_KEYS   # 全部被剔除则重置，避免永久降级
        key = healthy[_key_round_index % len(healthy)]
        _key_round_index = (_key_round_index + 1) % len(healthy)
    tag = f"...{key[-4:]}" if key else "NONE"
    logger.info(f"Key 轮询: {tag}")
    return key


async def mark_key_result(key: str, ok: bool) -> None:
    """记录 Key 调用结果：成功清零失败计数，失败累计（供健康剔除使用）"""
    async with _key_lock:
        if ok:
            _key_fail_count.pop(key, None)
        else:
            _key_fail_count[key] = _key_fail_count.get(key, 0) + 1

# ---------- 扣费幂等键载体（2026-10-11 审阅 P1-②） ----------
def attach_bill_uid(usage, uid: str = ""):
    """给一次上游 LLM 调用的用量字典挂扣费幂等键，返回同一对象（无 usage 时原样返回）

    背景：`bill_token_usage` 只认 `usage["bill_uid"]`，非流式路径没有该载体，
    防双扣只剩「调用点唯一」这个人为约定（同一次调用的 usage 被消费两次即多收钱，
    形状即 P1-1）。键必须在**取到 usage 的那一处**现生成：若在扣费收口里合成，
    不同请求会拿到同名键，把该扣的钱判成重复 → 漏扣（比双扣更严重，口径见
    `llm_streaming._claim_bill`，回归钉子 test_missing_bill_uid_bills_without_claim）。

    已有键不覆盖——重复挂载得到同一个键，正是「第二次消费被拦」的前提；
    传 uid 则沿用，用于同一次调用的真实 usage 尾块与断连估算共用一个键。
    """
    if not isinstance(usage, dict) or usage.get("bill_uid"):
        return usage
    usage["bill_uid"] = uid or uuid.uuid4().hex
    return usage


# ---------- 公共非流式 LLM 调用（带 Flash 降级） ----------
# 场景：地图推荐 / 子代理等非流式调用。LLM_PRIMARY_MODEL 若与上游白名单不匹配（400），
# 自动用 LLM_FALLBACK_MODEL 重试——与主链路 model_try_list 降级语义一致。
async def post_chat_completion(payload: dict, timeout: float, prefer_flash: bool = False,
                               preferred_timeout: float | None = None,
                               models: list[str] | None = None) -> tuple[bool, dict]:
    """非流式 /chat/completions 公共原语：llm_endpoint 按模型路由端点，
    主模型失败时自动用 LLM_FALLBACK_MODEL（走 DeepSeek 端点）重试。
    prefer_flash=True 时 flash 优先、原模型兜底——轻量结构化任务（地图推荐/
    查询改写）flash 生成快得多（2026-09-09 埋点：主模型 10-17s，flash 1-6s）。
    preferred_timeout：首选模型的单独超时（2026-09-10 埋点：flash 间歇性挂起
    吃满 15s 才换备用，总计 21-27s；给首选 8s 快速熔断，总时长可控）。
    models：显式尝试链（按序、去重）——调用方自管阶梯时用（DB Agent P1-1），
    传入后 prefer_flash 与 payload.model 不参与链的构造。
    返回 (ok, data)；失败时 data 为 {"status":..., "body":...} 诊断信息。
    """
    import httpx
    from ..core.config import (
        LLM_FALLBACK_MODEL,
        LLM_PRIMARY_MODEL,
        apply_llm_request_options,
        llm_endpoint,
    )
    from ..middleware.circuit_breaker import get_breaker
    if models:
        chain = list(dict.fromkeys(models))
    else:
        primary = payload.get("model") or LLM_PRIMARY_MODEL
        chain = [LLM_FALLBACK_MODEL, primary] if prefer_flash else [primary, LLM_FALLBACK_MODEL]
    seen: set = set()
    last = {"status": None, "body": ""}
    for idx, model in enumerate(chain):
        if model in seen:
            continue
        seen.add(model)
        attempt_timeout = (preferred_timeout or timeout) if idx == 0 else timeout
        base_url, api_key = llm_endpoint(model, os.getenv("DEEPSEEK_API_KEY", ""))
        body = dict(payload, model=model)
        apply_llm_request_options(body, model)
        if model.startswith("qwen"):
            # 百炼兼容端点对带 temperature 的请求偶发 400（历史注释），
            # 随机温度抖动只用于 DeepSeek 侧，qwen 兜底时剥离
            body.pop("temperature", None)
        try:
            async def _post_once():
                async with httpx.AsyncClient(timeout=attempt_timeout) as client:
                    return await client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=body,
                    )

            breaker = get_breaker(
                f"llm-post:{model}", call_timeout=attempt_timeout + 5,
            )
            resp = await breaker.call(_post_once)
            if resp.status_code == 200:
                await mark_key_result(api_key, True)
                data = resp.json()
                # 扣费幂等键（P1-②）：每次成功的最上游调用一个键，随 usage 带给所有消费点
                attach_bill_uid(data.get("usage") if isinstance(data, dict) else None)
                return True, data
            last = {"status": resp.status_code, "body": resp.text[:200]}
            await mark_key_result(api_key, False)
        except Exception as e:
            last = {"status": None, "body": str(e)[:200]}
            await mark_key_result(api_key, False)
    return False, last

def display_safe_tool_result(result):
    """tool_result SSE 事件对外文本消毒（2026-09-10 审查 P2）：

    web_search 等第三方内容/错误串此前随 tool_result 事件直发前端，绕过安全过滤
    （answer 路径同块均先滤）。返回消毒后的深拷贝，不影响喂给 LLM 的原对象。
    """
    import copy

    from ..core.safety_filter import get_filter, sanitize_error_text

    sf = get_filter()

    def _clean(v):
        if isinstance(v, str):
            v = sanitize_error_text(v)
            if sf.contains_sensitive(v):
                return "[该内容已被安全过滤]"
            return v
        if isinstance(v, list):
            return [_clean(x) for x in v]
        if isinstance(v, dict):
            return {k: _clean(x) for k, x in v.items()}
        return v

    return _clean(copy.deepcopy(result))
