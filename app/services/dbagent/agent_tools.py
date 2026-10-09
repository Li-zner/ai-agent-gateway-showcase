"""十个工具的处理器：返回（回喂模型的观察文本, 终态结果）（批次 F 步骤 1 自 agent.py 拆出）。
"""
from __future__ import annotations

from ...core.audit import ACT_DBAGENT_WRITE_APPROVAL, query_audit
from ...core.metrics import dbagent_guard_rejects_total
from .catalog import metric
from .evidence import ReportRejected, check_citations
from .masking import mask_rows_for_sql
from .sql_guard import SqlRejectedError, guard_sql, guard_write_sql, with_row_limit
from .agent_ctx import WriteBlocked, _RunCtx, _maybe_await


async def _tool_recall_memory(args: dict, ctx: _RunCtx) -> str:
    """会话记忆检索（memory.recall 受信实现）：默认本会话，cross 需开关且限同
    actor。命中登记为证据（可被报告 [E#] 引用）——记忆里的数字也可溯源。

    记忆表在独立 schema（不在守卫白名单），本工具是模型接触记忆的唯一通道。
    """
    if not ctx.memory:
        return ("本任务未关联会话记忆，无历史可检索。"
                "请只依据本轮查询回答。")
    query = str(args.get("query") or "").strip()
    if not query:
        return "recall_memory 需要 query 关键词。"
    scope = str(args.get("scope") or "current")
    if scope not in ("current", "cross"):
        return "scope 只允许 current（本会话）或 cross（跨会话，需开关）。"
    time_from = args.get("time_from") or None
    time_to = args.get("time_to") or None
    from . import memory as memory_svc
    try:
        result = await memory_svc.recall(
            ctx.memory["session_id"], ctx.memory["actor"],
            bool(ctx.memory.get("allow_cross")), query, scope,
            time_from if isinstance(time_from, str) else None,
            time_to if isinstance(time_to, str) else None)
    except ValueError as exc:
        return f"检索被拒：{exc}。请只用本会话记忆或提示用户开启跨会话开关。"
    except Exception as exc:  # noqa: BLE001 — 记忆故障不阻塞任务
        return (f"记忆检索失败（{type(exc).__name__}），请改为只依赖本轮查询。")
    blocks, turns = result["blocks"], result["turns"]
    if not blocks and not turns:
        return (f"没有找到匹配「{query[:60]}」的记忆"
                f"（scope={scope}）。可换关键词、放宽时间窗，或改用 run_sql 直接查。")
    lines = [f"记忆检索命中（scope={scope}，query={query[:60]}）："]
    for b in blocks:
        lines.append(f"- [块 {b['session_id'][:8]}… 第{b['seq_from']}-{b['seq_to']}轮] "
                     f"{str(b['summary'])[:300]}")
    rows = []
    for t in turns:
        lines.append(f"- [轮 {t['session_id'][:8]}… #{t['seq']} @ {t['created_at'][:16]}] "
                     f"问：{t['question'][:120]}｜答：{t['report'][:200]}")
        rows.append({"session_id": t["session_id"], "seq": t["seq"],
                     "question": t["question"], "report": t["report"],
                     "created_at": t["created_at"]})
    eid = ctx.log.register(f"[recall_memory] {query}", rows, kind="read")
    return "\n".join(lines) + f"\n检索结果已登记为证据 {eid}（引用格式 [E#]）。"


