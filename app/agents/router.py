"""
任务路由模块 — 简单任务 vs 复杂任务的分发决策器

功能:
  1. classify_intent() - 基于关键词 + LLM 轻量分类，判断用户意图涉及的领域
  2. RouteDecision - 路由决策结果，告诉调用方是"简单"还是"复杂"
  3. handle_simple_task() - 直接执行单个 Agent + 一次 LLM 格式化，不走多 Agent 协商

设计原则:
  - 简单任务: 精确匹配关键词，直接调工具 → LLM 格式化 → 返回，零多余开销
  - 复杂任务: 走现有的四 Agent 圆桌讨论流程 (orchestrator.py)
"""

import json
import asyncio
import uuid
from typing import List, Dict, Optional
from ..core.logging import setup_logging
from ..core.persona_manager import is_civil_persona
from ..core.config import (
    HTTP_TIMEOUT_MEDIUM, TOOL_TIMEOUT,
)
from ..core.place_extract import extract_destination
# 必须模块级导入：safe_set 是模块级函数，曾因 SemanticCache 只在
# handle_simple_task 内局部导入而必然 NameError（被 except 吞掉，缓存静默不写入）
# SemanticCache 模块级可解析是历史修复契约（09-06 NameError 事故），单测钉住
from ..core.semantic_cache import SemanticCache, safe_set

# 简单任务的格式化/收尾原语已拆至 simple_task.py（2026-09-14：router.py 达 622 行
# 硬限，按子系统边界搬迁）；以旧名 re-export，handle_simple_task 与历史测试无需改动。
from .simple_task import (  # noqa: F401
    _build_formatter_messages, _deliver_simple_answer, _emit_simple_result,
    _fallback_tool_view,
    _format_via_llm, _load_file_ctx, _safe_history, _save_simple_task_artifacts,
)

# 意图词表与命中判定已拆至 intent_lexicon.py（2026-10-18：router.py 因接英文意图达 611 行，
# 越过「单文件 ≤600 行」硬限，按子系统边界搬迁）。词表名沿用，classify_by_keywords 与
# 既有测试的引用口径不变；match_terms 由私有 _match_terms 升为跨模块公开名。
from .intent_lexicon import (  # noqa: F401
    INTENT_KEYWORDS, RECOMMEND_PATTERNS, SIMPLE_MAX_DOMAINS,
    LEGAL_SCENE_MARKERS, match_terms,
)

logger = setup_logging()


def _has_travel_trip_intent(query: str) -> bool:
    """识别“目的地 + 旅游/旅行”的规划意图，交给统一槽位澄清。"""
    # 法律场景不认作旅游规划（P1 #77）：判据走词表腿的「一把尺」match_terms
    if match_terms(query, LEGAL_SCENE_MARKERS):
        return False
    return bool(extract_destination(query)) and match_terms(
        query, ("旅游", "旅行", "游玩", "度假", "自由行",
                "travel", "trip", "tour", "vacation", "visit", "holiday")
    )


