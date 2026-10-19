"""流式对话共享上下文与入口门禁

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
ChatStreamCtx 承载一次 /v2/chat/stream 请求的全程可变状态；各编排模块
（fast_paths / react / core）通过它共享上下文与改写字段。
"""
import asyncio
import time  # noqa: F401 — 存量：L3 改用 day_window 后本模块已无直接使用，保留防隐式再导出
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import HTTPException

from ..models.schemas import ChatRequest
from ..core.config import (
    LLM_PRIMARY_MODEL, LLM_FALLBACK_MODEL, SUMMARY_THRESHOLD,
)
from ..core.logging import setup_logging
from ..core.metrics import gateway_requests_total
from ..core.memory_manager import MemoryManager
from ..core.persona_manager import get_persona_manager
from ..core.constants import today_cn
from ..core.quota import reserve_used_questions
from ..core.semantic_cache import SemanticCache, safe_set
from ..core.stream_utils import build_file_context
from ..core.redis import get_redis  # noqa: F401 — 存量保留（同 time）
from ..core.concurrency import spawn
from ..middleware.rate_limit import (
    check_qps, check_concurrent, get_daily_usage, release_concurrent,
    _get_limits,
)
from ..agents.memory import compress_message_history
from ..agents.orchestrator import build_shared_context
from .chat_support import lang_instruction, safe_format_prompt, set_request_lang
from .rag_request_trace import finish_request, record_span, set_request_identity

logger = setup_logging()

# 兜底截断阈值（2026-09-25 修登记 bug）：原先 30 与 get_context 读取上限
# SUMMARY_THRESHOLD=30 相等，compress_message_history 恒 no-op，滚动摘要失败时
# 旧轮无任何兜底。改 20 对齐 runner.py 任务链路 09-22 拍板：留 10 条压缩余量，
# 让第 11 轮起的旧轮真正折叠进【历史摘要】，与 LLM 滚动摘要层互为兜底。
_HISTORY_COMPACT_MSGS = 20


@dataclass
class ChatStreamCtx:
    """一次 /v2/chat/stream 请求的共享可变状态"""
    req: ChatRequest
    username: str
    user_role: str
    user_perms: Optional[List[str]]
    today: str
    conv_id: str
    mm: MemoryManager
    user_profile: str
    cache_ctx: str
    lang_instr: str
    current_user: dict     # 原始 user 字典（供透传 / 权限判断）
    # ---- 以下由 build_stream_ctx 填充 ----
    persona: object = None
    persona_id: str = ""
    selected_model: str = LLM_PRIMARY_MODEL
    model_try_list: List[str] = field(default_factory=lambda: [LLM_PRIMARY_MODEL])
    messages: List[Dict] = field(default_factory=list)
    user_query: str = ""
    tools: List = field(default_factory=list)
    matched_agents: List[str] = field(default_factory=list)
    is_simple: bool = True
    is_recommend: bool = False
    api_key: str = ""
    today_str: str = ""
    # ---- 运行时可变状态 ----
    civ_civil_mapped_query: Optional[str] = None
    civil_route_match: Optional[str] = None
    rebuild_lock: Optional[str] = None
    rebuild_renew_task: Optional[asyncio.Task] = None
    concurrent_renew_task: Optional[asyncio.Task] = None
    partial_answer: str = ""
    saved_normally: bool = False
    finished: bool = False
    has_history: bool = False
    # 本轮注入的文件上下文长度（L2：同会话文件内容只计费一次的扣除依据）
    file_ctx_chars: int = 0
    concurrent_lease: str = ""
    # 旅行槽位填充：pending 保存待补信息，clear 标记本轮消费后收尾清除。
    travel_pending: Dict = field(default_factory=dict)
    travel_waiting_slot: bool = False
    travel_pending_clear: bool = False