async def _tool_run_sql(args: dict, ctx: _RunCtx) -> str:
    """守卫 -> 执行 -> 登记证据；守卫拒绝也作为观察回喂。"""
    params = args.get("params") or []
    if not isinstance(params, list) or len(params) > 20:
        return "params 必须是不超过 20 个值的数组。"
    try:
        guarded = guard_sql(str(args.get("sql", "")))
    except SqlRejectedError as exc:
        dbagent_guard_rejects_total.labels("read").inc()
        return f"守卫拒绝：{exc}。请改写为合规查询（白名单表 / 单条只读 / LIMIT<=200）。"
    executed = with_row_limit(guarded)
    try:
        rows = await ctx.db_fetch(executed, tuple(params))
    except Exception as exc:  # noqa: BLE001
        return f"执行失败：{type(exc).__name__}: {str(exc)[:200]}。请换一种写法。"
    # 出云脱敏：模型与证据链只见过遮蔽行（敏感值代号化）；真实行只在
    # 人审通道（审批预览/变更账本）
    rows = mask_rows_for_sql(executed, rows, ctx.masker)
    # params 一并入证据基底：时间窗数字（"最近 14 天"的 14）来自参数，
    # 属实取数条件，应可被引用（此前只匹配结果行导致误拒）
    eid = ctx.log.register(guarded, rows,
                           params=list(params) or None)
    return f"查询完成，证据 {eid} 已登记：{ctx.log.get(eid).summary()}"


async def _tool_write_sql(args: dict, ctx: _RunCtx) -> str:
    """写提议：计划硬闸（自动代提） -> 写守卫 -> 人工审批 -> 执行 -> 登记写证据。

    计划硬闸（2026-10-04 终版）：计划通道开启且未获批时，不再拦截回喂让模型
    自觉调 submit_plan（限流时段模型反复回纯文本，死循环耗尽墙钟——实测），
    而是直接以本次写提案自动代提计划门：人批准计划后顺势进写门再批一次。
    两道门都在，模型与用户都不需多走一步。
    任何一道不过都只产生观察文本（回喂模型），不执行。
    """
    if ctx.db_write is None or ctx.confirm_write is None:
        return "写入被拒：当前运行没有开写通道（或无审批回调），请只用只读工具完成任务。"
    params = args.get("params", [])
    reason = str(args.get("reason") or "")
    if not isinstance(params, list) or len(params) > 20:
        return "写入被拒：params 必须是不超过 20 个值的数组。"
    try:
        guarded = guard_write_sql(str(args.get("sql", "")),
                                  allow_delete=ctx.allow_delete)
    except SqlRejectedError as exc:
        dbagent_guard_rejects_total.labels("write").inc()
        return f"写守卫拒绝：{exc}。改写要点：值走 $N 参数、必带 WHERE 与 RETURNING。"
    if ctx.confirm_plan is not None and not ctx.plan_approved:
        goal = reason or f"执行写操作：{guarded[:60]}"
        plan_steps = [f"执行写语句并回读确认：{guarded[:100]}"]
        plan_approved = bool(await _maybe_await(
            ctx.confirm_plan(goal, plan_steps)))
        ctx.plan_approved = plan_approved
        if not plan_approved:
            return "执行计划被人工否决，本次写操作未提交。请调整方案（或改为只读汇报）。"
    approved = bool(await _maybe_await(
        ctx.confirm_write(guarded, params, reason)))
    if not approved:
        return "写入已被人工否决，未执行。请调整方案（或改用只读汇报）。"
    try:
        rows = await ctx.db_write(guarded, params)
    except WriteBlocked as exc:
        # 执行前复检不一致（tasks 写包装器）：数据在审批等待期间被并发修改，
        # 未写任何数据——模型需重新核对后提案，而非重试原语句
        return f"写执行被阻止：{exc}"
    except Exception as exc:  # noqa: BLE001
        return f"写执行失败：{type(exc).__name__}: {str(exc)[:200]}。请检查后重试或改为只读汇报。"
    if ctx.propose_only:
        # 影子期（#10）：db_write 是登记包装器，SQL 未执行（数据未变更）。
        # 不登记写证据——没有回读行可引用；报告如实说"已交人工执行"。
        return ("已交人工执行：系统处于影子期（propose-only），写门批准后不自动写库，"
                f"SQL 与参数已登记改动日志（action=proposed，awaiting_manual）。SQL：{guarded[:200]}"
                "。请如实向用户汇报「已提交人工执行、尚未生效」，不要声称已写入，也不要重试。")
    if ctx.forced_propose is not None and ctx.forced_propose():
        # P1-5：L2/unknown 高危提案即便批准也只登记不执行（双人复核的替代）。
        # 与影子期分支分开报原因，报告里能看出是哪条规则拦的。
        return ("已交人工执行：本笔属 L2/unknown 高危提案（影响行数大或风险评估未完成），"
                "批准后不自动写库，SQL 与参数已登记改动日志（action=proposed，"
                f"awaiting_manual）。SQL：{guarded[:200]}"
                "。请如实向用户汇报「已提交人工执行、尚未生效」，不要声称已写入，也不要重试。")
    # RETURNING 回读行同样遮蔽后再进模型上下文（真实行已由账本层留档给人审）
    rows = mask_rows_for_sql(guarded, rows, ctx.masker)
    eid = ctx.log.register(guarded, rows, kind="write", params=params)
    return f"写入完成，证据 {eid}（写）已登记：{ctx.log.get(eid).summary()}"