def classify_by_keywords(query: str) -> Dict:
    """
    基于关键词的意图分类（零 API 调用）
    返回: {"agents": [...], "is_simple": bool, "is_recommend": bool}
    """
    matched = set()
    for agent, keywords in INTENT_KEYWORDS.items():
        if keywords and match_terms(query, keywords):
            matched.add(agent)
    matched_list = sorted(matched)

    # 检测是否为全推荐模式
    # 条件：用户表达了全面的旅行规划需求
    is_recommend = False
    has_recommend_kw = match_terms(query, RECOMMEND_PATTERNS)
    # 法律咨询场景的"推荐"不触发旅游全推荐（P2 #20；P1 #77 判据升级为词表腿 match_terms，英文法律句不再漏过）
    if has_recommend_kw and match_terms(query, LEGAL_SCENE_MARKERS):
        has_recommend_kw = False
    # "渭南旅游/成都旅行"即使没有"推荐/规划"也是旅行规划意图，必须补出发地槽位。
    has_travel_trip = _has_travel_trip_intent(query)
    if has_recommend_kw or has_travel_trip:
        # 触发全推荐模式的条件（满足任意一条即可）：
        # 1. 匹配了至少2个领域关键词
        # 2. 包含明确的全局规划意图词（攻略/规划/安排/行程等）
        # 3. 匹配了1个领域且包含推荐/安排/规划等词
        # 4. 已识别目的地且明确提到旅游/旅行
        comprehensive_plan = match_terms(query, [
            "攻略", "规划", "安排", "行程", "旅游计划",
            "旅行攻略", "帮我规划", "帮我安排",
            "plan", "arrange", "itinerary",
        ])
        if len(matched_list) >= 2:
            is_recommend = True
        elif comprehensive_plan:
            is_recommend = True
        elif has_travel_trip or (len(matched_list) >= 1 and has_recommend_kw):
            is_recommend = True

    # 全推荐模式下，启用所有4个旅游Agent
    if is_recommend:
        for agent in ["query_weather", "query_hotel", "query_route", "query_food"]:
            matched.add(agent)
        matched_list = sorted(matched)
        # 全推荐模式永远复杂
        return {
            "agents": matched_list,
            "is_simple": False,
            "is_recommend": True,
            "method": "keyword"
        }

    # 排除 search_knowledge 的简单/复杂判断（知识库永远走单独路径）
    travel_agents = [a for a in matched_list if a != "search_knowledge"]
    has_knowledge = "search_knowledge" in matched_list

    # 如果既有知识库又有旅游意图 → 复杂
    if has_knowledge and travel_agents:
        return {
            "agents": matched_list,
            "is_simple": False,
            "is_recommend": False,
            "method": "keyword"
        }

    # 纯知识库 → 简单
    if has_knowledge and not travel_agents:
        return {
            "agents": matched_list,
            "is_simple": True,
            "is_recommend": False,
            "method": "keyword"
        }

    return {
        "agents": matched_list,
        "is_simple": len(matched_list) <= SIMPLE_MAX_DOMAINS,
        "is_recommend": False,
        "method": "keyword"
    }


async def classify_by_llm(query: str, username: str = "") -> Dict:
    """
    基于 LLM 的轻量意图分类（用于关键词无法判断的边界情况）
    只做分类，不生成回答，token 消耗极小。
    返回: {"agents": [...], "is_simple": bool}
    """
    from ..services.chat_support import get_deepseek_key
    api_key = await get_deepseek_key()
    if not api_key:
        logger.warning("LLM 分类无 API Key，回退关键词分类")
        return classify_by_keywords(query)

    system_prompt = """你是一个意图分类器。分析用户问题，判断涉及以下哪些领域（可多选）：
- query_weather: 天气/气温相关
- query_hotel: 酒店/住宿相关
- query_route: 路线/交通相关
- query_food: 美食/餐厅相关
- search_knowledge: 民法典/法律相关

输出 JSON 格式：
{"agents": ["query_weather"], "is_simple": true, "is_recommend": false}

规则：
- 如果只涉及 1 个领域 → is_simple: true
- 如果涉及 2 个及以上 → is_simple: false（需要多 Agent 协作）
- 如果不涉及任何领域 → agents: [], is_simple: true
- 如果用户请求旅游推荐/攻略/安排行程 → agents 包含 query_weather,query_hotel,query_route,query_food 全部四个，is_simple: false, is_recommend: true
- 只输出 JSON，不要输出其他文字"""

    try:
        from ..core.concurrency import llm_semaphore
        from ..services.chat_support import post_chat_completion
        # 2026-09-30 校准修复（逻辑审阅 A1）：原先裸 httpx + HTTP_TIMEOUT_SHORT(5s)
        # 打非流式补全，而主模型典型 10-17s（chat_support 埋点口径）——开关开了
        # 也必超时回退关键词。改走 post_chat_completion 原语（sub_agents 同款）：
        # flash 优先 + 主模型兜底 + Key 口径一致 + 熔断，超时 15s。
        # max_tokens 100→300：flash 会先花预算在 reasoning_content 上（data_ask
        # 实测口径），100 会挤空正文；分类提示词极小，300 无成本差异。
        async with llm_semaphore:
            ok, data = await post_chat_completion(
                {"messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"用户问题: {query}"}
                ],
                 "response_format": {"type": "json_object"},
                 "temperature": 0.0,
                 "max_tokens": 300},
                HTTP_TIMEOUT_MEDIUM, prefer_flash=True)
        if not ok:
            raise ValueError(f"upstream {data.get('status')}")

        content = data["choices"][0]["message"]["content"]
        if not content:
            # flash 空 正文（预算被 reasoning 吃掉）按失败处理，走关键词回退
            raise ValueError("empty content (flash reasoning budget)")
        result = json.loads(content)
        result["method"] = "llm"
        # 计入计费（2026-09-09 主人拍板）：意图分类属用户请求触发（调用方由
        # INTENT_USE_LLM 门控，默认关；口径先对齐，启用即计费）
        _usage = data.get("usage") or {}
        if username and (_usage.get("prompt_tokens") or _usage.get("completion_tokens")):
            from ..services.llm_streaming import record_token_usage
            record_token_usage(_usage, username, "")
        # 确保 is_recommend 字段存在
        if "is_recommend" not in result:
            result["is_recommend"] = False
        return result
    except Exception as e:
        logger.warning(f"LLM 分类失败，回退关键词: {e}")
        kw_result = classify_by_keywords(query)
        kw_result["method"] = "keyword_fallback"
        return kw_result


