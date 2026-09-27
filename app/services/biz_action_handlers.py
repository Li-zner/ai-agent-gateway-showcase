"""全业务写动作的执行器与回滚器（P2 首批：账号启停、支付渠道开关、钱包提案）。

与 rag_rerank_remediation 同一套范式：事务内 `SELECT ... FOR UPDATE` 读原值 →
原值写进 result.baseline 快照 → 参数化 UPDATE → 回读复核 → verification.passed。
回滚只认快照里的原值，不重新推断，避免"审批-执行"窗口内被人改过之后回错。

三个必须写下来的边界：
1. 账号启停后必须调 invalidate_user_cache：鉴权读的是 Redis 里的用户快照
   （middleware/auth.py:253 get_cached_user，TTL 300 秒），不失效则封禁最长
   5 分钟不生效，而界面上动作已经显示"执行成功"。
2. 拒绝动 role=admin 的账号：worker 用的是应用主账号（超级用户），数据库层
   拦不住，自锁与团队锁死只能靠这里挡。控制台管理员本人也是 role=admin，
   所以这一条同时覆盖了"禁止禁用自己"。
3. 支付渠道只碰 is_active，不碰 fee_rate/min_amount/max_amount——费率是资金
   口径，走 payment/admin_routes.py 的既有端点（该端点本期保留为已知旁路）。
"""
from __future__ import annotations

# 不允许通过动作面启停的角色（防把控制台管理员批量锁死）
PROTECTED_ROLES = frozenset({"admin"})


async def set_user_active(params: dict) -> dict:
    """禁用或启用单个账号，返回带原值快照的执行结果。"""
    from ..core.db import get_pool
    from ..core.quota import invalidate_user_cache

    username = str(params["username"])
    active = bool(params["active"])
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT username, role, is_active FROM users "
                "WHERE username=$1 FOR UPDATE",
                username,
            )
            if row is None:
                raise ValueError(f"账号不存在: {username}")
            if (row["role"] or "user") in PROTECTED_ROLES:
                raise ValueError("管理员账号不允许通过动作面启停")
            was_active = bool(row["is_active"])
            if was_active is active:
                return {
                    "result": {"was_active": was_active, "changed": False},
                    "verification": {"passed": True, "reason": "状态未变化"},
                }
            await conn.execute(
                "UPDATE users SET is_active=$2 WHERE username=$1",
                username, active,
            )
            after = await conn.fetchval(
                "SELECT is_active FROM users WHERE username=$1", username)
    await invalidate_user_cache(username)
    passed = bool(after) is active
    return {
        "result": {
            "was_active": was_active,
            "changed": True,
            "after": {"is_active": after},
        },
        "verification": {
            "passed": passed,
            "reason": "回读与目标状态一致" if passed else "回读与目标状态不一致",
        },
    }


async def rollback_user_active(params: dict, result: dict) -> dict:
    """按快照恢复 is_active 原值；本次未改动时直接判定回滚成功。

    2026-09-28 批1 P2-11：回滚前 FOR UPDATE 重读并复核 PROTECTED_ROLES——
    动作与回滚之间账号可能已被提权为 admin，照旧回滚就绕过了"管理员不允许启停"
    的保护；顺带补存在性判断（用户不存在不再靠"回读不一致"兜底）。两种拒绝都按
    框架约定返回 passed=False 转人工，不抛业务错。
    """
    from ..core.db import get_pool
    from ..core.quota import invalidate_user_cache

    was_active = result.get("was_active")
    if was_active is None or not result.get("changed"):
        return {"passed": True, "skipped": True,
                "reason": "未捕获原值或本次未改动，无需回滚"}
    username = str(params["username"])
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE username=$1 FOR UPDATE", username)
            if row is None:
                # 框架约定：回滚函数不抛业务错（路由层无 except ValueError，抛了变 500），
                # 用 passed=False 让动作标 failed、原因落 rollback_result，转人工
                return {"passed": False, "reason": f"用户不存在，无法回滚: {username}"}
            if (row["role"] or "user") in PROTECTED_ROLES:
                return {"passed": False,
                        "reason": f"账号 {username} 已是受保护角色，"
                                  "禁止回滚启停（提权发生在动作之后，请人工处理）"}
            await conn.execute(
                "UPDATE users SET is_active=$2 WHERE username=$1",
                username, bool(was_active),
            )
            restored = await conn.fetchval(
                "SELECT is_active FROM users WHERE username=$1", username)
    await invalidate_user_cache(username)
    return {"passed": bool(restored) is bool(was_active),
            "restored_active": restored}


async def set_channel_active(params: dict) -> dict:
    """开启或关闭一个支付渠道（仅 is_active，不含费率）。"""
    from ..core.db import get_pool

    channel_code = str(params["channel_code"])
    active = bool(params["active"])
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT channel_code, is_active FROM payment_channels "
                "WHERE channel_code=$1 FOR UPDATE",
                channel_code,
            )
            if row is None:
                raise ValueError(f"渠道不存在: {channel_code}")
            was_active = bool(row["is_active"])
            if was_active is active:
                return {
                    "result": {"was_active": was_active, "changed": False},
                    "verification": {"passed": True, "reason": "状态未变化"},
                }
            await conn.execute(
                "UPDATE payment_channels SET is_active=$2, "
                "updated_at=CURRENT_TIMESTAMP WHERE channel_code=$1",
                channel_code, active,
            )
            after = await conn.fetchval(
                "SELECT is_active FROM payment_channels WHERE channel_code=$1",
                channel_code)
    passed = bool(after) is active
    return {
        "result": {
            "was_active": was_active,
            "changed": True,
            "after": {"is_active": after},
        },
        "verification": {
            "passed": passed,
            "reason": "回读与目标状态一致" if passed else "回读与目标状态不一致",
        },
    }


async def rollback_channel_active(params: dict, result: dict) -> dict:
    """按快照恢复渠道 is_active 原值。"""
    from ..core.db import get_pool

    was_active = result.get("was_active")
    if was_active is None or not result.get("changed"):
        return {"passed": True, "skipped": True,
                "reason": "未捕获原值或本次未改动，无需回滚"}
    channel_code = str(params["channel_code"])
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        await conn.execute(
            "UPDATE payment_channels SET is_active=$2, "
            "updated_at=CURRENT_TIMESTAMP WHERE channel_code=$1",
            channel_code, bool(was_active),
        )
        restored = await conn.fetchval(
            "SELECT is_active FROM payment_channels WHERE channel_code=$1",
            channel_code)
    return {"passed": bool(restored) is bool(was_active),
            "restored_active": restored}