async def _tool_write_batch(args: dict, ctx: _RunCtx) -> str:
    """批提议（P1-2）：计划硬闸 -> 逐条写守卫 -> 批审批门 -> 单事务执行 -> 逐条登记证据。

    原子语义在执行层（pools.execute_write_batch 单事务）：任一条失败整组回滚。
    守卫是整组闸：任一条拒绝，整组不进门、不执行（模型需修正后重新提议）。
    """
    if ctx.db_write_batch is None or ctx.confirm_write_batch is None:
        return ("批提案通道未开启：请改用单条 write_sql 分步提交"
                "（每次审批一条），或只用只读工具完成任务。")
    raw_statements = args.get("statements")
    reason = str(args.get("reason") or "")
    if not isinstance(raw_statements, list) or not 2 <= len(raw_statements) <= 5:
        return "写入被拒：statements 必须是 2-5 条语句的数组。"
    statements: list[dict] = []
    for i, st in enumerate(raw_statements, 1):
        if not isinstance(st, dict) or not isinstance(st.get("params", []), list):
            return "写入被拒：每条语句需含 sql（字符串）与 params（数组）。"
        if len(st.get("params", [])) > 20:
            return f"写入被拒：第 {i} 条 params 超过 20 个值。"
        try:
            guarded = guard_write_sql(str(st.get("sql", "")),
                                      allow_delete=ctx.allow_delete)
        except SqlRejectedError as exc:
            dbagent_guard_rejects_total.labels("write").inc()
            return (f"写守卫拒绝整组（第 {i} 条）：{exc}。"
                    "修正该条后重新提交整组——组内其余语句未执行。")
        statements.append({"sql": guarded, "params": st.get("params", [])})
    if ctx.confirm_plan is not None and not ctx.plan_approved:
        plan_steps = [f"原子执行写语句 {i}/{len(statements)}：{st['sql'][:80]}"
                      for i, st in enumerate(statements, 1)]
        plan_approved = bool(await _maybe_await(
            ctx.confirm_plan(reason or "执行批写提案", plan_steps)))
        ctx.plan_approved = plan_approved
        if not plan_approved:
            return "执行计划被人工否决，本批写操作未提交。请调整方案（或改为只读汇报）。"
    approved = bool(await _maybe_await(
        ctx.confirm_write_batch(statements, reason)))
    if not approved:
        return "批写入已被人工否决，整组未执行。请调整方案（或改用只读汇报）。"
    try:
        entries = await ctx.db_write_batch(statements)
    except WriteBlocked as exc:
        # 同单条写：任一语句复检不一致即整组拒绝（事务未开启，无部分生效）
        return f"批写执行被阻止（整组未执行）：{exc}"
    except Exception as exc:  # noqa: BLE001
        return (f"批写执行失败（事务已整体回滚，无部分生效）："
                f"{type(exc).__name__}: {str(exc)[:200]}。请检查后重试或改为只读汇报。")
    if ctx.propose_only:
        return ("已交人工执行：系统处于影子期（propose-only），批写门批准后不自动写库，"
                f"{len(statements)} 条 SQL 与参数已登记改动日志（action=proposed，"
                "awaiting_manual，同一组）。请如实向用户汇报「已提交人工执行、尚未生效」，"
                "不要声称已写入，也不要重试。")
    if ctx.forced_propose is not None and ctx.forced_propose():
        return ("已交人工执行：本组属 L2/unknown 高危批提案（组内任一语句达 L2 或评估未完成），"
                f"批准后不自动写库，{len(statements)} 条 SQL 与参数已登记改动日志"
                "（action=proposed，awaiting_manual，同一组）。请如实向用户汇报"
                "「已提交人工执行、尚未生效」，不要声称已写入，也不要重试。")
    eids = []
    for sql, rows in entries:
        rows = mask_rows_for_sql(sql, rows, ctx.masker)
        eids.append(ctx.log.register(sql, rows, kind="write"))
    joined = "；".join(f"{eid}：{ctx.log.get(eid).summary()}" for eid in eids)
    return (f"批写完成（{len(entries)} 条原子提交，要么全生效要么全不生效），"
            f"证据已登记：{joined}")