async def classify_intent(query: str, use_llm: bool = False, username: str = "") -> Dict:
    """
    统一的意图分类入口

    Args:
        query: 用户问题
        use_llm: 是否优先使用 LLM 分类（更准确但慢一点）

    返回:
        {"agents": [...], "is_simple": bool, "is_recommend": bool, "method": str}
    """
    if use_llm:
        return await classify_by_llm(query, username=username)
    return classify_by_keywords(query)


def get_tools_for_intent(agents: List[str]) -> list:
    """
    根据意图匹配的 Agent 列表，返回对应的工具定义列表。
    只包含匹配到的工具，不包含全部工具 → 减小 prompt 长度，加快 LLM 响应。

    返回: tools list (function calling 格式)
    """
    from .tool_definitions import (
        TOOL_WEATHER, TOOL_HOTEL, TOOL_ROUTE, TOOL_FOOD,
        TOOL_SEARCH_KNOWLEDGE, TOOL_WEB_SEARCH,
    )
    TOOL_MAP = {
        "query_weather": TOOL_WEATHER,
        "query_hotel": TOOL_HOTEL,
        "query_route": TOOL_ROUTE,
        "query_food": TOOL_FOOD,
        "search_knowledge": TOOL_SEARCH_KNOWLEDGE,
        "web_search": TOOL_WEB_SEARCH,
    }
    tools = []
    travel_agents = {"query_weather", "query_hotel", "query_route", "query_food"}
    # 民法典问题只允许知识库检索，不能暴露联网或项目知识工具。
    # 旅游场景仍保留 web_search，由 ReAct 根据实时信息需求调用。
    if any(agent in travel_agents for agent in agents):
        tools.append(TOOL_WEB_SEARCH)
    for agent in agents:
        tool = TOOL_MAP.get(agent)
        if tool and tool not in tools:
            tools.append(tool)
    return tools


def get_agent_names_for_orchestrator(agents: List[str]) -> List[str]:
    """
    返回应该参与多 Agent 讨论的 Agent 名称列表。
    排除 search_knowledge（知识库不需要参与圆桌讨论）。
    """
    return [a for a in agents if a in ("query_weather", "query_hotel", "query_route", "query_food")]


# ============================================================
# 简单任务执行器
# ============================================================

