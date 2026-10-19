"""三道人工门：写审批门、批组门、计划门的构造与等待（批次 F 步骤 2 自 tasks.py 拆出）。

依赖 task_state（注册表与超时原语）与 task_ledger（门指纹）；上层调用者只有 tasks._run_task。"""
from __future__ import annotations

import asyncio
import json
import time
from ...core.db import get_pool
from ...core.metrics import dbagent_gate_decisions_total
from .changes import _assess_write_risk, _renumbered_preview
from .task_state import (
    _ABANDONED,
    _GATES,
    _GATE_DECISION_TIMEOUT,
    _claim_background,
    _claim_gate_timeout,
    _ensure_gate_listener,
    _fetch_one,
    _fire_alert,
    _jsonb,
    _persist,
    logger,
)
from .task_ledger import (
    _gate_fingerprint,
)


def _make_gate(task_id: str, steps: list):
    """构造写审批门：登记提案 -> 状态落库 + 本进程事件挂起 -> 等决定。

    双实例：决定可打到任一实例（decide() 广播见下）；本进程事件由
    _GATES 轮询器或本进程 decide 唤醒。
    """

    async def confirm(sql: str, params: list, reason: str) -> bool:
        logger.info("[gate-trace] write门进入 task=%s", task_id)
        try:
            preview = await _renumbered_preview(sql, params)
        except Exception as exc:  # noqa: BLE001
            preview = {"kind": "unknown", "preview_sql": None, "rows": [],
                       "note": f"预览生成失败（{type(exc).__name__}），请按 SQL 与参数人工判断"}
        risk = await _assess_write_risk(sql, params)
        # ⑤执行前复检：update/delete 的预览指纹随批准暂存；INSERT、预览失败、
        # 风险评估失败不设指纹（复检退避，人审兜底）
        fingerprint = None
        if preview.get("kind") in ("update", "delete"):
            fingerprint = _gate_fingerprint(preview.get("rows") or [], risk)
        # 重大变更标记（业主拍板：影响行数 ≥ 全表 15% 需最高管理员批准）
        proposal = {"sql": sql, "params": params, "reason": reason,
                    "preview": preview, "risk": risk, "decision": None,
                    "critical": bool(risk.get("critical"))}
        await _persist(task_id, status="awaiting_approval",
                       proposal=json.dumps(proposal, ensure_ascii=False, default=str))
        _fire_alert(
            f"DB Agent 待审批｜任务 {task_id}（写提案）",
            f"{reason[:120]}\nSQL：{sql[:200]}\n"
            f"审批请回中控台操作，群内回复无效\n风险 {risk.get('level')}｜预计影响 {risk.get('affected')} 行"
            f"（全表 {risk.get('table_rows')}）")
        logger.info("[gate-trace] write门挂起等审批 task=%s", task_id)
        decision, decided_by = await _await_gate(task_id, "write")
        proposal["decision"] = "approved" if decision else "rejected"
        proposal["decided_by"] = decided_by
        if decision:
            _GATES.setdefault(task_id, {})["last_approved_by"] = decided_by
            # P1-5：等级随批准暂存，写包装器据此强制 L2/unknown 转 proposed
            _GATES.setdefault(task_id, {})["last_risk"] = str(risk.get("level") or "")
            # ⑤：预览指纹随批准暂存，写包装器执行前比对（审批-执行窗口复检）
            _GATES.setdefault(task_id, {})["last_fingerprint"] = fingerprint
        # 注意：此处不再回写 status='running'——decide() 已把状态置 running，
        # 门协程再回写会与下一道门的 awaiting 状态竞态（写门挂起被覆盖回
        # running 的实测：任务卡死无门可批）。终态由 _finalize 落。
        await _persist(task_id,
                       proposal=json.dumps(proposal, ensure_ascii=False, default=str))
        return decision

    return confirm


_BATCH_RANK = {"L2": 3, "L1": 2, "L0": 1}


def _group_risk(risks: list[dict]) -> dict:
    """批提案组级风险：任一 L2 → L2；任一 unknown → unknown（评估失败保守视高）；
    否则取组内最高级，reasons 带语句序号合并。critical（重大变更，业主拍板
    ≥15% 行）任一语句命中即整组标记。"""
    critical = any(bool(r.get("critical")) for r in risks)
    if any(r.get("level") == "L2" for r in risks):
        return {"level": "L2", "reasons": ["批提案组内存在 L2 级语句"],
                "critical": critical}
    if any(r.get("level") == "unknown" for r in risks):
        return {"level": "unknown",
                "reasons": ["批提案组内存在风险评估未完成的语句，请按 SQL 与参数人工判断"],
                "critical": critical}
    top = max(risks, key=lambda r: _BATCH_RANK.get(r.get("level"), 0))
    reasons = [f"第 {i + 1} 条：{reason}" for i, r in enumerate(risks)
               for reason in r.get("reasons", [])]
    return {"level": top.get("level", "unknown"),
            "reasons": reasons or ["组风险评估为空"], "critical": critical}


