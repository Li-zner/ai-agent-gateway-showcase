"""中控台「问数」端点（P1 只读观测）：只编排，不含业务逻辑（业务在 data_ask）。

挂在 rag_admin_router 之下，因此继承路由级 require_admin（默认拒绝）；
端点内再显式判一次 role，沿用 2026-09-19 审查 F-P2-2 的双保险口径。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..core.audit import ACT_RAG_DATA_ASK, audit
from ..core.db_readonly import ReadonlyPoolNotConfigured
from ..middleware.auth import get_current_user
from ..services.data_ask import QUESTION_MAX_CHARS, ask_data
from ..services.data_ask_sql import ALLOWED_TABLES, SqlRejectedError

data_router = APIRouter(tags=["admin"])

# 批1 P1-9（2026-09-28）：history 会被原样拼进 messages，放在 system 提示之后、
# 本轮问题之前。放行 system/tool 等于让调用方追加一条系统消息改 SQL 阶段规则。
_HISTORY_ROLES = {"user", "assistant"}


class DataAskBody(BaseModel):
    """问数入参：问题必填，history 供多轮追问复用上一轮的表与口径。"""

    question: str = Field(max_length=QUESTION_MAX_CHARS)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=12)

    @field_validator("history")
    @classmethod
    def _history_roles(cls, v: list[dict[str, str]]) -> list[dict[str, str]]:
        for item in v:
            if item.get("role") not in _HISTORY_ROLES:
                raise ValueError("history 只接受 role=user/assistant")
        return v


@data_router.get("/data/tables")
async def data_tables(current_user: dict = Depends(get_current_user)) -> dict:
    """返回当前只读白名单表与用途说明（前端展示"能问什么"，也是排障入口）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return {"tables": [{"name": k, "usage": v} for k, v in ALLOWED_TABLES.items()]}


@data_router.post("/data/ask")
async def data_ask(body: DataAskBody,
                   current_user: dict = Depends(get_current_user)) -> dict:
    """自然语言查询白名单数据；返回 SQL + 结果行 + 模型结论。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question 不能为空")
    # 审计先落，失败也要留下"谁问过什么"；SQL 与行数在成功后补记
    await audit(current_user["username"], ACT_RAG_DATA_ASK,
                {"question_length": len(question)})
    try:
        result = await ask_data(question, body.history)
    except SqlRejectedError as e:
        # 守卫拒绝是可预期的业务结果（问题超纲 / 模型想写库），返回 422 让前端提示改写
        await audit(current_user["username"], ACT_RAG_DATA_ASK,
                    {"outcome": "rejected", "reason": str(e)[:200]})
        raise HTTPException(status_code=422, detail=f"查询被只读守卫拒绝: {e}")
    except ReadonlyPoolNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        # LLM 上游不可用 / 只读池连不上：外部依赖失败必须 5xx，不得伪装成空结果
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        # SQL 由模型产出，执行失败（列名写错、语法错）属上游产出质量问题：
        # 原因带回前端供操作者改写追问，但不能报成 200 空结果骗过界面
        raise HTTPException(status_code=502,
                            detail=f"查询执行失败: {type(e).__name__}: {str(e)[:200]}")
    await audit(current_user["username"], ACT_RAG_DATA_ASK,
                {"outcome": "ok", "row_count": result["row_count"],
                 "elapsed_ms": result["elapsed_ms"], "sql": result["sql"][:300]})
    return result