async def _call_simple_tool(agent_name: str, user_query: str,
                            username: str, user_perms, history: list | None = None,
                            persona_id: str = "") -> dict:
    """简单任务第 1 步：调用单个工具（天气/知识库/联网走 dispatch_tool，旅游三工具走子 Agent）"""
    if agent_name in ("query_weather", "search_knowledge", "web_search"):
        # 统一走 dispatch_tool：天气自动提取城市（不再把整句话当地址），
        # 知识库/联网检索按原 query 调用（修复知识类问题被误报"未知工具"的 P0）
        # history 供 weather 无地点时查上下文最近提及的城市（2026-09-10 语义定稿）
        from ..core.stream_utils import dispatch_tool
        return await dispatch_tool(agent_name, {}, user_query, user_perms, username,
                                   history=history, persona_id=persona_id)
    if agent_name in ("query_hotel", "query_route", "query_food"):
        # 简单任务：从关键词中提取城市/目的地参数
        args = _extract_simple_args(agent_name, user_query)
        from .sub_agents import call_sub_agent
        return await call_sub_agent(agent_name, args, user_query, username=username)
    return {"error": f"未知工具: {agent_name}"}


async def _run_simple_tool(agent_name: str, user_query: str, username: str,
                           user_perms, history: list | None,
                           persona_id: str = "") -> dict:
    """执行简单任务工具，统一超时与空结果语义。"""
    try:
        result = await asyncio.wait_for(
            _call_simple_tool(
                agent_name, user_query, username, user_perms,
                history=history, persona_id=persona_id,
            ),
            timeout=TOOL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        result = {"error": "工具超时"}
    return result if result is not None else {"error": "工具返回空"}


async def _format_simple_answer(api_key: str, user_query: str, username: str,
                                persona_id: str, lang: str, agent_name: str,
                                tool_result: dict, file_ids,
                                history: list | None) -> tuple[str, bool]:
    """基于工具结果生成回答；失败时降级为可读数据视图（不再回退原始 JSON）。

    返回 (文本, 可写语义缓存)。降级/兜底文案 cache_allowed=False（2026-10-07 C1/P1-3）：
    与 runner 主路 write_cache=not blocked 同契约——缓存认字不认人，拦不住降级文案，
    一旦钉进 60 天，同问全部命中"服务暂时不可用"。
    wait_for 超时与 _format_via_llm 内部异常同口径：工具结果已在手，
    超时只是格式化环节失败，应降级 _fallback_tool_view 而非整任务报错
    （2026-09-15 审查 B-12）。
    """
    prompt = _build_simple_prompt(user_query, agent_name, tool_result, persona_id, lang)
    file_ctx = await _load_file_ctx(file_ids, username)
    if file_ctx:
        prompt["system"] += f"\n\n{file_ctx}"
    try:
        answer = await asyncio.wait_for(
            _format_via_llm(
                api_key, prompt["system"], user_query, username=username, history=history,
            ),
            timeout=HTTP_TIMEOUT_MEDIUM,
        )
    except asyncio.TimeoutError:
        logger.warning(f"简单任务格式化超时，降级数据视图: tool={agent_name}")
        answer = None
    if answer is None:
        return _fallback_tool_view(tool_result), False
    return answer, True


async def _civil_simple_answer(user_query: str, username: str, persona_id: str,
                               lang: str, tool_result: dict, file_ids,
                               history: list | None) -> tuple[str, bool]:
    """民法典任务路径收口：有检索依据才调用模型，且回答必须通过引用溯源校验。

    cache_allowed 口径（2026-10-07 C1/P1-3）：拒答/服务降级/未过溯源校验的文案
    一律不入缓存——民事人格确定性通道的这类文案是"当前问题检索依据不足"，
    钉 60 天等于把一个瞬时缺口固化成永久答案。
    """
    from ..services.chat_support import get_deepseek_key
    from ..services.civil_grounding import (
        civil_tool_result, grounding_reply, validate_civil_answer,
    )
    if isinstance(tool_result, dict) and tool_result.get("mapping_hit"):
        return tool_result.get("message") or grounding_reply("no_evidence", lang), False
    if civil_tool_result(tool_result) is None:
        return grounding_reply("no_evidence", lang), False
    api_key = await get_deepseek_key()
    if not api_key:
        return grounding_reply("service_error", lang), False
    answer, _fmt_cache = await _format_simple_answer(
        api_key, user_query, username, persona_id, lang,
        "search_knowledge", tool_result, file_ids, history,
    )
    grounded, _ = validate_civil_answer(answer, tool_result)
    # 格式化降级视图不含条文依据，validate 必判 ungrounded；两处共 False 才放行
    return (answer if grounded else grounding_reply("ungrounded", lang)), (grounded and _fmt_cache)


async def _build_simple_answer(task_id: str, agent_name: str, user_query: str,
                               username: str, persona_id: str, lang: str,
                               tool_result, file_ids,
                               history) -> tuple[str, bool] | None:
    """单工具结果 → (最终回答, 可写缓存)；返回 None 表示已由 fail_task 收口，调用方直接返回。

    民法典人格走确定性通道（检索依据不足即拒答），其余走单次格式化调用。
    """
    if agent_name == "search_knowledge" and is_civil_persona(persona_id):
        return await _civil_simple_answer(
            user_query, username, persona_id, lang,
            tool_result, file_ids, history,
        )
    from ..core.task_manager import fail_task
    from ..services.chat_support import get_deepseek_key
    api_key = await get_deepseek_key()
    if not api_key:
        # 环境变量名不进任务状态（前端会原样展示）
        logger.error("DEEPSEEK_API_KEY 未设置")
        await fail_task(task_id, "服务配置不完整，请联系管理员")
        return None
    return await _format_simple_answer(
        api_key, user_query, username, persona_id, lang,
        agent_name, tool_result, file_ids, history,
    )


async def handle_simple_task(
    task_id: str,
    username: str,
    conversation_id: str,
    user_query: str,
    agent_name: str,
    persona_id: str = "",
    file_ids: Optional[List[str]] = None,
    cache_ctx: str | None = None,
    lang: str = "zh",
    # 知识库检索权限：默认仅公开（fail-closed）；None=不过滤只能由 admin 显式传入（P0 修复）。
    # 默认 () 而非 None/[]：理由见 runner.run_agent_task 同名参数（2026-09-07 审查 P2）
    user_perms: list | tuple | None = (),
):
    """简单任务处理（不走圆桌）：调单工具 → 单次 LLM 格式化 → 分块写 Redis → 存记忆/缓存"""
    from ..core.task_manager import (
        update_status, fail_task, is_cancelled_remote,
    )
    from ..core.memory_manager import MemoryManager

    conv_id = conversation_id or f"conv_{username}_{uuid.uuid4().hex[:12]}"
    mm = MemoryManager(username, conv_id, persona_id)

    try:
        if not await update_status(task_id, "generating"):
            return

        # 会话历史（2026-09-10 修复：本通道此前只写记忆不读，追问全部失忆）。
        # 先读后用：既注入格式化 LLM，也供 weather 无地点时查上下文城市
        history = await _safe_history(mm)

        if await is_cancelled_remote(task_id):
            return

        # ---- 1. 调用单个工具 ----
        tool_result = await _run_simple_tool(
            agent_name, user_query, username, user_perms, history,
            persona_id=persona_id,
        )
        if agent_name == "query_weather":
            from ..services.conversation_profiles import persist_weather_snapshot
            await persist_weather_snapshot(mm, tool_result)
        if await is_cancelled_remote(task_id):
            return

        # 工具结果只进日志。结果缓冲（result_buf）会被前端当回答正文原样展示，
        # 任何非正文文本写进去都会拼在最终回答里
        logger.info(f"简单任务工具返回: agent={agent_name}, keys={list(tool_result) if isinstance(tool_result, dict) else type(tool_result).__name__}")

        # ---- 2. 轻量 LLM 格式化回答（单次调用）----
        built = await _build_simple_answer(
            task_id, agent_name, user_query, username, persona_id, lang,
            tool_result, file_ids, history,
        )
        if built is None:
            return
        final_answer, cache_allowed = built
        if await is_cancelled_remote(task_id):
            return

        # ---- 3. 写入结果：DFA 检查 → 分块写 Redis → 状态/记忆/语义缓存 ----
        # 收口逻辑在 simple_task._deliver_simple_answer（2026-10-09 拆出，压 handle_simple_task 行数）
        if not await _deliver_simple_answer(
            mm, task_id, user_query, final_answer, cache_allowed,
            cache_ctx, agent_name, persona_id,
        ):
            return

    except Exception as e:
        logger.error(f"简单任务异常: {e}", exc_info=True)
        from ..core.safety_filter import sanitize_error_text
        # 异常 str 可能带完整 URL/Key，剥指纹后再进任务状态（前端原样展示）
        await fail_task(task_id, sanitize_error_text(str(e)))


def _extract_simple_args(agent_name: str, query: str) -> dict:
    """
    从查询中提取简单参数（关键词匹配，无需 LLM）

    地名语义走 place_extract 公共层（与快速通道同源：目的地=问题中的城市，
    出发地只作 route 起点）；本函数仅保留 hotel 预算 / food 菜系两类关键词扩展。
    """
    from ..core.place_extract import auto_tool_args
    args = auto_tool_args(agent_name, query)

    if agent_name == "query_hotel":
        # 提取预算关键词
        budget_kw = ["经济", "便宜", "实惠", "预算", "省钱"]
        luxury_kw = ["豪华", "五星", "高档", "贵", "奢侈"]
        if any(kw in query for kw in budget_kw):
            args["budget"] = "经济型"
        elif any(kw in query for kw in luxury_kw):
            args["budget"] = "豪华"

    elif agent_name == "query_food":
        # 提取菜系
        cuisine_kw = ["川菜", "粤菜", "湘菜", "火锅", "烧烤", "日料", "西餐", "中餐"]
        for c in cuisine_kw:
            if c in query:
                args["cuisine"] = c
                break

    return args


def _build_simple_prompt(query: str, agent_name: str, tool_result: dict,
                         persona_id: str = "", lang: str = "zh") -> dict:
    """
    构建简单任务的 system + user prompt
    不走多 Agent 讨论，直接让 LLM 基于工具结果回答问题
    """
    from ..core.persona_manager import get_persona_manager
    pm = get_persona_manager()
    # 按请求解析人格（原实现忽略 persona_id 直接用全局 current，并发下串人格）
    persona = pm.get_effective(persona_id)

    from ..core.constants import today_cn
    today_str = today_cn()
    # 2026-09-14 逐行审查去重：本地 try/except 变体与 chat_support.safe_format_prompt
    # 行为完全一致（缺占位符回退原文），统一走公共实现
    from ..services.chat_support import safe_format_prompt
    system = (
        safe_format_prompt(persona.system_prompt, today=today_str, name=persona.name)
        if persona else f"你是AI助手。今天是{today_str}。"
    )
    # 多语言指令：与流式路径 chat_support.lang_instruction 一致（P2：任务路径此前漏掉 lang）
    from ..services.chat_support import lang_instruction
    system += lang_instruction(lang)

    # 注入工具结果：与快路径共用不可信数据边界，不能只做旧式字符串替换。
    from ..core.safety_filter import format_untrusted_tool_context
    if agent_name == "search_knowledge" and is_civil_persona(persona_id):
        system += (
            f"\n\n你使用工具 [{agent_name}] 查询到了以下结果。\n\n"
            f"## 核心规则（必须遵守）\n"
            f"1. 只回答与用户问题最直接相关的一条或少数几条《民法典》法条，"
            f"不得为了面面俱到而堆砌全部相关法条\n"
            f"2. 只能使用工具返回的民法典依据，不得使用自身知识、常识、联网信息"
            f"或其他法律条文补全\n"
            f"3. 引用条文号必须出现在检索结果中；依据不足时明确说明无法依据知识库回答\n"
            f"4. 回答末尾以「依据:」列出实际引用的条文号\n\n"
            f"工具返回的数据：\n{format_untrusted_tool_context(tool_result)}"
        )
    else:
        system += (
            f"\n\n你使用工具 [{agent_name}] 查询到了以下结果。\n\n"
            f"## 核心规则（必须遵守）\n"
            f"1. **禁止编造**：只能基于工具返回的数据回答，不能编造任何具体数据（价格、距离、气温、评分等）\n"
            f"2. **先推荐再追问**：如果用户是求推荐，第一句就直接给推荐方案，不要反问\n"
            f"3. **数据不足时**：明确告诉用户哪些信息是工具提供的，哪些是估算的\n\n"
            f"工具返回的数据：\n{format_untrusted_tool_context(tool_result)}"
        )

    return {
        "system": system,
        "user": query
    }
