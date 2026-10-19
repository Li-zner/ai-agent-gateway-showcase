"""RAG 监测管理端接口（仅管理员）—— 人工介入窗口 + LLM 健康问询。

MVP（2026-09-13）：
- GET  /api/admin/rag/status：近 24h 诊断结论统计 + 明细 + 检索/答案侧量级，
  异常定位（evidence/action/patch）直接可见，供人工介入决策
- POST /api/admin/rag/ask：把实时监测摘要注入上下文，由 LLM 回答运维提问
  （"现在检索健康吗""哪类异常最多""该不该介入"）——监测数据的自然语言窗口
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.audit import ACT_ADMIN, ACT_RAG_CONTENT_VIEW, audit
from ..middleware.auth import get_current_user, require_admin
from ..services.rag_request_content import (
    fetch_request_content,
    request_content_payload,
)
from .rag_admin_actions import actions_router
from .rag_admin_ask import (  # ask 端点已拆出；非 router 项为兼容旧导入路径的再导出
    _ASK_SYSTEM,
    _ask_user_content,
    RagAskBody,
    RagAskMessage,
    ask_router,
    rag_ask,
)
from .rag_admin_changesets import changeset_router
from .rag_admin_data import data_router
from .rag_admin_eval import eval_router
from .rag_admin_status import (
    _as_json,
    _fetch_request_detail,
    _fetch_requests,
    _fetch_status,
    _load_latest_evaluation,
    effective_config,
)

# 路由级门禁（2026-09-19 审查 F-P2-2）：整个 /api/admin/rag 分区默认要求 admin，
# 含后面 include 的 changeset_router / eval_router。端点内的 role 判断保留，
# 作用是让「新增端点忘写鉴权」从静默越权变成不可能。
rag_admin_router = APIRouter(prefix="/api/admin/rag", tags=["admin"],
                             dependencies=[Depends(require_admin)])


@rag_admin_router.get("/status")
async def rag_status(
        include_eval: bool = Query(default=False),
        current_user: dict = Depends(get_current_user)):
    """RAG 监测概览：近 24h 诊断结论 + 检索/答案量级（人工介入窗口）。
    include_eval=1 时把评测人格流量算回统计（默认排除）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    try:
        return await _fetch_status(include_eval)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"监测数据不可用: {type(e).__name__}")

@rag_admin_router.get("/effective-config")
async def rag_effective_config(current_user: dict = Depends(get_current_user)):
    """当前生效配置 + 来源（默认/env/运行期覆盖）——口径漂移的在线唯一真相（2026-09-30）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return await effective_config()

@rag_admin_router.get("/requests")
async def rag_requests(
        limit: int = Query(default=50, ge=1, le=200),
        status: str | None = Query(default=None, max_length=32),
        persona: str | None = Query(default=None, max_length=64),
        include_eval: bool = Query(default=False),
        current_user: dict = Depends(get_current_user)):
    """请求级 trace 列表（管理员）。默认不含评测流量，include_eval=1 计入。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    try:
        return {"items": await _fetch_requests(limit, status, persona, include_eval)}
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"请求 trace 不可用: {type(exc).__name__}")


@rag_admin_router.get("/requests/{request_uid}")
async def rag_request_detail(
        request_uid: uuid.UUID,
        current_user: dict = Depends(get_current_user)):
    """单请求完整阶段时间线（管理员）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    try:
        result = await _fetch_request_detail(request_uid)
        await audit(
            current_user["username"],
            ACT_RAG_CONTENT_VIEW,
            {
                "request_uid": str(request_uid),
                "span_count": len(result["spans"]),
                "chunk_count": len(result["content"]["chunks"]),
            },
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"请求 trace 不可用: {type(exc).__name__}")


@rag_admin_router.get("/incidents")
async def rag_incidents(
        limit: int = Query(default=50, ge=1, le=200),
        status: str | None = Query(default=None, max_length=32),
        severity: str | None = Query(default=None, max_length=16),
        current_user: dict = Depends(get_current_user)):
    """incident 列表（管理员）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    try:
        from ..core.db import get_pool
        from ..services.rag_incidents import list_incidents
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            items = await list_incidents(
                conn, limit=limit, status=status, severity=severity)
        return {"items": items}
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"incident 不可用: {type(exc).__name__}")