def _make_batch_gate(task_id: str, steps: list):
    """批提案审批门（P1-2）：一组语句一次审批，门机制与单条写门相同。"""

    async def confirm_batch(statements: list[dict], reason: str) -> bool:
        logger.info("[gate-trace] batch写门进入 task=%s（%d 条）",
                    task_id, len(statements))
        previews: list[dict] = []
        for st in statements:
            try:
                previews.append(await _renumbered_preview(st["sql"], st["params"]))
            except Exception as exc:  # noqa: BLE001
                previews.append({"kind": "unknown", "preview_sql": None, "rows": [],
                                 "note": f"预览生成失败（{type(exc).__name__}）"})
        risks = [await _assess_write_risk(st["sql"], st["params"])
                 for st in statements]
        group_risk = _group_risk(risks)
        fingerprints: list[dict | None] = []
        for preview, risk in zip(previews, risks):
            # ⑤：逐条指纹（打满截断/评估失败按 _gate_fingerprint 口径退避）
            fingerprints.append(
                _gate_fingerprint(preview.get("rows") or [], risk)
                if preview.get("kind") in ("update", "delete") else None)
        proposal = {"kind": "batch", "statements": statements, "reason": reason,
                    "previews": previews, "risk": group_risk, "decision": None,
                    "critical": bool(group_risk.get("critical"))}
        await _persist(task_id, status="awaiting_approval",
                       proposal=json.dumps(proposal, ensure_ascii=False, default=str))
        _fire_alert(
            f"DB Agent 待审批｜任务 {task_id}（批提案 {len(statements)} 条）",
            f"{reason[:120]}\n审批请回中控台操作，群内回复无效\n组风险 {group_risk.get('level')}｜"
            f"预计影响 {sum(int(r.get('affected') or 0) for r in risks)} 行")
        logger.info("[gate-trace] batch写门挂起等审批 task=%s", task_id)
        decision, decided_by = await _await_gate(task_id, "write")
        proposal["decision"] = "approved" if decision else "rejected"
        proposal["decided_by"] = decided_by
        if decision:
            _GATES.setdefault(task_id, {})["last_approved_by"] = decided_by
            # P1-5：组级风险（任一 L2 即 L2 / unknown 保守视高）同款暂存
            _GATES.setdefault(task_id, {})["last_risk"] = str(group_risk.get("level") or "")
            # ⑤：逐条预览指纹随批准暂存（与 statements 顺序对齐）
            _GATES.setdefault(task_id, {})["last_fingerprints"] = fingerprints
        await _persist(task_id,
                       proposal=json.dumps(proposal, ensure_ascii=False, default=str))
        return decision

    return confirm_batch


def _make_plan_gate(task_id: str):
    """计划审批门（ZCode plan mode 式），挂起/广播机制与写门一致。"""

    async def confirm_plan(goal: str, steps: list) -> bool:
        logger.info("[gate-trace] plan门进入 task=%s", task_id)
        plan = {"goal": goal, "steps": list(steps), "decision": None}
        await _persist(task_id, status="awaiting_plan_approval",
                       plan=json.dumps(plan, ensure_ascii=False, default=str))
        _fire_alert(
            f"DB Agent 待审批｜任务 {task_id}（执行计划）",
            f"{goal[:150]}\n审批请回中控台操作，群内回复无效\n步骤 {len(steps)} 项：{'；'.join(steps[:3])[:220]}")
        logger.info("[gate-trace] plan门挂起等审批 task=%s", task_id)
        decision, decided_by = await _await_gate(task_id, "plan")
        plan["decision"] = "approved" if decision else "rejected"
        plan["decided_by"] = decided_by
        # 同写门：不回写 status（decide 已置 running，门协程回写会竞态覆盖
        # 下一道门的 awaiting 状态——写门挂起被覆盖的实测教训）
        await _persist(task_id,
                       plan=json.dumps(plan, ensure_ascii=False, default=str))
        return decision

    return confirm_plan


