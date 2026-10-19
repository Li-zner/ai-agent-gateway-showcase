import json
import os
import asyncio
import time
import uuid
from typing import List, Dict
from ..core.redis import get_redis
from ..core.db import get_pool
from ..core.logging import setup_logging
from ..core.config import HISTORY_LIMIT, HISTORY_TTL, HISTORY_SUMMARY_TTL, DB_ACQUIRE_TIMEOUT
from ..core.concurrency import spawn
from ..agents.memory import generate_summary
from ..services.conversation_profiles import (
    extract_profile_updates, get_conversation_profile as load_conversation_profile,
    normalize_conversation_profile,
)

logger = setup_logging()

# 滚动摘要按会话串行（2026-09-10 审查 P2）：并发压缩各自基于同一旧摘要生成，
# 后写者覆盖前者溢出内容（原文在 PG，仅摘要完整性受损）。进程内按 conv 键加锁；
# ponytail: 多实例间仍是 last-writer-wins，彻底解决需 Redis 分布式锁——
# 当前单进程内是最小正确修法。锁带引用计数（2026-09-14 审计 P2）：最后一位
# 使用者退出即从字典移除，防锁字典随会话数无界增长（原实现每会话永留一把锁）。
class _KeyedLock:
    """按会话键的锁 + 在用计数：refs>0 期间不被回收，保证等待者持同一把锁"""

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


_summary_locks: dict = {}


def _get_summary_lock(key: str) -> _KeyedLock:
    kl = _summary_locks.get(key)
    if kl is None:
        kl = _KeyedLock()
        _summary_locks[key] = kl
    kl.refs += 1  # 取锁与计数同协程同步执行（无 await），事件循环内原子
    return kl


def _release_summary_lock(key: str, kl: _KeyedLock) -> None:
    kl.refs -= 1
    if kl.refs <= 0 and _summary_locks.get(key) is kl:
        _summary_locks.pop(key, None)