@rag_admin_router.get("/incidents/{incident_id}")
async def rag_incident_detail(
        incident_id: int,
        current_user: dict = Depends(get_current_user)):
    """incident 详情与关联 verdict（管理员）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_incidents import get_incident
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        item = await get_incident(conn, incident_id)
    if item is None:
        raise HTTPException(status_code=404, detail="incident 不存在")
    return item


@rag_admin_router.get("/incidents/{incident_id}/recommendations")
async def rag_incident_recommendations(
        incident_id: int,
        current_user: dict = Depends(get_current_user)):
    """返回 incident 的确定性修复建议，不直接创建动作。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_recommendations import recommend_incident_actions
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        items = await recommend_incident_actions(conn, incident_id)
    return {"items": items}


@rag_admin_router.post("/incidents/{incident_id}/ack")
async def rag_incident_ack(
        incident_id: int,
        current_user: dict = Depends(get_current_user)):
    """认领 incident。"""
    return await _transition_incident_endpoint(
        incident_id, "acknowledged", current_user)


@rag_admin_router.post("/incidents/{incident_id}/resolve")
async def rag_incident_resolve(
        incident_id: int,
        current_user: dict = Depends(get_current_user)):
    """解决 incident。"""
    return await _transition_incident_endpoint(
        incident_id, "resolved", current_user)


@rag_admin_router.post("/incidents/clear")
async def rag_incidents_clear(
        current_user: dict = Depends(get_current_user),
        stale_hours: int = Query(default=24, ge=24, le=720)):
    """超管批量清理：静默超过 stale_hours 的活跃事件标记为已解决。

    只改状态不删行（日志永久留档），下限 24h 防止正在爆发的事件被清空；
    清理后若同一规则再触发，会自动开出新事件，不会被永久静音。

    已知上限（2026-09-22 审查 P2，本批未清零）：候选 SELECT 无 LIMIT 且逐条
    各自提交，积压大时会长时间独占一条 pool 连接。LIMIT 200 只能加在 SELECT 侧
    （services/rag_incidents.clear_stale_incidents 的 fetch），而该文件不在本批
    可改范围内——在路由里重抄一遍 SELECT 会让两处口径分叉，故此处只登记上限。
    分页口径已就绪：本端点按 incident_id 升序清理并返回 cleared_count，
    调用方按"cleared_count == 批次上限就再点一次"即可把积压分批排干。
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_incidents import clear_stale_incidents
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        cleared = await clear_stale_incidents(
            conn, owner=current_user.get("username", ""),
            stale_hours=stale_hours)
    # 操作人、时间由 audit 落库，事件 id 列表进 detail，事后可追责
    await _audit_admin(current_user, "incidents_clear",
                       cleared_ids=[c["incident_id"] for c in cleared],
                       stale_hours=stale_hours)
    return {"cleared": cleared, "cleared_count": len(cleared)}


async def _transition_incident_endpoint(incident_id: int, target: str,
                                        current_user: dict) -> dict:
    """执行 incident 状态迁移并统一错误处理。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_incidents import transition_incident
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        item = await transition_incident(
            conn, incident_id, status=target,
            owner=current_user.get("username", ""))
    if item is None:
        raise HTTPException(status_code=409,
                            detail="incident 当前状态不允许该操作")
    await _audit_admin(current_user, "incident_transition",
                       incident_id=incident_id, target_status=target)
    return item


async def _audit_admin(current_user: dict, operation: str, **detail) -> None:
    """统一记录 RAG 控制面写操作，失败不反噬已提交业务事务。"""
    await audit(
        current_user.get("username", ""),
        ACT_ADMIN,
        {"operation": operation, **detail},
    )


