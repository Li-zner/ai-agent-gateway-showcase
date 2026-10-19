"""
数据库维护模块
- 对话记忆：全量写入一个月，满月后压缩归档
- 用户画像：按需读写，清理过期画像
- 语义缓存：清理低频缓存
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.logging import setup_logging

logger = setup_logging()

# ============================================================
# 配置
# ============================================================
COMPRESS_AFTER_DAYS = 30       # 对话记忆 30 天后压缩
PROFILE_EXPIRE_DAYS = 90        # 用户画像 90 天未更新则清理
CACHE_CLEAN_DAYS = 60           # 语义缓存 60 天未命中则清理
CACHE_HARD_MAX_DAYS = 180       # 语义缓存硬过期：命中再高超过 180 天也清理（防热门条目永生）
BATCH_SIZE = 500                # 每批处理条数


def _utc_now() -> datetime:
    """返回 aware UTC 当前时间，匹配迁移后的 timestamptz 时间列。"""
    return datetime.now(timezone.utc)


async def compress_old_conversations():
    """
    压缩 30 天前的对话记忆：
    1. 按 (user_id, conversation_id) 分组
    2. 将整组对话合并为一条摘要
    3. 删除原始详细记录
    4. 插入压缩后的记录
    """
    pool = await get_pool()
    cutoff = _utc_now() - timedelta(days=COMPRESS_AFTER_DAYS)
    logger.info(f"开始压缩 {cutoff.date()} 之前的对话记忆...")

    async with pool.acquire(timeout=5) as conn:
        # 获取需要压缩的对话列表（分组统计）
        # P1 修复：排除已压缩的摘要记录（role='system'）——否则摘要记录（created_at
        # 用 last_msg 旧时间，恒 < cutoff）下一轮被再次选中，msg_count=1 走单条删除
        # 分支把摘要本身删掉，30 天前的对话历史两个维护周期后永久丢失
        rows = await conn.fetch("""
            SELECT user_id, conversation_id, COUNT(*) as msg_count,
                   MIN(created_at) as first_msg, MAX(created_at) as last_msg
            FROM conversation_memories
            WHERE created_at < $1 AND role != 'system'
            GROUP BY user_id, conversation_id
            ORDER BY MIN(created_at)
        """, cutoff)

        if not rows:
            logger.info("没有需要压缩的对话记忆")
            return

        total_compressed = 0
        next_pause_at = BATCH_SIZE * 5
        for row in rows:
            if row["msg_count"] < 2:
                # 只有一条消息的直接删除（同样排除摘要记录，双保险）
                await conn.execute("""
                    DELETE FROM conversation_memories
                    WHERE user_id=$1 AND conversation_id=$2 AND created_at < $3
                      AND role != 'system'
                """, row["user_id"], row["conversation_id"], cutoff)
                total_compressed += row["msg_count"]
                continue

            total_compressed += await _compress_one_conversation(conn, row, cutoff)

            # 每批暂停一下，避免长时间锁表。用下一暂停点比较而非取模：
            # 单对话 msg_count 跨过 BATCH_SIZE*5 整数倍时取模恒不为 0，
            # 节流会整轮失效（2026-09-15 审查 P2）
            if total_compressed >= next_pause_at:
                await asyncio.sleep(0.1)
                next_pause_at += BATCH_SIZE * 5

        logger.info(f"对话压缩完成，共压缩 {total_compressed} 条消息，{len(rows)} 个对话")


async def _compress_one_conversation(conn, row, cutoff) -> int:
    """压缩单个对话：取消息 -> 生成摘要 JSON -> 同事务 DELETE 原始记录 + INSERT 摘要。

    返回本次压缩的消息条数。原子性见 P0 #44（中断回滚防对话数据丢失）。
    """
    # 获取该对话的所有消息内容（id 决胜：同一事务写入的消息 created_at 相同，缺 id 会乱序）
    # 排除摘要记录：混合对话（旧消息已压缩 + 又有新消息跨过 cutoff）不把旧摘要当原始消息重复压缩
    msg_rows = await conn.fetch("""
        SELECT role, content, created_at
        FROM conversation_memories
        WHERE user_id=$1 AND conversation_id=$2 AND created_at < $3 AND role != 'system'
        ORDER BY created_at ASC, id ASC
    """, row["user_id"], row["conversation_id"], cutoff)

    # 空行守卫（2026-09-09 审查 P1）：分组查询与明细查询之间无锁，并发维护/另一实例
    # 已压缩时明细可为 0 行，msg_rows[0] 直接 IndexError 并中断当日全部后续维护
    if not msg_rows:
        return 0
    msg_count = len(msg_rows)
    first_time = msg_rows[0]["created_at"].strftime("%Y-%m-%d %H:%M")
    last_time = msg_rows[-1]["created_at"].strftime("%Y-%m-%d %H:%M")

    # 提取用户主要关注点
    user_messages = [m["content"][:200] for m in msg_rows if m["role"] == "user"]
    user_summary = "；".join(user_messages[:5])
    if len(user_messages) > 5:
        user_summary += f"……（共{len(user_messages)}条用户消息）"

    compressed_content = json.dumps({
        "type": "compressed",
        "original_count": msg_count,
        "period": f"{first_time} ~ {last_time}",
        "summary": f"用户在该对话中主要关注：{user_summary}",
        "full_text": "\n".join([f"{m['role']}: {m['content']}" for m in msg_rows])
    }, ensure_ascii=False)

    # 原子压缩：DELETE 与 INSERT 必须同事务（P0 #44），中断时回滚防对话数据丢失
    async with conn.transaction():
        # 删除原始详细记录
        # DELETE 必须与 SELECT 同口径排除 role='system'（2026-09-05 修复）：
        # 否则混合对话（已有旧摘要 + 新消息又跨过 cutoff）会把旧摘要一并删除，
        # 而新摘要只含新消息——旧摘要的 full_text 原文永久丢失（与 67-71 行单条分支保持一致）。
        await conn.execute("""
            DELETE FROM conversation_memories
            WHERE user_id=$1 AND conversation_id=$2 AND created_at < $3
              AND role != 'system'
        """, row["user_id"], row["conversation_id"], cutoff)

        # 插入压缩后的摘要记录
        await conn.execute("""
            INSERT INTO conversation_memories
            (user_id, conversation_id, role, content, created_at)
            VALUES ($1, $2, 'system', $3, $4)
        """, row["user_id"], row["conversation_id"], compressed_content, row["last_msg"])

    return msg_count


async def clean_expired_profiles():
    """
    清理过期用户画像：
    - 删除 90 天未更新的画像
    """
    pool = await get_pool()
    cutoff = _utc_now() - timedelta(days=PROFILE_EXPIRE_DAYS)
    async with pool.acquire(timeout=5) as conn:
        result = await conn.execute("""
            DELETE FROM user_profiles
            WHERE updated_at < $1
        """, cutoff)
        deleted = result.split()[-1] if result else "0"
        logger.info(f"清理过期用户画像: {deleted} 条")
        # 2026-09-22 审阅 P2：会话画像按 updated_at 直删，判据与 user_profiles 一致。
        # 原先这里挂的是 `NOT EXISTS(conversation_memories)` 的"孤儿"条件，而
        # compress_old_conversations 压缩时恰恰保留了 role='system' 的摘要行
        # （见同文件压缩分支的 DELETE 口径）——被压缩过的会话永远"还有记忆行"，
        # NOT EXISTS 恒不成立，conversation_profiles 只增不减；不传 conversation_id
        # 的每次请求还会新建一行（memory_manager._upsert_conversation_profile），
        # 于是无界增长。画像本身可由下一轮对话重新提取，不是不可再生数据，
        # 超期即删与用户画像同留期（PROFILE_EXPIRE_DAYS）。
        expired_conv = await conn.execute(
            "DELETE FROM conversation_profiles WHERE updated_at < $1", cutoff)
        conv_count = expired_conv.split()[-1] if expired_conv else "0"
        logger.info(f"清理过期会话画像: {conv_count} 条")


async def clean_stale_cache():
    """
    清理低频语义缓存：
    - 创建超 60 天且命中数 <2（冷条目），或超 180 天硬过期（防热门条目永生）
    """
    pool = await get_pool()
    cutoff = _utc_now() - timedelta(days=CACHE_CLEAN_DAYS)
    async with pool.acquire(timeout=5) as conn:
        hard_cutoff = _utc_now() - timedelta(days=CACHE_HARD_MAX_DAYS)
        result = await conn.execute("""
            DELETE FROM semantic_cache
            WHERE (created_at < $1 AND hit_count < 2) OR created_at < $2
        """, cutoff, hard_cutoff)
        deleted = result.split()[-1] if result else "0"
        logger.info(f"清理低频语义缓存: {deleted} 条（硬过期阈值 {CACHE_HARD_MAX_DAYS} 天）")


async def _run_maintenance_step(label: str, task) -> None:
    """单步维护隔离：一个步骤失败不能阻断后续清理。"""
    try:
        await task()
    except Exception as e:
        logger.error(f"数据库维护步骤失败: {label}: {e}", exc_info=True)


async def run_maintenance():
    """执行全部维护任务；每个步骤独立隔离，失败不阻断后续步骤。"""
    logger.info("开始数据库维护...")
    await _run_maintenance_step("对话压缩", compress_old_conversations)
    await _run_maintenance_step("画像清理", clean_expired_profiles)
    await _run_maintenance_step("缓存清理", clean_stale_cache)

    async def _settle():
        from ..payment.deferred import settle_pending_deductions
        settled = await settle_pending_deductions()
        if settled:
            logger.info(f"待结算扣费单已补偿结算: {settled} 张")

    await _run_maintenance_step("待结算补偿", _settle)
    logger.info("数据库维护完成")


async def maintenance_loop(interval_hours: int = 24):
    """定时维护循环

    2026-09-09 审查 P1：多实例拓扑下每个实例的 lifespan 都起本循环，原先并发维护
    既会重复压缩（双份摘要）又会互触发 IndexError。Redis NX 锁对齐 slow_query_watch
    模式：拿不到锁的实例本轮跳过。
    ponytail: 锁 TTL 1 小时为维护时长上界且无续期；超 1h 的极端维护可能被另一实例
    并发进入，后果只是重复压缩（幂等），不丢数据——升级路径是 token 续期锁。
    """
    while True:
        try:
            redis = await get_redis()
            token = uuid.uuid4().hex
            if await redis.set("lock:db_maintenance", token, nx=True, ex=3600):
                try:
                    await run_maintenance()
                finally:
                    # 仅持有者可释放（Lua 比对 token），防误删他人锁
                    await redis.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] then "
                        "return redis.call('del', KEYS[1]) else return 0 end",
                        1, "lock:db_maintenance", token,
                    )
            else:
                logger.info("本轮维护由其他实例执行，跳过")
        except Exception as e:
            # 单次 Redis/锁异常不能终止后台维护协程；下一周期继续尝试。
            logger.warning(f"数据库维护循环异常（下轮重试）: {e}")
        logger.info(f"下次维护在 {interval_hours} 小时后")
        await asyncio.sleep(interval_hours * 3600)
