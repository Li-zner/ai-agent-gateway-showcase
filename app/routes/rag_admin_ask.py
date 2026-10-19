"""中控台「运维问答」端点：/ask 与 /ask/stream（监测摘要 + 问数并入的自然语言窗口）。

从 rag_admin.py 拆出（2026-10-04，文件 600 行门禁）。挂在 rag_admin_router 之下，
因此继承路由级 require_admin（默认拒绝）；端点内再显式判一次 role，沿用
2026-09-19 审查 F-P2-2 的双保险口径。
"""
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core.audit import ACT_RAG_ASK, audit
from ..core.logging import setup_logging
from ..middleware.auth import get_current_user
from .rag_admin_status import _fetch_status

ask_router = APIRouter(tags=["admin"])

logger = setup_logging()


class RagAskMessage(BaseModel):
    """运维会话历史消息，限制长度和角色集合。"""

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class RagAskBody(BaseModel):
    question: str
    history: list[RagAskMessage] = Field(default_factory=list, max_length=10)


# 回答形态（2026-10-03 业主定稿）：不设格式模板——此前钉三段结构被反馈"太生硬"，
# 输出形式完全放开。只保留任务引导：事件类问题按 读事件→析因→修复建议 推进；
# 数据类问题（问数已并入本助手）优先用注入的实查结果回答。
_ASK_SYSTEM = (
    "你是 RAG 检索系统的运维助手，同时负责数据问数，语气像坐在旁边的资深同事："
    "先回应问题本身，自然作答，不套模板、不设固定段落、不复读格式。"
    "处理事件类问题时：先读提问中编号对应的事件，然后分析其可能存在的原因，"
    "最后对怎么修复给出一些建议。"
    "处理数据类问题时：优先依据下方「数据查询结果」回答；没有查询结果且监测数据里"
    "也没有的信息，直接说明而不是编造。"
    "所有回答使用简体中文；request_uid、chunk_key 等技术标识符保留原样。"
)


def _ask_user_content(context: str, question: str, data_context: str = "") -> str:
    """/ask 与 /ask/stream 共用的用户消息；data_context 非空时注入问数实查结果"""
    parts = [f"监测数据：\n{context}"]
    if data_context:
        parts.append(f"数据查询结果（只读库实查）：\n{data_context}")
    parts.append(f"管理员提问：{question}")
    return "\n\n".join(parts)


# 疑似查数问题的关键词启发式（ponytail:朴素天花板——误报多花两次 LLM 调用，
# 漏报退回监测快照作答；升级路径 = 意图分类小模型）
_DATA_HINT_RE = re.compile(
    r"多少|几[个条天单次笔名只]|统计|查[一询]?下|排行|趋势|占比|比率|率|"
    r"GMV|gmv|余额|金额|订单|用户数|合计|总数|总量|平均|最高|最低|最多|最少|"
    r"前\s*\d|最近\s*\d|近\s*\d|哪[个些一].*(多|高|大|少)")