async def _tool_get_metrics(args: dict, ctx: _RunCtx) -> str:
    """按登记口径取指标。表名/时间列/表达式全是代码内受信标识符；
    group_by 只接受该指标的建议维度（白名单），天数是唯一外部值 -> $1 绑定。
    """
    try:
        m = metric(str(args.get("metric", "")))
    except ValueError as exc:
        return f"指标错误：{exc}"
    group_by = args.get("group_by")
    if group_by is not None and str(group_by) not in m["dimensions"]:
        return f"group_by 必须是该指标的建议维度之一：{'/'.join(m['dimensions'])}"
    days = args.get("days", 7)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        return "days 必须是 1-365 的整数。"
    dim = str(group_by) if group_by is not None else m["date_col"]
    selects = ([dim] if dim else []) + [f"{m['expr']} AS value"]
    sql = f"SELECT {', '.join(selects)} FROM {m['table']}"
    params: tuple = ()
    if m["date_col"]:
        sql += f" WHERE {m['date_col']} >= CURRENT_DATE - $1::int"
        params = (days,)
    if dim:
        sql += f" GROUP BY {dim} ORDER BY {dim}"
    rows = await ctx.db_fetch(sql, params)
    rows = mask_rows_for_sql(sql, rows, ctx.masker)
    eid = ctx.log.register(sql, rows, params=list(params) or None)
    return f"指标取数完成，证据 {eid} 已登记：{ctx.log.get(eid).summary()}"


def _detail_dict(row: dict) -> dict:
    """query_audit 的 detail 是 asyncpg 原样返回的 jsonb——字符串形态，需解析。"""
    import json as _json

    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            return _json.loads(detail)
        except ValueError:
            return {}
    return detail if isinstance(detail, dict) else {}


async def _tool_list_recent_writes(args: dict, ctx: _RunCtx) -> str:
    """最近获批准的写操作审计摘要；只作上下文，不登记证据（不可被报告引用）。

    归属过滤（10-08 台账 457）：非最高管理员只能看到自己提交的批准记录，
    否则他人的 SQL 头与审批人名字会进他的模型上下文。admin 看全量（业主
    单人模式，与 list_tasks / execute_rollback 同一口径）。存量审计行没有
    requested_by（10-08 才补记），一律视为不属我——fail-closed，随新审批自然补齐。
    """
    limit = args.get("limit", 5)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        limit = 5
    rows = await query_audit(action=ACT_DBAGENT_WRITE_APPROVAL, limit=50)
    approved = [(r, _detail_dict(r)) for r in rows
                if _detail_dict(r).get("decision") == "approved"]
    if not ctx.is_admin:
        approved = [(r, d) for r, d in approved
                    if d.get("requested_by") == ctx.actor]
    if not approved:
        return "最近没有获批准的写操作。"
    lines = [f"- {r['created_at'][:16]} by {r['user_id']}: {d.get('sql_head', '')}"
             for r, d in approved[:limit]]
    return "最近获批准的写操作（审计摘要，仅上下文，不可作报告证据）：\n" + "\n".join(lines)