async def ensure_chat_allowed(current_user: dict) -> str:
    """角色分级限流 + 并发控制 + 日配额 + 试用额度门禁；返回 today 日期串

    任一门槛不过抛 HTTPException（由路由层转响应）；通过后调用方负责
    在 finally 释放并发槽位（在 chat_generate 的 cleanup 中完成）。
    """
    username = current_user["username"]
    user_role = current_user.get("role", "user")
    # 业务日（2026-09-28 L3 统一）：上海 05:00 切日，daily_* 键与 TTL 同一口径
    # （day_window 单一真相源；此前 UTC 切日对中文用户在北京 08:00 重置，语义失真）
    from ..core.day_window import quota_day
    today = quota_day()

    if not await check_qps(username, user_role):
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    lease = await check_concurrent(username, user_role)
    if lease is None:
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="并发请求过多，请稍后再试")
    current_user["_concurrent_lease"] = lease

    usage = await get_daily_usage(username, today)
    # 角色限额是唯一权威源，禁止 SSE/Task 两条路径各维护一份默认阈值。
    limits = _get_limits(user_role)
    if usage["request_count"] >= limits["daily_req"]:
        await release_concurrent(username, lease)
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="今日请求次数已达上限")
    if usage["token_sum"] >= limits["daily_token"]:
        await release_concurrent(username, lease)
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='429').inc()
        raise HTTPException(status_code=429, detail="今日 Token 消耗已达上限")
    # 三态（2026-09-14 审计 P1）：额度耗尽 402；DB 故障 503——不再把服务故障
    # 误报成"免费额度已用尽"。fail-closed 语义不变，只是状态码如实区分。
    from ..core.quota import QuotaDependencyError
    try:
        _reserved = await reserve_used_questions(current_user)
    except QuotaDependencyError:
        await release_concurrent(username, lease)
        gateway_requests_total.labels(method='POST', endpoint='/v2/chat/stream', status='503').inc()
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后再试")
    if not _reserved:
        await release_concurrent(username, lease)
        raise HTTPException(status_code=402, detail="免费额度已用完，请绑定手机号后继续使用")
    return today


def _build_system_prompt(req: ChatRequest, today_str: str):
    """解析人格与模型链，返回 (persona, persona_id, selected_model, model_try_list, system_content)"""
    pm = get_persona_manager()
    # 按请求解析人格，不改全局 current（并发用户互不覆盖，P1）
    persona = pm.get_effective(req.persona_id)
    persona_id = persona.id if persona else (req.persona_id or pm.current_id)
    if persona:
        system_content = safe_format_prompt(persona.system_prompt, today=today_str, name=persona.name)
        selected_model = persona.model or LLM_PRIMARY_MODEL
    else:
        system_content = f"你是AI助手。今天是{today_str}。"
        selected_model = LLM_PRIMARY_MODEL
    model_try_list = [selected_model]
    if LLM_FALLBACK_MODEL != selected_model:
        model_try_list.append(LLM_FALLBACK_MODEL)
    return persona, persona_id, selected_model, model_try_list, system_content


async def _assemble_messages(req: ChatRequest, mm: MemoryManager, system_content: str,
                             username: str = "") -> tuple[list, int]:
    """组装 system/user 消息：人格 + 共享上下文 + 文件上下文 + 历史压缩 + 最新消息提示

    返回 (messages, file_ctx_chars)：注入的文件上下文长度单独带出，供计费层做
    "同会话文件只计一次"的扣除估算（L2，2026-09-28 主人拍板）。
    """
    messages = [{"role": "system", "content": system_content}]
    file_context_str = await build_file_context(req, username)
    file_ctx_chars = len(file_context_str) if file_context_str else 0
    if file_context_str:
        messages.append({"role": "system", "content": file_context_str})
    history_dicts = await mm.get_context(limit=SUMMARY_THRESHOLD)
    history_dicts = compress_message_history(history_dicts, max_messages=_HISTORY_COMPACT_MSGS)
    for msg in history_dicts:
        messages.append(msg)
    messages.append({
        "role": "system",
        "content": "注意：请以用户最新的消息为准。如果用户改变了目的地、预算、人数等计划，立即按新信息回答，不要沿用旧信息。"
    })
    return messages, file_ctx_chars


def _rewrite_user_query(user_query: str, persona_id: str) -> str:
    """保留查询改写扩展点；民法典领域裁决由硬路由和法域映射统一处理。

    旧实现会在用户问题前注入消保法、劳动法等提示词，诱导模型引用非民法典内容。
    该逻辑已移除，避免模型绕开“只依据民法典回答”的产品契约。
    """
    return user_query