async def _data_enrichment(question: str, history: list[dict]) -> str:
    """问数并入运维助手（2026-10-03）：疑似查数问题时先跑只读问数管线
    （NL->守卫SQL->只读执行），把实查 SQL 与行注入上下文供模型自由作答。

    任何失败（守卫拒绝/执行报错/上游不可用）都静默降级为空串，
    不阻塞诊断问答的主链路。
    """
    if not _DATA_HINT_RE.search(question):
        return ""
    try:
        # 出云脱敏（2026-10-03）：不走 ask_data 完整管线——它的 answer 生成
        # 环节会把未脱敏行再送一次云端。这里只取 生成SQL→只读执行→列脱敏，
        # 解读交给诊断助手本体；data_ask 自带的列脱敏（_redact_rows）生效。
        import json as _json
        from ..core.db_readonly import run_readonly_query
        from ..services.data_ask import (
            _dedupe_columns,
            _json_safe,
            _redact_rows,
            generate_sql,
            load_schema,
        )
        from ..services.data_ask_sql import MAX_ROWS, with_row_limit
        schema = await load_schema()
        sql = await generate_sql(question, (history or [])[-6:], schema)
        records = await run_readonly_query(with_row_limit(sql, MAX_ROWS))
        if not records:
            return _json.dumps({"sql": sql, "rows": [], "row_count": 0},
                               ensure_ascii=False)
        columns = _dedupe_columns(records[0].keys())
        rows = [dict(zip(columns, (_json_safe(v) for v in r.values())))
                for r in records]
        columns, rows = _redact_rows(columns, rows)
        return _json.dumps({
            "sql": sql, "columns": columns, "rows": rows[:20],
            "row_count": len(rows)}, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — 问数失败只降级，不让整条问答 5xx
        return ""


async def _build_ask_messages(status, question: str, history) -> list:
    """构造 /ask 与 /ask/stream 共用的问答消息序列（监测摘要瘦身 + 问数并入）。

    两入口此前逐行重复这段上下文装配，改协议时易漏改另一侧，故收敛于此。
    """
    # 结构化瘦身（公共实现见 rag_monitor._slim_status）：字符硬截会把 JSON
    # 截成非法片段；明细限 10 条 + evidence 截短 + 丢 patch 全文
    import json as _json
    from ..services.rag_monitor import _slim_status
    context = _json.dumps(_slim_status(status), ensure_ascii=False)
    history_messages = [{"role": item.role, "content": item.content}
                        for item in history]
    # 问数并入（2026-10-03）：疑似查数问题先实查只读库，结果注入上下文
    data_context = await _data_enrichment(question, history_messages)
    return [
        {"role": "system", "content": _ASK_SYSTEM},
        *history_messages,
        {"role": "user", "content": _ask_user_content(context, question, data_context)},
    ]


@ask_router.post("/ask")
async def rag_ask(body: RagAskBody, current_user: dict = Depends(get_current_user)):
    """向 LLM 提问 RAG 运行状况（自动注入实时监测摘要）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    question = (body.question or "").strip()[:500]
    if not question:
        raise HTTPException(status_code=422, detail="question 不能为空")
    await audit(
        current_user["username"],
        ACT_RAG_ASK,
        {"question_length": len(question)},
    )
    try:
        status = await _fetch_status()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"监测数据不可用: {type(e).__name__}")

    messages = await _build_ask_messages(status, question, body.history)
    try:
        from ..services.chat_support import post_chat_completion
        from ..core.config import HTTP_TIMEOUT_MEDIUM
        # max_tokens=2000 + MEDIUM 超时：flash 模型先输出 reasoning_content，
        # SHORT(5s) 下完整监测摘要的推理跑不完（冒烟实测 502），预算与超时都留足
        ok, data = await post_chat_completion(
            {"model": None, "messages": messages,
             "temperature": 0.2, "max_tokens": 2000},
            timeout=HTTP_TIMEOUT_MEDIUM)
        if not ok:
            raise HTTPException(status_code=502, detail="上游 LLM 不可用")
        answer = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not answer.strip():
            raise HTTPException(status_code=502, detail="LLM 返回空内容，请重试或缩小问题范围")
        return {"answer": answer, "status_snapshot": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"问询失败: {type(e).__name__}")


@ask_router.post("/ask/stream")
async def rag_ask_stream(body: RagAskBody,
                         current_user: dict = Depends(get_current_user)):
    """运维会话流式版（P2-1）：SSE 逐段输出，协议对齐主聊天
    （answer_chunk/answer_complete/[DONE]，thought 透传但不承载业务语义）。

    上下文构造与审计口径与 /ask 完全一致；/ask 保留作为非流式兼容入口。
    与 /ask 的已知差异：不做 flash 模型降级——主模型失败以 answer_error
    事件送达，由操作者重试。
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    question = (body.question or "").strip()[:500]
    if not question:
        raise HTTPException(status_code=422, detail="question 不能为空")
    await audit(
        current_user["username"],
        ACT_RAG_ASK,
        {"question_length": len(question)},
    )
    try:
        status = await _fetch_status()
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"监测数据不可用: {type(e).__name__}")

    messages = await _build_ask_messages(status, question, body.history)

    async def _events():
        from ..core.config import LLM_PRIMARY_MODEL
        from ..core.stream_utils import sse, stream_llm
        from ..services.chat_support import get_deepseek_key
        collected = []
        try:
            # stream_llm 不对空 key 兜底（llm_endpoint 原样透传），
            # 必须走带健康度轮询的 key 池，与主聊天流同源。
            api_key = await get_deepseek_key()
            async for event in stream_llm(
                    api_key, LLM_PRIMARY_MODEL, messages,
                    temperature=0.2, max_tokens=2000,
                    username=current_user.get("username", "")):
                if event.get("type") == "reasoning":
                    yield sse("thought", event.get("text") or "")
                elif event.get("type") == "content":
                    text = event.get("text") or ""
                    collected.append(text)
                    yield sse("answer_chunk", text)
            answer = "".join(collected).strip()
            if not answer:
                logger.warning(
                    f"运维问答流式返回空内容: user={current_user.get('username')}, "
                    f"question_len={len(question)}")
                yield sse("answer_error", "LLM 返回空内容，请重试或缩小问题范围")
            else:
                yield sse("answer_complete", answer)
        except Exception as e:
            # 流已开始、200 状态码已提交，异常只能经 answer_error 事件送达；
            # 服务端必须留痕，否则运维侧对 key 池/上游故障完全无感（同主聊天流口径）
            logger.warning(
                f"运维问答流式失败: user={current_user.get('username')}, "
                f"err={type(e).__name__}", exc_info=True)
            yield sse("answer_error", f"问询失败: {type(e).__name__}")
        yield "data: [DONE]\n\n"

    # 防缓冲头：不依赖前置 nginx/Caddy 的 proxy_buffering off，端点自身即时逐段下发
    return StreamingResponse(
        _events(), media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