async def _await_gate(task_id: str, kind: str) -> tuple[bool, str]:
    """挂起等审批决定：本实例事件唤醒 or 跨实例轮询 PG 状态变化。

    审批超时（#2 配套）：_GATE_DECISION_TIMEOUT 秒无人批 -> 自动否决收尾，绝不让任务无限
    占并发槽；轮询异常逐条记日志，不静默。P2-1：决定落库时 pg_notify 即时唤醒本实例事件
    （跨实例同样生效），1s 轮询保留——监听断开/notify 丢失只退化为秒级延迟。
    """
    event = asyncio.Event()
    _GATES[task_id] = {"event": event, "kind": kind}
    await _ensure_gate_listener()
    pool = await get_pool()
    deadline = time.monotonic() + _GATE_DECISION_TIMEOUT
    polls = 0
    reminded = False
    try:
        while True:
            polls += 1
            if polls % 30 == 1:
                logger.info("[gate-trace] %s 门轮询第 %d 次 task=%s",
                            kind, polls, task_id)
            # P3-5 半程提醒：等待过半仍无人批 -> webhook 催一次（每道门一次，send_alert 自带节流与 fail-safe）
            if not reminded and time.monotonic() > deadline - _GATE_DECISION_TIMEOUT / 2:
                reminded = True
                from ...core.alerting import send_alert
                asyncio.create_task(send_alert(
                    "DB Agent 审批等待过半",
                    f"任务 {task_id} 的 {kind} 门已等待 "
                    f"{_GATE_DECISION_TIMEOUT / 2:.0f}s 无人审批，剩余同额时间"
                    "后将自动否决。请到中控台处理。")).add_done_callback(
                    _claim_background)
            if time.monotonic() > deadline:
                logger.warning("[gate-trace] %s 门审批超时，自动否决 task=%s",
                               kind, task_id)
                # P1-3：自动否决也计入门决定（decision=auto_rejected），此前超时路径零打点、否决率不可观测
                dbagent_gate_decisions_total.labels(kind, "auto_rejected").inc()
                try:
                    # 状态同步摘走（台账 450）：否则这段窗口 decide() 仍可审批成功
                    if not await _claim_gate_timeout(task_id):
                        logger.info("[gate-trace] %s 门超时但状态已被处理 task=%s",
                                    kind, task_id)
                except Exception as exc:  # noqa: BLE001 — 摘状态失败不卡否决返回
                    logger.warning("[gate-trace] %s 门超时状态摘除失败 task=%s: %s",
                                   kind, task_id, type(exc).__name__)
                _fire_alert(
                    f"DB Agent 审批超时自动否决｜任务 {task_id}",
                    f"{kind} 门等待 {_GATE_DECISION_TIMEOUT:.0f}s 无人审批，"
                    "提案已自动否决")
                return False, "system: 审批超时自动否决"
            if task_id in _ABANDONED:
                # P2-2：墙钟已终态落库——挂起门立即退出，绝不同意后续写（否则僵尸协程会把
                # failed 任务翻回 awaiting_approval）
                logger.info("[gate-trace] %s 门见任务已废弃，直接否决 task=%s",
                            kind, task_id)
                # 逻辑账对账（2026-10-06）：废弃是门退出的第三态，此前零打点致决定数与任务数对不上
                dbagent_gate_decisions_total.labels(kind, "abandoned").inc()
                return False, "system: 任务已废弃"
            try:
                async with pool.acquire() as conn:
                    row = await _fetch_one(conn, task_id)
                    if row and row["status"] not in ("awaiting_approval",
                                                     "awaiting_plan_approval"):
                        logger.info("[gate-trace] %s 门见到状态 %s，读决定 task=%s",
                                    kind, row["status"], task_id)
                        # 决定由另一实例写入（或本实例 decide 先改库再 set 事件）。修复 2026-10-04：读决定
                        # 必须在 acquire 会话内完成——此前在块外用已归还连接，InterfaceError 每秒重演被当
                        # "抖动"吞掉、决定永远读不回，门只能等 300s 超时自动否决（7044cd2 引入，双实例验收实测）
                        obj = _jsonb(row.get(
                            "plan" if kind == "plan" else "proposal"), {}) or {}
                        return (bool(obj.get("decision") == "approved"),
                                str(obj.get("decided_by") or ""))
            except Exception as exc:  # noqa: BLE001 — 单次抖动重试，但留痕
                logger.warning("[gate-trace] %s 门轮询异常 task=%s: %s: %s",
                               kind, task_id, type(exc).__name__, str(exc)[:120])
            try:
                await asyncio.wait_for(event.wait(), timeout=1.0)
                event.clear()
            except asyncio.TimeoutError:
                continue
    finally:
        _GATES.pop(task_id, None)