async def build_stream_ctx(req: ChatRequest, current_user: dict, today: str) -> ChatStreamCtx:
    """构建共享上下文：会话/画像/缓存上下文/人格/消息/用户查询改写"""
    username = current_user["username"]
    user_role = current_user.get("role", "user")
    user_perms = None if user_role == "admin" else (current_user.get("permissions") or [])

    lang_instr = lang_instruction(req)
    today_str = today_cn()
    # 无会话 id 时用 uuid 后缀（2026-09-07 审查 P2）：秒级时间戳同秒并发会共用
    # 会话导致历史互相污染
    conv_id = req.conversation_id or f"conv_{username}_{uuid.uuid4().hex[:12]}"
    # 人格/模型链先解析：缓存上下文要带真实人格与所选模型维度（2026-09-19 审查 core P2-5）；
    # 也早于 MemoryManager 构造——画像写入口按人格排除评测流量（批1 B-P1-3）
    persona, persona_id, selected_model, model_try_list, system_content = _build_system_prompt(req, today_str)
    mm = MemoryManager(username, conv_id, persona_id)
    try:
        user_profile = await mm.get_profile()
    except Exception as _pf_err:
        logger.warning(f"画像加载失败（按无画像处理）: {_pf_err}")
        user_profile = ""
    # 独立首轮问答允许跨会话复用；有历史时上层会跳过缓存，避免上下文串答。
    # user_perms 进键（2026-09-28 L5）：升级 vip 后不再命中旧公开权限回答
    # 先登记界面语言：兜底/安全这类答案级文案的无 lang 吐点据此切换中/英。
    set_request_lang(req)
    _cache_ctx = SemanticCache.build_cache_ctx(
        username, persona_id, user_profile, "",
        user_query=req.query, lang=getattr(req, "lang", "") or "",
        model=selected_model, user_perms=user_perms,
    )

    ctx = ChatStreamCtx(
        req=req, username=username, user_role=user_role, user_perms=user_perms,
        today=today, conv_id=conv_id, mm=mm, user_profile=user_profile,
        cache_ctx=_cache_ctx, lang_instr=lang_instr, current_user=current_user,
        today_str=today_str,
        concurrent_lease=current_user.get("_concurrent_lease", ""),
    )

    ctx.persona, ctx.persona_id = persona, persona_id
    ctx.selected_model, ctx.model_try_list = selected_model, model_try_list
    set_request_identity(
        persona=persona_id,
        selected_model=selected_model,
        provider="qwen" if selected_model.startswith("qwen") else "deepseek",
    )

    shared_context = build_shared_context(user_profile or "")
    system_content += f"\n{shared_context}"
    system_content += lang_instr

    ctx.messages, ctx.file_ctx_chars = await _assemble_messages(
        req, mm, system_content, username)
    ctx.has_history = any(
        msg.get("role") in ("user", "assistant") for msg in ctx.messages
    )
    ctx.user_query = _rewrite_user_query(req.query, persona_id)
    return ctx


async def finalize_answer(ctx: ChatStreamCtx, answer: str, write_cache: bool = True) -> None:
    """保存对话并累计用量（generate 内多处 finalize 尾的统一封装）

    行为等价：save_messages + 可选写语义缓存 + update_daily_usage。
    write_cache=False 用于 max_steps 截断的不完整回答（Bug #2：残缺答案禁止入缓存）。

    2026-09-14 逐行审查加固：全部 10 个调用点都在回答内容（含 answer_complete/[DONE]）
    送达之后才调本函数——台账收口失败（Redis/PG 抖动）绝不能让上层再降级出第二段
    回答（C-F11 缺陷族的单点根治）。故整体 try/except：失败记 warning，不影响用户。
    """
    from ..middleware.rate_limit import update_daily_usage
    try:
        # 答案侧监测采集（2026-09-13 MVP）：所有 finalize 路径（生成/缓存/法条拦截）
        # 都过此处，是「输入→检索→输出」链输出侧的唯一收口。失败静默不影响主流程。
        try:
            from .answer_trace import record_answer
            record_answer(ctx.user_query or ctx.req.query, answer, ctx.username,
                          persona=ctx.persona_id or "default")  # R2 三处之一：空 persona 不得落 civil_code（评测集污染）
        except Exception as e:  # 监测采集绝不影响回答主流程，但留痕可查
            import logging
            logging.getLogger(__name__).debug(
                f"答案 trace 采集失败（不影响主流程）: {type(e).__name__}: {e}")
        try:
            record_span(
                "answer",
                result_count=len(answer or ""),
                attributes={"answer_len": len(answer or "")},
            )
            finish_request(status="success")
        except Exception as e:
            logger.debug(f"请求级 trace 收尾失败（不影响主流程）: {type(e).__name__}: {e}")
        await ctx.mm.save_messages(
            {"role": "user", "content": ctx.req.query},
            {"role": "assistant", "content": answer},
        )
        if ctx.travel_pending_clear and hasattr(ctx.mm, "clear_pending_travel"):
            await ctx.mm.clear_pending_travel()
        if write_cache and not ctx.has_history:
            # safe_set 兜底：后台任务异常不能无人认领（与 runner/router 同一封装）
            spawn(safe_set(ctx.req.query, answer, cache_ctx=ctx.cache_ctx), name="semantic-cache-set")
        await update_daily_usage(ctx.username, ctx.today, inc_request=1, inc_token=0)
    except Exception as fin_err:
        # 台账失败只告警：此刻回答已送达前端，重抛会触发上层降级出重复回答
        logger.warning(
            f"finalize 台账收口失败（回答已送达，历史/用量可能缺失）: "
            f"{type(fin_err).__name__}: {fin_err}")