async def _tool_submit_plan(args: dict, ctx: _RunCtx) -> str:
    """计划门：挂起等人工批准。无回调（CLI 等场景）时降级为直接放行。"""
    goal = str(args.get("goal", ""))[:200]
    steps = [str(s).strip()[:120] for s in args.get("steps", []) if str(s).strip()][:8]
    if not goal or len(steps) < 2:
        return "计划需要一句话目标（goal）和至少两步（steps）。"
    if ctx.confirm_plan is None:
        return "计划通道未开启，直接继续执行。"
    approved = bool(await _maybe_await(ctx.confirm_plan(goal, steps)))
    ctx.plan_approved = approved
    return ("计划已获人工批准，请按步骤执行。" if approved
            else "计划被人工否决。请调整方案后重新提交，或改为只读汇报。")


async def _tool_rollback_change(args: dict, ctx: _RunCtx) -> str:
    """按 HG 编号回滚一笔已应用变更（changes 模块为唯一权威实现）。

    冲突检查先行，干净时走人工审批门（展示整组全量反向操作）后整体执行；但组风险达
    L2/unknown 时批准不等于执行（P1-5，台账 449 后半）→ 交中控台「回滚」按钮人工执行。
    """
    from . import changes

    ref_text = str(args.get("change_ref") or "").strip()
    if ctx.propose_only:
        # 影子期（#10）：自动回滚同样写库，与"批准后不执行"矛盾——停用，反向操作交 DBA 手工执行
        return ("影子期（propose-only）：自动回滚已停用。反向操作见改动日志"
                f"（{ref_text} 条目），请向用户说明由人工执行。")
    if changes.is_latest_word(ref_text):
        change = await changes.fetch_latest_applied()
    else:
        num = changes.parse_ref(ref_text)
        change = await changes.fetch_change(num) if num else None
    if change is None:
        return (f"没有找到变更 {ref_text or '(空)'}。编号见改动日志（HG1 起），"
                "或用 latest 表示最近一笔已应用的变更。")
    ref = changes.ref_of(change["change_id"])
    if change.get("action") != "apply" or change.get("status") != "applied":
        return (f"{ref} 当前状态为 {change.get('status')}，"
                "只有已应用（applied）的变更可以回滚。")
    # 归属校验（业主 2026-10-05 拍板）：非最高管理员只能回滚自己发起的变更，存量无主行同样拒绝
    if ctx.actor and not ctx.is_admin and (change.get("actor") or "") != ctx.actor:
        return (f"回滚被拒：{ref} 由 {(change.get('actor') or '未知发起人')} 登记，"
                "非最高管理员只能回滚自己发起的变更。")
    conflicts = await changes.rollback_conflicts(change)
    if conflicts:
        return (f"回滚 {ref} 被冲突检查阻止，未执行任何操作——这些行在变更之后又被修改过，"
                "回滚会覆盖新数据：\n- " + "\n- ".join(conflicts))
    # 全量反向操作清单（10-08 台账 449）：审批门此前只展示第一条，批准范围比真实发生的窄
    group_id = change.get("group_id")
    members = await changes.fetch_group(group_id) if group_id else [change]
    if group_id and not members:
        return f"{ref} 所属批提案组 {group_id} 取不到成员，未执行任何操作。"
    members = list(members)
    bad = [changes.ref_of(m["change_id"]) for m in members if m.get("status") != "applied"]
    if bad:
        return (f"{ref} 属于批提案组，组内 {'、'.join(bad)} 当前不是 applied 状态，"
                "整组回滚已停止（组是原子语义，不能拆着回滚）。")
    empty = [changes.ref_of(m["change_id"]) for m in members if not (m.get("reverse_ops") or [])]
    if empty:
        return (f"{'、'.join(empty)} 没有可自动执行的反向操作，"
                "需按改动日志人工处理，本次未执行任何操作。")
    ops: list[dict] = []
    for member in reversed(members):        # 倒序 = 与执行相反的顺序回滚
        ops.extend(member.get("reverse_ops") or [])
    gscope = f"，同组 {len(members)} 笔一起回滚" if group_id else ""
    reason = (f"回滚 {ref}：共 {len(ops)} 条反向操作，冲突检查通过"
              f"（原变更影响 {change.get('affected')} 行{gscope}）")
    if ctx.confirm_write_batch is not None:
        approved = bool(await _maybe_await(
            ctx.confirm_write_batch([{"sql": o["sql"], "params": o.get("params") or []}
                                     for o in ops], reason)))
    else:
        # 无批门（CLI/受信路径）：退回单条门，但 reason 已写明总条数，不虚报范围
        approved = bool(await _maybe_await(
            ctx.confirm_write(ops[0]["sql"], ops[0].get("params") or [], reason)))
    if not approved:
        return "回滚已被人工否决，未执行。"
    if ctx.forced_propose is not None and ctx.forced_propose():
        # P1-5 同款（台账 449 后半）：反向操作达 L2/unknown 时批准 ≠ 执行。这里不登记 proposed
        # 行——反向操作本就在原 applied 行的 reverse_ops 里，中控台「回滚」按钮走同一个
        # execute_rollback（整组原子 + 冲突检查）；再登记只会落进 awaiting_manual 且无执行入口。
        scope = f"，同组 {len(members)} 笔一起" if group_id else ""
        return (f"已交人工执行：{ref} 的反向操作风险等级为 L2/unknown（影响行数大或"
                "风险评估未完成），批准后不自动回滚。冲突检查已通过，请在中控台改动日志"
                f"对 {ref} 点「回滚」由人工执行（共 {len(ops)} 条反向操作{scope}）。"
                "请如实向用户汇报「回滚已提交人工执行、数据尚未恢复」，不要声称已回滚，也不要重试。")
    # 审计 actor 带真实审批人（深审 A17：此前硬编码 dbagent_tool，审批人丢失）
    actor = "dbagent_tool"
    if ctx.approval_actor is not None:
        actor = f"agent:{ctx.approval_actor() or 'unknown'}"
    result = await changes.execute_rollback(
        change["change_id"], actor,
        owner_actor=ctx.actor or None, owner_is_admin=ctx.is_admin)
    return f"回滚完成：{result['ref']} 共 {result['ops']} 条反向操作已执行，数据已恢复。"