@rag_admin_router.get("/actions")
async def rag_actions(
        limit: int = Query(default=50, ge=1, le=200),
        status: str | None = Query(default=None, max_length=32),
        incident_id: int | None = Query(default=None, ge=1),
        current_user: dict = Depends(get_current_user)):
    """修复动作列表（管理员）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_actions import list_actions
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        items = await list_actions(
            conn, limit=limit, status=status, incident_id=incident_id)
    return {"items": items}


@rag_admin_router.get("/actions/{action_id}")
async def rag_action_detail(
        action_id: int,
        current_user: dict = Depends(get_current_user)):
    """单条修复动作详情（管理员）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_actions import get_action
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        item = await get_action(conn, action_id)
    if item is None:
        raise HTTPException(status_code=404, detail="动作不存在")
    return item


@rag_admin_router.post("/incidents/{incident_id}/actions")
async def rag_plan_action(
        incident_id: int,
        body: "RagActionPlanBody",
        current_user: dict = Depends(get_current_user)):
    """为 incident 创建白名单动作；L1 默认等待审批。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    if len(str(body.params)) > 8192:
        raise HTTPException(status_code=413, detail="动作参数过大")
    from ..core.db import get_pool
    from ..services.rag_actions import (
        IncidentNotActionableError,
        IncidentNotFoundError,
        create_action,
    )
    pool = await get_pool()
    try:
        async with pool.acquire(timeout=5) as conn:
            async with conn.transaction():
                action = await create_action(
                    conn, incident_id=incident_id, action_key=body.action_key,
                    params=body.params, reason=body.reason,
                    requested_by=current_user.get("username", ""))
        await _audit_admin(
            current_user, "rag_action_create", incident_id=incident_id,
            action_id=action["action_id"], action_key=action["action_key"],
            status=action["status"])
        return action
    except IncidentNotFoundError:
        raise HTTPException(status_code=404, detail="incident 不存在")
    except IncidentNotActionableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"动作创建失败: {type(exc).__name__}")


@rag_admin_router.post("/actions/{action_id}/approve")
async def rag_approve_action(
        action_id: int,
        current_user: dict = Depends(get_current_user)):
    """批准动作并送入执行队列。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_actions import approve_action
    from ..services.rag_policy import PolicyRejectedError
    pool = await get_pool()
    try:
        async with pool.acquire(timeout=5) as conn:
            item = await approve_action(
                conn, action_id, current_user.get("username", ""))
    except PolicyRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=409, detail="动作当前不允许审批")
    await _audit_admin(current_user, "rag_action_approve",
                       action_id=action_id, status=item["status"])
    return item


@rag_admin_router.post("/actions/{action_id}/cancel")
async def rag_cancel_action(
        action_id: int,
        current_user: dict = Depends(get_current_user)):
    """取消尚未执行的动作。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_actions import cancel_action
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        item = await cancel_action(
            conn, action_id, current_user.get("username", ""))
    if item is None:
        raise HTTPException(status_code=409, detail="动作当前不允许取消")
    await _audit_admin(current_user, "rag_action_cancel",
                       action_id=action_id, status=item["status"])
    return item


@rag_admin_router.post("/actions/{action_id}/rollback")
async def rag_rollback_action(
        action_id: int,
        current_user: dict = Depends(get_current_user)):
    """人工回滚已成功或已失败、且支持回滚的动作（failed 带真快照时可重试回滚）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..core.db import get_pool
    from ..services.rag_actions import rollback_action
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        item = await rollback_action(
            conn, action_id, current_user.get("username", ""))
    if item is None:
        raise HTTPException(status_code=409, detail="动作当前不允许回滚")
    await _audit_admin(current_user, "rag_action_rollback",
                       action_id=action_id, status=item["status"])
    return item


