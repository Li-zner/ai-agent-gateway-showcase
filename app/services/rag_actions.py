"""RAG 修复动作控制面：白名单、审批、串行执行和验证。

默认只自动执行 L0 只读动作；L1 安全动作必须显式开启
RAG_AUTO_REMEDIATION_ENABLED=1，L2/L3 永远需要人工决策。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from ..core.logging import setup_logging
from .action_registry import ActionSpec, get_action_spec
from .rag_action_audit import (
    finish_action as _finish_action,
    verification_records as _verification_records,
    verification_table_exists as _verification_table_exists,
)
# 探测辅助留在本模块对外可用（tests 引用 rag_actions._resolve_probe_url），
# 注册表本体已迁到 action_registry
from .rag_action_handlers import _resolve_probe_url, _validate_probe_url  # noqa: F401

logger = setup_logging()


def _auto_l1_enabled() -> bool:
    """L1 自动执行开关：运行期读 env（与 _probe_targets 一致），热变更无需重启。"""
    return os.getenv("RAG_AUTO_REMEDIATION_ENABLED", "0") == "1"


def _action_environment() -> str:
    """当前 worker 执行环境；未配置时按 local 处理。"""
    return (
        os.getenv("RAG_ACTION_ENVIRONMENT")
        or os.getenv("APP_ENV")
        or "local"
    ).strip().lower()


_LEASE_SECONDS = 600
_LOOP_INTERVAL = 30
# 心跳周期远小于租约时长：连续两次心跳失败仍留有一次机会。
_HEARTBEAT_INTERVAL = 120
# 续租只延长 lease_until、不改 lease_token：完成写入仍必须匹配当前 token，
# 旧 worker 结果照旧被丢弃（_finish_action 的原子防线不受心跳影响）。


class IncidentNotFoundError(LookupError):
    """动作关联的 incident 不存在。"""


class IncidentNotActionableError(ValueError):
    """已解决或关闭的 incident 不允许继续创建动作。"""


def _json_value(value):
    """asyncpg JSONB 读取兼容。"""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def _action_row(row) -> dict:
    """动作记录转 API 输出。"""
    try:
        rollback_available = (
            row["status"] in ("succeeded", "failed")
            and get_action_spec(row["action_key"]).rollback is not None
        )
    except ValueError:
        rollback_available = False
    return {
        "action_id": int(row["action_id"]),
        "incident_id": (int(row["incident_id"])
                        if row["incident_id"] is not None else None),
        "request_uid": str(row["request_uid"]) if row["request_uid"] else None,
        "action_key": row["action_key"],
        "risk_level": row["risk_level"],
        "status": row["status"],
        "target_type": row["target_type"],
        "target_ref": row["target_ref"],
        "params": _json_value(row["params"]),
        "reason": row["reason"],
        "precheck": _json_value(row["precheck"]),
        "baseline": _json_value(row["baseline"]),
        "result": _json_value(row["result"]),
        "verification": _json_value(row["verification"]),
        "rollback_plan": row["rollback_plan"],
        "rollback_result": _json_value(row["rollback_result"]),
        "rollback_available": rollback_available,
        "requested_by": row["requested_by"],
        "approved_by": row["approved_by"],
        "executed_by": row["executed_by"],
        "attempt_count": row["attempt_count"],
        "lease_token": str(row["lease_token"]) if row["lease_token"] else None,
        "policy_decision_id": row["policy_decision_id"],
        "environment": row["environment"],
        "config_version": row["config_version"],
        "created_at": str(row["created_at"]),
        "started_at": str(row["started_at"]) if row["started_at"] else None,
        "ended_at": str(row["ended_at"]) if row["ended_at"] else None,
    }


async def _lock_incident_for_action(conn, incident_id: int | None) -> None:
    """锁定 incident 并拒绝终态事件，防止解析竞态后继续创建动作。"""
    if incident_id is None:
        return
    status = await conn.fetchval(
        "SELECT status FROM rag_incidents WHERE incident_id=$1 FOR UPDATE",
        incident_id)
    if status is None:
        raise IncidentNotFoundError(f"incident 不存在: {incident_id}")
    if status in ("resolved", "closed"):
        raise IncidentNotActionableError(
            f"incident 当前状态为 {status}，不允许创建动作")


async def create_action(conn, *, incident_id: int | None, action_key: str,
                        params: dict, reason: str,
                        requested_by: str) -> dict:
    """创建幂等动作记录；调用方必须以事务包裹以保持状态检查原子性。"""
    await _lock_incident_for_action(conn, incident_id)
    spec = get_action_spec(action_key)
    from .rag_policy import evaluate_action

    policy = await evaluate_action(
        conn,
        action_key=action_key,
        params=params,
        requested_by=requested_by,
    )
    params = policy["params"]
    # 目标标识由策略层按注册表统一推导并用于冷却分桶，这里只落库
    target_ref = policy["target_ref"]
    payload = json.dumps(params, ensure_ascii=False, sort_keys=True)
    raw_key = f"{incident_id}|{spec.key}|{payload}|{reason}"
    idem = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    status = policy["status"]
    row = await conn.fetchrow(
        "INSERT INTO rag_remediation_actions (incident_id, action_key, "
        "risk_level, status, target_type, target_ref, params, reason, "
        "rollback_plan, idempotency_key, requested_by, policy_decision_id, "
        "environment, config_version) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13,$14) "
        "ON CONFLICT (idempotency_key) DO UPDATE SET updated_at=now() "
        "RETURNING *",
        incident_id, spec.key, spec.risk_level, status, spec.target_type,
        target_ref, payload, reason,
        spec.rollback_plan, idem, requested_by, policy["decision_id"],
        policy["environment"], policy["config_version"])
    return _action_row(row)


async def list_actions(conn, *, limit: int = 50, status: str | None = None,
                       incident_id: int | None = None) -> list[dict]:
    """查询动作列表。"""
    rows = await conn.fetch(
        "SELECT * FROM rag_remediation_actions "
        "WHERE ($1::text IS NULL OR status=$1) "
        "AND ($2::bigint IS NULL OR incident_id=$2) "
        "ORDER BY action_id DESC LIMIT $3",
        status, incident_id, max(1, min(int(limit), 200)))
    return [_action_row(row) for row in rows]


async def get_action(conn, action_id: int) -> dict | None:
    """读取单条动作及策略、验证时间线。"""
    row = await conn.fetchrow(
        "SELECT * FROM rag_remediation_actions WHERE action_id=$1",
        action_id)
    if row is None:
        return None
    item = _action_row(row)
    decision = None
    if row["policy_decision_id"] is not None:
        decision_row = await conn.fetchrow(
            "SELECT decision_id, environment, decision, reason, "
            "policy_version, requested_params, requested_by, cooldown_until, "
            "created_at FROM rag_policy_decisions WHERE decision_id=$1",
            row["policy_decision_id"])
        if decision_row is not None:
            decision = {
                "decision_id": int(decision_row["decision_id"]),
                "environment": decision_row["environment"],
                "decision": decision_row["decision"],
                "reason": decision_row["reason"],
                "policy_version": decision_row["policy_version"],
                "requested_params": _json_value(
                    decision_row["requested_params"]),
                "requested_by": decision_row["requested_by"],
                "cooldown_until": (
                    str(decision_row["cooldown_until"])
                    if decision_row["cooldown_until"] else None
                ),
                "created_at": str(decision_row["created_at"]),
            }
    verification_rows = await conn.fetch(
        "SELECT verification_id, attempt_no, phase, passed, metrics, evidence, reason, "
        "created_at FROM rag_action_verifications "
        "WHERE action_id=$1 ORDER BY verification_id",
        action_id,
    ) if await _verification_table_exists(conn) else []
    item["policy_decision"] = decision
    item["verifications"] = [{
        "verification_id": int(verification["verification_id"]),
        "attempt_no": int(verification["attempt_no"]),
        "phase": verification["phase"],
        "passed": verification["passed"],
        "metrics": _json_value(verification["metrics"]),
        "evidence": _json_value(verification["evidence"]),
        "reason": verification["reason"],
        "created_at": str(verification["created_at"]),
    } for verification in verification_rows]
    return item


async def approve_action(conn, action_id: int, approved_by: str) -> dict | None:
    """审批前重新执行策略校验，然后把动作送入执行队列。"""
    from .rag_policy import evaluate_action

    async with conn.transaction():
        action = await conn.fetchrow(
            "SELECT * FROM rag_remediation_actions "
            "WHERE action_id=$1 AND status='waiting_approval' FOR UPDATE",
            action_id)
        if action is None:
            return None
        if action["incident_id"] is not None:
            incident_status = await conn.fetchval(
                "SELECT status FROM rag_incidents WHERE incident_id=$1",
                action["incident_id"])
            if incident_status in ("resolved", "closed") or incident_status is None:
                return None
        policy = await evaluate_action(
            conn,
            action_key=action["action_key"],
            params=_json_value(action["params"]),
            requested_by=approved_by,
            environment=action["environment"],
        )
        row = await conn.fetchrow(
            "UPDATE rag_remediation_actions SET status='queued', approved_by=$1, "
            "approved_at=now(), policy_decision_id=$2, config_version=$3, "
            "updated_at=now() WHERE action_id=$4 RETURNING *",
            approved_by, policy["decision_id"], policy["config_version"],
            action_id)
        return _action_row(row) if row else None


async def cancel_action(conn, action_id: int, actor: str) -> dict | None:
    """取消尚未执行的动作。"""
    row = await conn.fetchrow(
        "UPDATE rag_remediation_actions SET status='cancelled', "
        "executed_by=COALESCE(executed_by,$1), ended_at=now(), updated_at=now() "
        "WHERE action_id=$2 AND status IN ('proposed','waiting_approval','queued') "
        "RETURNING *",
        actor, action_id)
    return _action_row(row) if row else None


async def _record_rollback_attempt(conn, action_id: int, actor: str) -> None:
    """回滚动手前，在事务外留一条持久的「已发起」痕迹（台账 482，完整理由见服务修复日志）。

    回滚器自备连接并自己提交、动作行却在外层事务最后提交，中间崩溃会留下「已回滚但
    状态仍 succeeded」。本 UPDATE 走 autocommit 独立落库，对账时只要见 manual_attempt
    而终态未动，即判定「已发起、结果未知」；正常路径下终态写回会覆盖整列，不留脏。
    """
    await conn.execute(
        "UPDATE rag_remediation_actions "
        # 列本身 JSONB NOT NULL DEFAULT '{}'（迁移 j1e2f3a4b5c6:40），直接 || 合并；
        # $2 落在 jsonb_build_object 的 "any" 形参上，不显式 ::text 服务端推不出类型
        "SET rollback_result=rollback_result || jsonb_build_object('manual_attempt', "
        "       jsonb_build_object('by', $2::text, 'at', now()::text)), "
        "    updated_at=now() "
        "WHERE action_id=$1 AND status IN ('succeeded','failed')",
        action_id, actor)


async def rollback_action(conn, action_id: int, actor: str) -> dict | None:
    """对已成功或已失败、且有回滚函数的动作执行人工回滚。

    failed 也纳入（台账 481 副作用）：自动回滚失败的动作停在 failed 但 result 里的快照是真的，
    原先只认 succeeded 等于此类动作再无第二次机会；拿不到真快照时回滚实现一律早退成 skipped，
    由 _rollback_status 收成 failed，不会造出假「已撤销」。
    """
    await _record_rollback_attempt(conn, action_id, actor)
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM rag_remediation_actions "
            "WHERE action_id=$1 AND status IN ('succeeded','failed') FOR UPDATE",
            action_id)
        if row is None:
            return None
        try:
            spec = get_action_spec(row["action_key"])
            if spec.rollback is None:
                return None
            rollback_result = await spec.rollback(
                _json_value(row["params"]), _json_value(row["result"]))
        except Exception as exc:
            # 台账 482：回滚器抛业务错（如 ChangeSetError）曾直穿本函数 → 外层事务回滚、
            # 动作仍是 succeeded、路由无 except → 中控台裸 500。违约的兜底放在框架边界：
            # 收成 passed=False 让动作转 failed；回滚器用自备连接，不污染本 conn 事务状态。
            logger.warning("RAG 动作 %s 人工回滚抛错（转 failed 交人工）: %s: %s",
                           action_id, type(exc).__name__, exc)
            rollback_result = {"passed": False, "error": type(exc).__name__,
                               "reason": str(exc)[:200]}
        status = _rollback_status(rollback_result)
        updated = await conn.fetchrow(
            "UPDATE rag_remediation_actions SET status=$1, rollback_result=$2::jsonb, "
            "executed_by=COALESCE(executed_by,$3), updated_at=now() "
            "WHERE action_id=$4 RETURNING *",
            status, json.dumps(rollback_result, ensure_ascii=False),
            actor, action_id)
        return _action_row(updated) if updated else None


async def check_action(conn, action_id: int, actor: str, note: str) -> dict | None:
    """把执行失败的动作「人工已核对」终结：只翻状态与留痕，不碰任何业务数据。

    台账 481 后续：无快照可撤销的 failed 动作点回滚只会反复写回 skipped，永远进不了
    终态；人工核对过实际数据、确认「不再需要撤销」即落 `checked`。不复用 `cancelled`
    （只发生在执行之前，语义是「什么都没发生」，会把真实写入算成零操作），也不复用
    `rolled_back`（那是系统按快照撤销，这里是人来担保）。
    """
    row = await conn.fetchrow(
        "UPDATE rag_remediation_actions SET status='checked', "
        # 只加一个键：留痕与回滚结果同居 rollback_result，详情面板原样可见
        # executed_by 不动——它是「谁执行了这次写」，把核对人塞进去会污染审计口径
        "rollback_result=rollback_result || jsonb_build_object('manual_check', "
        "  jsonb_build_object('by', $2::text, 'at', now()::text, 'note', $3::text)), "
        "ended_at=COALESCE(ended_at,now()), updated_at=now() "
        "WHERE action_id=$1 AND status='failed' RETURNING *",
        action_id, actor, note)
    return _action_row(row) if row else None


async def _claim_next(conn, worker: str, environment: str) -> dict | None:
    """原子抢占一个 queued 或租约过期的动作。"""
    row = await conn.fetchrow(
        "UPDATE rag_remediation_actions SET status='executing', "
        "executed_by=$1, started_at=COALESCE(started_at,now()), "
        "lease_until=now() + ($2 || ' seconds')::interval, lease_token=$4::uuid, "
        "attempt_count=attempt_count+1, updated_at=now() "
        "WHERE action_id = ("
        "  SELECT action_id FROM rag_remediation_actions "
        "  WHERE (status='queued' OR (status='executing' AND lease_until < now())) "
        "  AND environment=$3 "
        "  AND (incident_id IS NULL OR EXISTS ("
        "    SELECT 1 FROM rag_incidents i "
        "    WHERE i.incident_id=rag_remediation_actions.incident_id "
        "    AND i.status NOT IN ('resolved','closed'))) "
        "  ORDER BY action_id FOR UPDATE SKIP LOCKED LIMIT 1"
        ") RETURNING *",
        worker, str(_LEASE_SECONDS), environment, str(uuid.uuid4()))
    return _action_row(row) if row else None


def _action_lock_key(action: dict) -> str:
    """同一环境同一动作类型必须串行，避免交叉改配置和回滚。

    RAG-1（2026-09-19 审查）：带 handler 的动作进一步合并为**环境级共用锁**——
    adjust_rerank_top 与 apply_knowledge_change 各自改配置/重建索引后都要过
    同一个质量门禁差分，跨类型并行会互相污染对方的基线与复测结果（本闭环
    唯一质量防线）。L3/人工动作不执行 handler，保留动作粒度即可。
    """
    env = action.get("environment") or "local"
    try:
        spec = get_action_spec(action["action_key"])
        if spec.handler is not None and action.get("risk_level") != "L3":
            return f"{env}:__handler__"
    except Exception:  # noqa: silent-except 豁免：未注册 action_key 退回动作粒度锁键，真实报错留给执行路径
        pass  # 未知 action_key 退回动作粒度，真实报错留给执行路径
    return f"{env}:{action['action_key']}"


async def _try_action_lock(conn, lock_key: str) -> bool:
    """获取会话级 advisory lock；连接断开时 PostgreSQL 自动释放。"""
    return bool(await conn.fetchval(
        "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
        lock_key,
    ))


async def _release_action_lock(conn, lock_key: str) -> None:
    """释放会话级动作锁，失败只记录以避免掩盖主结果。"""
    try:
        await conn.fetchval(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
            lock_key,
        )
    except Exception as exc:
        logger.warning("释放 RAG 动作锁失败 %s: %s", lock_key, exc)


async def _requeue_action(conn, action_id: int, lease_token: str | None) -> bool:
    """同动作已被其他执行器占用时，把本次抢占安全退回队列。"""
    if not lease_token:
        return False
    return await conn.fetchval(
        "UPDATE rag_remediation_actions SET status='queued', "
        "started_at=NULL, lease_until=NULL, lease_token=NULL, updated_at=now() "
        "WHERE action_id=$1 AND status='executing' "
        "AND lease_token=$2::uuid RETURNING 1",
        action_id, lease_token,
    ) is not None


def _rollback_status(rollback_result: dict) -> str:
    """回滚结果 → 动作终态。

    `skipped` 是四个回滚实现共用的判空口径（快照里没有可恢复的原值就早退，
    碰库之前返回 `passed=True, skipped=True`）——它表示**什么都没撤销**，
    不能记成 rolled_back。台账 481 的假"已撤销"有两个入口：异常路径的盲回滚
    （D5 已删）与这里的 skipped+passed=True；后者在人工回滚按钮放开
    （failed 动作也可回滚）之后会被运营直接踩到，所以两个入口共用这一条判据。
    """
    if rollback_result.get("skipped"):
        return "failed"
    return "rolled_back" if rollback_result.get("passed") else "failed"


async def verify_or_rollback(spec: ActionSpec, params: dict,
                             outcome: dict) -> tuple[str, dict, dict]:
    """按验证结果决定成功、回滚或失败。"""
    verification = outcome.get("verification") or {"passed": True}
    if verification.get("passed"):
        return "succeeded", verification, {}
    if spec.rollback is not None:
        try:
            rollback_result = await spec.rollback(
                params, outcome.get("result") or {})
        except Exception as exc:
            return "failed", verification, {
                "passed": False,
                "error": type(exc).__name__,
            }
        return _rollback_status(rollback_result), verification, rollback_result
    return "failed", verification, {}


async def _requeue_claimed_action(pool, action: dict) -> bool:
    """把未能取得动作锁的执行记录退回队列。"""
    async with pool.acquire(timeout=5) as conn:
        return await _requeue_action(
            conn, action["action_id"], action.get("lease_token"))


async def _heartbeat_loop(pool, action_id: int, lease_token: str) -> None:
    """执行期间周期性续租；租约被接管（更新 0 行）或动作已终态即退出。

    与 claim/finish 的租约机制一样属 DB 级语义，由部署时数据库验证覆盖，
    不在单测内造假连接。
    """
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        try:
            async with pool.acquire(timeout=5) as conn:
                renewed = await conn.execute(
                    "UPDATE rag_remediation_actions "
                    "SET lease_until=now() + ($3 || ' seconds')::interval, "
                    "updated_at=now() "
                    "WHERE action_id=$1 AND status='executing' "
                    "AND lease_token=$2::uuid",
                    action_id, lease_token, str(_LEASE_SECONDS))
        except Exception as exc:
            logger.warning("RAG 动作 %s 心跳续租失败: %s: %s",
                           action_id, type(exc).__name__, exc)
            continue
        if str(renewed).split()[-1] == "0":
            logger.warning("RAG 动作 %s 租约已失效，停止心跳", action_id)
            return


async def _execute_locked_action(pool, lock_conn, lock_key: str,
                                 action: dict) -> bool:
    """持有动作锁执行并落库，返回结果是否真实写回。"""
    heartbeat = asyncio.create_task(
        _heartbeat_loop(pool, action["action_id"],
                        str(action["lease_token"])))
    try:
        try:
            spec = get_action_spec(action["action_key"])
            if action["risk_level"] == "L3" or spec.handler is None:
                raise RuntimeError("L3 动作只能人工执行")
            outcome = await spec.handler(action["params"])
            status, verification, rollback_result = await verify_or_rollback(
                spec, action["params"], outcome)
            result = outcome.get("result") or {}
        except Exception as exc:
            logger.warning("RAG 动作 %s 执行失败: %s: %s",
                           action["action_id"], type(exc).__name__, exc)
            status = "failed"
            verification = {"passed": False}
            result = {"error": type(exc).__name__}
            # 台账 481（2026-10-08 第四轮 D5）：异常路径不再调 spec.rollback。
            # 这里能递给回滚器的只有 {"error": ...}，而四个回滚实现都按快照键
            # 判空（was_active/applied/baseline_value）→ 一律 skipped+passed=True，
            # 结果"账号不存在""管理员账号被拒"这类压根没写过库的失败被标成
            # rolled_back，中控台以为已撤销，再没人去核对真实状态。
            # 真正"写已提交后才失败"的场景本就不该走到这里：现有 handler 都在
            # 提交后的复测/失效步骤自捕异常，带真快照返回 verification.passed=False，
            # 由 verify_or_rollback 走快照回滚。到这里=连快照都没有，只能转人工。
            rollback_result = {"passed": False, "skipped": True,
                               "reason": "执行异常且无快照，未做盲回滚；"
                                         "请核对是否已有提交，需要时人工回滚"}

        async with pool.acquire(timeout=5) as conn:
            finished = await _finish_action(
                conn, action["action_id"], status=status, result=result,
                verification=verification, rollback_result=rollback_result,
                attempt_no=int(action.get("attempt_count") or 1),
                lease_token=action["lease_token"])
        if not finished:
            logger.warning("RAG 动作 %s 租约已失效，丢弃旧 worker 结果",
                           action["action_id"])
            return False
        try:
            from ..core.metrics import rag_action_executions_total
            rag_action_executions_total.labels(
                risk_level=action["risk_level"], status=status).inc()
        except Exception:  # noqa: silent-except — 指标失败不影响动作结果
            pass
        return True
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:  # noqa: silent-except — 上方已 cancel 心跳，此处仅等其收尾，取消属预期
            pass
        except Exception as exc:
            logger.warning("RAG 动作 %s 心跳任务异常收尾: %s",
                           action["action_id"], type(exc).__name__)
        await _release_action_lock(lock_conn, lock_key)
        await pool.release(lock_conn)


async def execute_queued_actions(limit: int = 5) -> int:
    """执行至多 limit 个已批准动作；单动作失败不阻塞后续。"""
    from ..core.db import get_pool

    pool = await get_pool()
    executed = 0
    for _ in range(max(1, int(limit))):
        async with pool.acquire(timeout=5) as conn:
            async with conn.transaction():
                action = await _claim_next(
                    conn,
                    os.getenv("HOSTNAME", "worker"),
                    _action_environment(),
                )
        if action is None:
            break
        lock_key = _action_lock_key(action)
        lock_conn = None
        try:
            lock_conn = await pool.acquire(timeout=5)
            locked = await _try_action_lock(lock_conn, lock_key)
        except Exception as exc:
            # RAG-6（2026-09-20 审查）：acquire 已成功而 _try_action_lock 抛错时
            # 连接没人归还，长驻 worker 反复触发会耗干连接池（其余路径的归还
            # 分别在本函数 not-locked 分支与 _execute_locked_action 的 finally）。
            if lock_conn is not None:
                await pool.release(lock_conn)
            logger.warning("获取 RAG 动作锁失败 %s: %s", lock_key, exc)
            await _requeue_claimed_action(pool, action)
            break
        if not locked:
            await pool.release(lock_conn)
            await _requeue_claimed_action(pool, action)
            logger.info("RAG 动作 %s 同类型正在执行，退回队列等待",
                        action["action_id"])
            break
        if await _execute_locked_action(pool, lock_conn, lock_key, action):
            executed += 1
    return executed


async def rag_action_loop() -> None:
    """常驻动作 worker：轮询并执行已批准动作。"""
    logger.info("RAG 修复动作 worker 已启动（每 %ss）", _LOOP_INTERVAL)
    while True:
        try:
            await execute_queued_actions()
        except Exception as exc:
            logger.warning("RAG 修复动作 worker 异常（继续）: %s: %s",
                           type(exc).__name__, exc)
        try:
            from ..core.metrics import rag_action_worker_last_run_timestamp_seconds
            rag_action_worker_last_run_timestamp_seconds.set_to_current_time()
        except Exception:  # noqa: silent-except — 指标失败不影响 worker
            pass
        await asyncio.sleep(_LOOP_INTERVAL)