def _tool_submit_report(args: dict, ctx: _RunCtx) -> tuple[str, dict | None]:
    """终点工具：证据链校验，通过即收稿；失败回喂重写（最多 2 次），再失败抛错。

    证据链是硬闸不放行；重写 2 次是给模型的纠错余量（扩容数据后偶发引用
    笔误，一次机会误杀率偏高，实测两次足够）。
    """
    report = str(args.get("report", ""))
    # 零证据任务免校验：能力问询/寒暄类回答里的数字（"六个工具/每次 10 步"）
    # 来自系统提示的自我描述，不是查询数据——无从引用也不算编造
    if not ctx.log._items:
        ctx.progress("出稿校验：零证据任务（自我描述类），免证据链校验")
        note = "\n\n> 注：本回答未查询数据库，内容未经数据核验。"
        return "", {"report": report + note + "\n\n" + ctx.log.appendix_markdown(),
                    "evidence_log": ctx.log, "usage": dict(ctx.usage)}
    problems = check_citations(report, ctx.log)
    ctx.progress(f"出稿校验：{len(problems)} 处引用问题"
                 + (f"，重写（剩余 {ctx.report_retries_left} 次）"
                    if problems else ""))
    if not problems:
        return "", {"report": report + "\n\n" + ctx.log.appendix_markdown(),
                    "evidence_log": ctx.log, "usage": dict(ctx.usage)}
    if ctx.report_retries_left <= 0:
        raise ReportRejected(problems)
    ctx.report_retries_left -= 1
    return (f"报告被证据链校验拒绝，逐条修正后重新 submit_report（还可重写 "
            f"{ctx.report_retries_left} 次）。"
            "修正要点：数字只保留查询结果原值并紧跟 [E#] 方括号引用；"
            "平均数/百分比/差值等派生数字全部删除或改文字：\n"
            + "\n".join(problems)), None
