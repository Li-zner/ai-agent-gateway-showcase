"""任务生成阶段编排（2026-09-20 CORE-1 拆段：从 runner.py 外移，行为等价）。

run_agent_task 受 ≤80 行/≤600 行门禁约束，把"缓存上下文 → generating 迁移 →
CORE-1 超时闹钟 → 缓存闸 → 意图路由 → 建消息 → ReAct"的编排主体放这里。
对 runner 私有步骤的回读一律 `getattr(R, "...")` 晚绑定——既有测试对
runner 模块属性的 monkeypatch 依然生效，不要改成 from-import 具名绑定。
"""
from ..core.config import TASK_TIMEOUT
from ..core.task_manager import timeout_deadline


async def gate_and_route(task_id, username, user_query, mm,
                         conversation_id, params, cache_state, mark_route):
    """阶段 1~2：语义缓存闸 → 意图路由 → 取密钥/建消息。

    params 携带 persona_id/file_ids/lang/user_perms。
    返回 (cache_state, done, value, intent)：done=True 时 value 为 outcome
    （None=阶段2 已 fail_task，维持 failed 默认）；done=False 时 value 为
    prepare_task_run 三元组。

    cache_state 是**可变列表** [cache_ctx, cacheable, rebuild_lock_token,
    renew_task]，持锁成功后原位写回而不是重新赋值元组（2026-09-22 审阅 P1）：
    原先靠返回元组回填，_prepare_task_run（DB 取密钥 + 文件解析）一抛异常元组
    就到不了调用方，run_agent_task 的 finally 收到 (None, None) 不释放锁，
    而 _renew_until_done 是 while True 每 15 秒续期——同一 ctx 的重建锁被永久
    占住，后续所有同上下文请求恒返回"服务繁忙"。
    """
    from . import runner as R  # 晚绑定：测试对 runner 模块属性的 monkeypatch 生效
    persona_id, file_ids, lang, user_perms = (
        params["persona_id"], params["file_ids"], params["lang"], params["user_perms"])
    _cache_ctx, cacheable = cache_state[0], cache_state[1]
    if cacheable:
        token, renew, served = await getattr(R, "_enter_cache_gate")(
            task_id, username, user_query, mm, _cache_ctx)
        # 拿到锁的第一件事是登记，不是判 served：登记之后任何异常路径都由调用方
        # finally 取消续期协程并释放锁（失败才靠 45s TTL 自愈）
        cache_state[2], cache_state[3] = token, renew
        if served:
            return cache_state, True, "success", ""
    handled, intent = await getattr(R, "_maybe_simple_task")(
        task_id, username, conversation_id, user_query,
        persona_id, file_ids, _cache_ctx if cacheable else None, lang, user_perms)
    if handled:
        mark_route("task_simple")
        return (cache_state, True,
                await getattr(R, "_resolve_handled_outcome")(task_id, username), "")
    prepared = await getattr(R, "_prepare_task_run")(
        task_id, username, mm, user_query, persona_id, file_ids, intent, lang)
    if prepared is None:
        return cache_state, True, None, ""
    return cache_state, False, prepared, intent


async def prepare_generation(task_id, username, user_query, mm,
                             conversation_id, params, mark_route, cache_state):
    """阶段 0~3：缓存上下文 → generating 迁移 → CORE-1 闹钟 → 闸/路由/建消息 → ReAct。

    返回 (outcome, deadline_task)：outcome 非 None 表示任务在生成前已终结/早退；
    抛出的异常由 run_agent_task 的 except 统一收口。

    cache_state 由调用方创建并传进来（2026-09-22 审阅 P1）：本函数在持锁后会
    继续抛异常（取密钥、文件解析），届时没有任何返回值可言，调用方 finally
    只能依赖它自己那只容器才拿得到 token/renew——在这里新建列表再返回等于没修。
    """
    from . import runner as R  # 晚绑定：测试对 runner 模块属性的 monkeypatch 生效
    persona_id, lang = params["persona_id"], params["lang"]
    _cache_ctx, cacheable = await getattr(R, "_build_cache_ctx")(
        mm, username, persona_id, user_query, lang,
        file_ids=params.get("file_ids"),      # L1：带文件不吃缓存（对齐流式路）
        user_perms=params.get("user_perms"),  # L5：权限维度进缓存键
    )
    # 即便未进缓存闸（cacheable=False），ctx 也要留给 finally 的统一清理。
    cache_state[0], cache_state[1] = _cache_ctx, cacheable
    if not await getattr(R, "update_status")(task_id, "generating"):
        return (await getattr(R, "_resolve_handled_outcome")(task_id, username), None)

    # CORE-1（09-20 审查）：断轮询后僵尸无截止——生成侧挂定时闹钟，到点走
    # CAS 终结 + 跨实例取消标记（已终态则 no-op），finally 撤闹钟。
    deadline_task = getattr(R, "spawn")(  # 晚绑定：runner.spawn 可被测试替换
        timeout_deadline(task_id, TASK_TIMEOUT), name=f"task-timeout-{task_id}")
    try:
        cache_state, done, value, intent = await gate_and_route(
            task_id, username, user_query, mm, conversation_id, params,
            cache_state, mark_route)
        if done:
            return value, deadline_task
        deepseek_api_key, messages, tools = value
        mark_route("task_react")
        await getattr(R, "_run_react_loop")(
            task_id, username, user_query, mm, messages, tools, intent,
            _cache_ctx if cacheable else None, params["user_perms"],
            deepseek_api_key, persona_id=persona_id,
        )
    except BaseException:
        # 闹钟不在返回值里就被抛出来了（含 CancelledError）：不撤掉它会成为
        # 孤儿协程，TASK_TIMEOUT 后独自把已终结的任务再 CAS 一次（09-22 P1）。
        deadline_task.cancel()
        raise
    # ReAct 内部可能因取消提前 return；不能无条件记成功，必须按 Redis
    # 真实终态回传 outcome，再决定是否退还未产出额度。
    return await getattr(R, "_resolve_handled_outcome")(task_id, username), deadline_task