# ============================================================
# 限流队列：控制 PG 写入并发，防止连接池耗尽
# ============================================================
# 上限 12，并按连接池规模联动取值（P1 #42：防连接池耗尽）。注意默认口径：
# db.py 的 DB_POOL_MAX_SIZE 默认 10（部署配置从未显式设置），故默认并发写为
# 10 // 4 = 2 而非 12；要拿到 12 需同时把 DB_POOL_MAX_SIZE 调到 48 以上，
# 调大是否有益需按实测落库延迟决定（2026-09-19 审查 core P2-3）。
_PG_WRITE_SEMAPHORE = asyncio.Semaphore(
    max(1, min(12, int(os.getenv("DB_POOL_MAX_SIZE", "10")) // 4))
)


# 旅行待补槽状态的生命周期（2026-09-22 审阅 P2）：此前跟历史一样吃
# HISTORY_TTL=24h，而读取侧只判"键是否存在"——昨天没答完的"从哪里出发"，今天
# 随手回个两字词就被填进昨天那个槽。补槽是"追问—回答"的相邻对（adjacency pair），
# 一次规划会话内分钟级闭合；用户十分钟不答即视为放弃这轮规划，重开一次澄清的
# 代价远小于拿昨天的槽位答今天的问题。
PENDING_TRAVEL_TTL_SECONDS = 10 * 60
# 槽值内的写入时间戳：Redis TTL 只在"键没被别的路径复用"时才可信（回填、
# 测试桩、跨版本旧键都不保证过期），读取方按同一常量再判一次新鲜度。
PENDING_TRAVEL_TS_KEY = "saved_at"


class MemoryManager:
    """冷热分层记忆管理器（连接池版）"""

    def __init__(self, user_id: str, conv_id: str, persona_id: str = ""):
        self.user_id = user_id
        self.conv_id = conv_id
        # 批1 B-P1-3：画像写入口要按人格排除评测流量，故 persona 必须传到本对象
        self.persona_id = persona_id
        # Redis 键含 user_id（2026-09-09 审查 P0 IDOR 修复）：键只含 conv_id 时，
        # 知道他人会话 ID 即可读其历史（注入 LLM 上下文）、写其热缓存（跨用户提示注入）；
        # PG 回源本就按 user_id 过滤，热缓存同口径对齐（旧键随 TTL 自然过期）
        self._history_key = f"conv:{self.user_id}:{self.conv_id}"
        self._summary_key = f"conv_summary:{self.user_id}:{self.conv_id}"
        self._pending_travel_key = f"conv_pending_travel:{self.user_id}:{self.conv_id}"

    # ---------- 待补槽状态：旅行澄清轮的短时会话记忆 ----------
    async def get_pending_travel(self) -> Dict:
        """读取旅行待补槽状态；损坏或依赖故障按无状态处理，不阻断聊天。"""
        try:
            redis = await get_redis()
            raw = await redis.get(self._pending_travel_key)
            if not raw:
                return {}
            if isinstance(raw, bytes):
                raw = raw.decode()
            state = json.loads(raw)
            return state if isinstance(state, dict) else {}
        except Exception as e:
            logger.warning(
                f"读取旅行待补槽状态失败（按无状态处理）: {type(e).__name__}"
            )
            return {}

    async def set_pending_travel(self, state: Dict) -> None:
        """写入旅行待补槽状态；短回复下一轮据此恢复上轮推荐意图。

        带时间戳 + 分钟级 TTL（2026-09-22 审阅 P2）：TTL 负责淘汰，saved_at 负责
        新鲜度判定（见 chat_travel_flow._pending_is_fresh），两者缺一都会让隔日的
        短回复填进昨天那个"从哪里出发"。
        """
        try:
            redis = await get_redis()
            payload = dict(state)
            payload[PENDING_TRAVEL_TS_KEY] = time.time()
            await redis.set(
                self._pending_travel_key,
                json.dumps(payload, ensure_ascii=False),
                ex=PENDING_TRAVEL_TTL_SECONDS,
            )
        except Exception as e:
            logger.warning(
                f"写入旅行待补槽状态失败（本轮仍可正常澄清）: {type(e).__name__}"
            )

    async def clear_pending_travel(self) -> None:
        """清除已消费/已取消的旅行待补槽状态。"""
        try:
            redis = await get_redis()
            await redis.delete(self._pending_travel_key)
        except Exception as e:
            logger.warning(
                f"清除旅行待补槽状态失败（TTL 自愈）: {type(e).__name__}"
            )

    # ---------- 读路径：L1(Redis) -> L2(PG) 回填 ----------
    async def get_context(self, limit: int = HISTORY_LIMIT, offset: int = 0) -> List[Dict]:
        """
        获取历史消息（支持分页）
        limit: 返回条数
        offset: 偏移量（用于分页加载早期消息）
        首页（offset=0）会注入滚动压缩摘要【历史摘要】，帮助 LLM 理解更早的上下文。
        """
        redis = await get_redis()

        # 首次加载：走 Redis L1 缓存
        if offset == 0:
            raw = await redis.lrange(self._history_key, -limit, -1)
            if raw:
                msgs = [json.loads(m) for m in raw]
                return await self._inject_summary(redis, msgs)

        # L1 缺失：从 PG 分页加载，回填 Redis
        pg_messages = await self._fetch_from_pg(limit, offset)
        if pg_messages and offset == 0:
            # 仅首页回填 Redis 热缓存。
            # 原子回填（2026-09-07 审查 P1）：裸 pipeline 逐条 RPUSH 时，同会话并发冷启动
            # 会把同一批消息写两遍（LLM 上下文重复，trim 只裁条数不去重）。
            # Lua 内"列表为空才整批写入"原子完成，败者自动跳过，不重不丢。
            await redis.eval(
                self._BACKFILL_LUA, 1, self._history_key,
                HISTORY_TTL, *[json.dumps(m) for m in pg_messages],
            )
            pg_messages = await self._inject_summary(redis, pg_messages)
        return pg_messages

    # 原子回填脚本：KEYS[1]=历史列表，ARGV[1]=TTL，ARGV[2:]=消息 JSON。
    # 列表已存在（含并发回填/新消息先到）即放弃，杜绝重复回填。
    _BACKFILL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
for i = 2, #ARGV do redis.call('RPUSH', KEYS[1], ARGV[i]) end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
return 1
"""

    async def _inject_summary(self, redis, msgs: List[Dict]) -> List[Dict]:
        """把滚动压缩摘要注入为第一条（system），供 LLM 理解更早上下文"""
        try:
            summary = await redis.get(self._summary_key)
            if summary:
                summary = summary.decode() if isinstance(summary, bytes) else summary
                if summary and summary.strip():
                    return [{"role": "system", "content": f"【历史摘要】{summary}"}] + msgs
        except Exception as e:
            logger.warning(f"读取对话摘要失败: {e}")
        return msgs

    async def _fetch_from_pg(self, limit: int, offset: int = 0) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire(timeout=DB_ACQUIRE_TIMEOUT) as conn:
            # A-R1（2026-09-29 对齐）：排除 role='system' 压缩摘要行——db_maintenance
            # 把 30 天前对话的全量 full_text 归档进该行（写侧 DELETE 同口径排除），
            # 不过滤的话 Redis L1 过期回源时几 MB 归档 JSON 整段进 prompt。
            # system 行只有压缩摘要一种来源（滚动摘要在 Redis 不经 PG），过滤安全。
            rows = await conn.fetch(
                "SELECT role, content FROM conversation_memories "
                "WHERE user_id = $1 AND conversation_id = $2 AND role != 'system' "
                "ORDER BY created_at DESC, id DESC LIMIT $3 OFFSET $4",
                self.user_id, self.conv_id, limit, offset
            )
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # ---------- 写路径：同步写 Redis，异步写 PG ----------
    async def save_user_message(self, user_msg: Dict):
        """仅保存用户消息（用于流式开始时立即保存，防止刷新丢失）"""
        redis = await get_redis()
        await redis.rpush(self._history_key, json.dumps(user_msg))
        await redis.expire(self._history_key, HISTORY_TTL)
        await self._trim_and_compress(redis)

    async def save_messages(self, user_msg: Dict, assistant_msg: Dict):
        """双写：Redis（热，按条数保留） + PG（冷，全量落库）  去重用户消息"""
        redis = await get_redis()
        # 去重：流式开始时可能已 save_user_message；若末条已是同一用户消息则跳过，
        # 避免完整对话历史里用户消息重复出现、污染 LLM 上下文（P1 修复）。
        last = None
        try:
            last_raw = await redis.lindex(self._history_key, -1)
            if last_raw is not None:
                last = json.loads(last_raw)
        except Exception as e:
            logger.debug(f"去重检查读取末条消息失败: {e}")
        # 先写用户消息（除非已存在），再写助手消息，保持顺序
        if last != user_msg:
            await redis.rpush(self._history_key, json.dumps(user_msg))
        await redis.rpush(self._history_key, json.dumps(assistant_msg))
        await redis.expire(self._history_key, HISTORY_TTL)
        await self._trim_and_compress(redis)

        # spawn 持强引用：裸 create_task 的后台任务可被 GC 中途回收（2026-09-07 审查 P2）
        spawn(self._save_to_pg(user_msg, assistant_msg), name=f"save_pg:{self.conv_id}")

    # ---------- 条数限制 + 滚动压缩摘要 ----------
    # 摘要成功前不裁剪热历史。Lua 先比对列表头仍是同一批 overflow，
    # 再原子 LTRIM；并发 append 只会追加尾部，不会造成错裁。
    _TRIM_VERIFIED_LUA = """
local count = #ARGV
if count == 0 then return 0 end
local current = redis.call('LRANGE', KEYS[1], 0, count - 1)
if #current ~= count then return 0 end
for i = 1, count do
    if current[i] ~= ARGV[i] then return 0 end
end
redis.call('LTRIM', KEYS[1], count, -1)
return 1
"""

    async def _trim_and_compress(self, redis):
        """保留最近 HISTORY_LIMIT 条；摘要成功后才裁剪旧消息。"""
        try:
            total = await redis.llen(self._history_key)
            if total <= HISTORY_LIMIT:
                return
            overflow = await redis.lrange(
                self._history_key, 0, total - HISTORY_LIMIT - 1
            )
            if overflow:
                spawn(
                    self._compress_and_trim(),
                    name=f"compress:{self.conv_id}",
                )
        except Exception as e:
            logger.warning(f"对话历史条数裁剪失败: {e}")

    async def _compress_and_trim(self) -> None:
        """会话锁内「重读溢出 → 生成摘要 → 验证式裁剪」串行完成。

        2026-10-06 债转修：溢出快照不再由 _trim_and_compress 带入（拿快照到
        真正执行之间可能已被并发批次裁掉前缀），改为锁内重读——否则后写批次的
        摘要会并入已裁消息、其裁剪又被 TRIM 验证拒绝，摘要与热历史对同一批
        消息重叠。多实例间仍是 ponytail 既定的 last-writer-wins 上限。
        """
        lock = _get_summary_lock(self._summary_key)
        acquired = False
        try:
            await lock.lock.acquire()
            acquired = True
            redis = await get_redis()
            total = await redis.llen(self._history_key)
            if total <= HISTORY_LIMIT:
                return
            overflow = await redis.lrange(
                self._history_key, 0, total - HISTORY_LIMIT - 1
            )
            if not overflow:
                return
            if not await self._compress_overflow(overflow):
                return
            await redis.eval(
                self._TRIM_VERIFIED_LUA,
                1, self._history_key, *overflow,
            )
        except Exception as e:
            logger.warning(f"滚动压缩/裁剪失败（不影响主流程，下轮重试）: {e}")
        finally:
            if acquired:
                lock.lock.release()
            # 等待锁期间被取消也必须归还引用计数，避免锁字典永久泄漏。
            _release_summary_lock(self._summary_key, lock)

    async def _compress_overflow(self, overflow_msgs) -> bool:
        """滚动摘要：旧摘要 + 本次溢出 → LLM 重新生成摘要并写回。

        仅供 _compress_and_trim 在已持会话锁时调用（锁语义 2026-10-06 上移）；
        失败返回 False，调用方据此跳过裁剪（摘要成功前不裁剪热历史）。
        """
        redis = await get_redis()
        old = await redis.get(self._summary_key)
        old_text = old.decode() if isinstance(old, bytes) else (old or "")
        combined = []
        if old_text and old_text.strip():
            combined.append({"role": "system", "content": f"此前摘要：{old_text}"})
        combined.extend(json.loads(m) for m in overflow_msgs)
        new_summary = await generate_summary(combined, username=self.user_id)
        if new_summary and new_summary.strip():
            await redis.set(self._summary_key, new_summary, ex=HISTORY_SUMMARY_TTL)
            return True
        return False

    async def _save_to_pg(self, user_msg: Dict, assistant_msg: Dict):
        """异步写入 PostgreSQL（带限流与错误处理）。

        2026-09-14 审计 P1：PG 写失败原先只记日志——Redis 热缓存 TTL 到期后该轮
        长期记忆永久丢失。补有限重试（共 3 次，1s/2s 退避），覆盖连接池抖动/
        瞬时断连等瞬态故障；重试在本 spawn 任务内 sleep，不阻塞回答主流程。
        ponytail: 仍非持久 outbox——进程在 3 次重试窗口内崩溃仍会丢，彻底解决
        需落盘 outbox + 独立投递循环（登记待专项）。
        """
        async with _PG_WRITE_SEMAPHORE:  # 限流：最多 12 个并发写入（见定义处，P1 #42）
            pool = await get_pool()
            # 2026-09-30 校准修复（B1）：5s 硬编码 → DB_ACQUIRE_TIMEOUT
            last_err = None
            committed = False
            user_uid = uuid.uuid4().hex
            assistant_uid = uuid.uuid4().hex
            for attempt in range(3):
                try:
                    async with pool.acquire(timeout=DB_ACQUIRE_TIMEOUT) as conn:
                        async with conn.transaction():
                            await conn.execute(
                                "INSERT INTO conversation_memories "
                                "(user_id, conversation_id, role, content, message_uid) "
                                "VALUES ($1, $2, $3, $4, $5) "
                                # 唯一索引是部分的（WHERE message_uid IS NOT NULL），
                                # 冲突子句必须带上同一谓词，否则 PG 推断不到索引 → 42P10
                                "ON CONFLICT (message_uid) "
                                "WHERE message_uid IS NOT NULL DO NOTHING",
                                self.user_id, self.conv_id, "user",
                                user_msg["content"], user_uid,
                            )
                            await conn.execute(
                                "INSERT INTO conversation_memories "
                                "(user_id, conversation_id, role, content, message_uid) "
                                "VALUES ($1, $2, $3, $4, $5) "
                                "ON CONFLICT (message_uid) "
                                "WHERE message_uid IS NOT NULL DO NOTHING",
                                self.user_id, self.conv_id, "assistant",
                                assistant_msg["content"], assistant_uid,
                            )
                    committed = True
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        await asyncio.sleep(1 * (attempt + 1))  # 退避 1s / 2s
            if not committed:
                logger.warning(f"PG 写入失败（重试 3 次后放弃，本轮长期记忆可能丢失）: {last_err}")
                return
            # 画像更新独立于记忆写入重试：画像失败不得触发整批消息再次 INSERT。
            async with pool.acquire(timeout=DB_ACQUIRE_TIMEOUT) as conn:
                try:
                    await self._update_profile_async(
                        conn, user_msg["content"], assistant_msg["content"]
                    )
                except Exception as e:
                    logger.warning(
                        f"用户画像更新失败（对话记忆已保存）: {type(e).__name__}"
                    )

    # ---------- L3：用户画像更新 ----------
    # （save_user_location 已随"出发地"手动输入功能一并移除；画像城市仍由
    # _update_profile_async 从对话文本自然提取）

    async def _upsert_conversation_profile(
        self, conn, updates: dict, increment_message_count: bool = False,
    ) -> None:
        """写入本会话画像；只在保存整轮消息时递增消息计数。"""
        if not updates and not increment_message_count:
            return
        delta = 1 if increment_message_count else 0
        await conn.execute(
            "INSERT INTO conversation_profiles "
            "(user_id, conversation_id, profile, message_count) "
            "VALUES ($1, $2, $3::jsonb, $4) "
            "ON CONFLICT (user_id, conversation_id) DO UPDATE "
            "SET profile = conversation_profiles.profile || EXCLUDED.profile, "
            "message_count = conversation_profiles.message_count + $4, "
            "updated_at = NOW()",
            self.user_id, self.conv_id,
            json.dumps(updates, ensure_ascii=False), delta,
        )

    async def update_profile_fields(self, updates: dict) -> None:
        """供旅行槽位流程写明确的出发地/目的地，不依赖文本猜测。"""
        if not updates or self._is_eval_traffic():
            return
        pool = await get_pool()
        async with pool.acquire(timeout=DB_ACQUIRE_TIMEOUT) as conn:
            await self._upsert_conversation_profile(conn, updates)

    async def get_conversation_profile(self) -> dict:
        """读取结构化会话画像，供路由恢复目的地、出发地和其他槽位。"""
        pool = await get_pool()
        async with pool.acquire(timeout=DB_ACQUIRE_TIMEOUT) as conn:
            return await load_conversation_profile(conn, self.user_id, self.conv_id)

    def _is_eval_traffic(self) -> bool:
        """批1 B-P1-3：评测流量（persona=civil_code_eval）不进画像口径。

        184 条民法典批量评测是打真实 chat 端点跑的，每一轮都会经
        `finalize_answer → save_messages → _update_profile_async` 写进
        `conversation_profiles`/`user_profiles` 并把 `message_count` 加一格，
        真实用户的画像与消息数被合成样本污染。trace/verdict 侧早有排除口径
        （`rag_monitor.py` 按 persona 反选），画像侧此前漏了。
        判据必须是 `is_eval_persona`（只排评测）；用 `is_civil_persona` 会连
        线上民法典真实用户一起关掉。记忆本体（会话历史）不排除——评测要复现
        真实链路的多轮行为。
        """
        from .persona_manager import is_eval_persona

        return is_eval_persona(self.persona_id)

    async def _update_profile_async(self, conn, user_content: str, assistant_content: str):
        """从用户问题提取会话画像，并保留全局画像作为跨会话偏好。"""
        if self._is_eval_traffic():
            return
        new_profile = extract_profile_updates(user_content)
        await self._upsert_conversation_profile(
            conn, new_profile, increment_message_count=True,
        )
        if new_profile:
            await conn.execute(
                "INSERT INTO user_profiles (user_id, profile) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE "
                "SET profile = user_profiles.profile || $2::jsonb, updated_at = NOW()",
                self.user_id, json.dumps(new_profile)
            )

    @staticmethod
    def _profile_to_dict(profile_data) -> dict:
        """把 asyncpg 返回的 JSONB 文本或 dict 统一解析为字典。"""
        if isinstance(profile_data, dict):
            return profile_data
        if isinstance(profile_data, str):
            try:
                parsed = json.loads(profile_data)
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _format_profile(profile_data: dict) -> str:
        """把画像压成 LLM 易读短句，天气只在有真实快照时注入。"""
        if not profile_data:
            return ""
        data = normalize_conversation_profile(profile_data)
        parts = []
        labels = {
            # 批1 B-P1-4（2026-09-28）：不回填姓名。抽取端已停写，但存量画像里仍有
            # name 字段，这里不列它就等于"忽略"；库里的字段与列一律不动。
            "travelers": "人数", "origin": "出发地",
            "destination": "目的地", "budget": "预算", "days": "行程",
        }
        for field, label in labels.items():
            if data.get(field):
                parts.append(f"{label}: {data[field]}")
        forecast = data.get("weather_forecast") or []
        if forecast:
            city = data.get("weather_city") or data.get("destination") or ""
            first = forecast[0]
            weather_text = (
                f"{city} {first.get('date', '')} "
                f"{first.get('day_weather', '')} "
                f"{first.get('day_temp', '')}/{first.get('night_temp', '')}℃"
            ).strip()
            parts.append(f"天气: {weather_text}")
        return f"【用户画像】{' | '.join(parts)}" if parts else ""

    # ---------- 获取画像（按需读取）----------
    async def get_profile(self) -> str:
        """优先读取本会话画像，再回退跨会话全局画像。"""
        pool = await get_pool()
        async with pool.acquire(timeout=DB_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(
                "SELECT profile FROM conversation_profiles "
                "WHERE user_id = $1 AND conversation_id = $2",
                self.user_id, self.conv_id,
            )
            if row and row["profile"]:
                formatted = self._format_profile(
                    self._profile_to_dict(row["profile"])
                )
                if formatted:
                    return formatted

            row = await conn.fetchrow(
                "SELECT profile, updated_at FROM user_profiles WHERE user_id = $1",
                self.user_id
            )
            if row and row["profile"]:
                # 全局画像只做跨会话兜底；会话画像不参与 90 天过期。
                from datetime import datetime, timedelta, timezone
                updated = row["updated_at"]
                if updated and updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated and (datetime.now(timezone.utc) - updated) > timedelta(days=90):
                    await conn.execute(
                        "DELETE FROM user_profiles WHERE user_id = $1 "
                        "AND updated_at < NOW() - INTERVAL '90 days'",
                        self.user_id
                    )
                    return ""
                return self._format_profile(self._profile_to_dict(row["profile"]))
            return ""