class RagActionCheckBody(BaseModel):
    """人工核对终结的说明；长度与 reason 同档。"""

    note: str = Field(..., min_length=1, max_length=500)


@rag_admin_router.post("/actions/{action_id}/check")
async def rag_check_action(
        action_id: int,
        body: RagActionCheckBody,
        current_user: dict = Depends(get_current_user)):
    """人工核对后终结执行失败的动作（只翻状态与留痕，不改动任何业务数据）。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=400,
                            detail="请填写核对说明：终结等于由人来担保数据状态")
    from ..core.db import get_pool
    from ..services.rag_actions import check_action
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        item = await check_action(
            conn, action_id, current_user.get("username", ""), note)
    if item is None:
        raise HTTPException(status_code=409, detail="仅执行失败的动作可标记为已核对")
    await _audit_admin(current_user, "rag_action_check",
                       action_id=action_id, status=item["status"], note=note)
    return item


@rag_admin_router.post("/actions/run-once")
async def rag_run_actions_once(
        current_user: dict = Depends(get_current_user)):
    """管理员手动触发一次动作 worker，便于演练和排障。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    from ..services.rag_actions import execute_queued_actions
    executed = await execute_queued_actions()
    await _audit_admin(current_user, "rag_action_run_once", executed=executed)
    return {"executed": executed}


# L2 变更集路由（列表/详情/提案）已拆至 rag_admin_changesets.py —— 拆分原因见该
# 模块头。挂载点刻意留在原路由位置，保持注册顺序与全部 URL 不变
# （子 router 不带 prefix，前缀由本 router 提供）。
rag_admin_router.include_router(changeset_router)

# 离线测评报告（列表 / 明细）见 rag_admin_eval.py：把 app/rag_eval/reports/ 下的
# 检索测评与端到端评分暴露成只读接口，供中控台"测评"分区直接查看。
rag_admin_router.include_router(eval_router)

# 只读问数（P1）：自然语言 -> SQL -> 只读角色执行。子路由不带 prefix，前缀由本
# router 提供（/api/admin/rag/data/*），因此同样继承上面的路由级 require_admin。
rag_admin_router.include_router(data_router)

# 业务写动作提议（P2）：不挂事件的独立入口，只创建待审批动作，执行仍走 worker。
rag_admin_router.include_router(actions_router)

# 运维问答（/ask 与 /ask/stream）已拆至 rag_admin_ask.py（600 行门禁）；子路由不带
# prefix，前缀由本 router 提供，同样继承上面的路由级 require_admin。
rag_admin_router.include_router(ask_router)

# DB Agent 已于 2026-10-03 拆出独立挂载（main.py，前缀不变）：按能力授权
# （capabilities.py）而非整体 admin——子账号由 permissions 允许清单开通。


@rag_admin_router.get("/daily-report")
async def rag_daily_report(current_user: dict = Depends(get_current_user)):
    """每日巡检报告（监测循环每日生成；当天未生成时现场补生成一次）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    try:
        from ..core.db import get_pool
        from ..services.rag_monitor import ensure_daily_report
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            row = await conn.fetchrow(
                "SELECT report_date, report, stats, created_at FROM rag_daily_reports "
                "WHERE report_date = CURRENT_DATE")
        if row is None:
            await ensure_daily_report()
            async with pool.acquire(timeout=5) as conn:
                row = await conn.fetchrow(
                    "SELECT report_date, report, stats, created_at FROM rag_daily_reports "
                    "WHERE report_date = CURRENT_DATE")
        if row is None:
            raise HTTPException(status_code=503, detail="报告生成失败，请稍后重试")
        return {"report_date": str(row["report_date"]), "report": row["report"],
                "stats": _as_json(row["stats"], {}),
                "generated_at": str(row["created_at"])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"报告不可用: {type(e).__name__}")


class RagActionPlanBody(BaseModel):
    """动作提案参数；仅允许白名单 action_key。"""

    action_key: str = Field(..., min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)
