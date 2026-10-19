import hashlib
import random
import time
import uuid
from collections import OrderedDict
from typing import Optional
from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.logging import setup_logging
from ..core.metrics import semantic_cache_hits_total, semantic_cache_misses_total
from ..core.config import CACHE_SIMILARITY_THRESHOLD, CACHE_L0_TTL
from .place_extract import travel_constraint_fingerprint

logger = setup_logging()


class _LRUCache:
    """进程内 LRU 热缓存（L0），带 TTL + 随机过期（防雪崩）"""

    def __init__(self, capacity: int = 256, default_ttl: int = 600):
        self._cache = OrderedDict()   # key -> (value, expire_ts)
        self._capacity = capacity
        self._default_ttl = default_ttl

    def get(self, key: str):
        item = self._cache.get(key)
        if item is None:
            return None
        value, expire = item
        if time.time() > expire:
            self._cache.pop(key, None)   # 过期即淘汰
            return None
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value: str, ttl: int = None):
        ttl = ttl if ttl is not None else self._default_ttl
        self._cache[key] = (value, time.time() + ttl)
        self._cache.move_to_end(key)
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    def invalidate(self, key: str) -> None:
        """删除单个热缓存键；动作执行器用于精确失效。"""
        self._cache.pop(key, None)

    def __len__(self):
        return len(self._cache)


# 全局热缓存实例（默认 TTL 从 config 读取，P1 去硬编码）
_hot_cache = _LRUCache(capacity=256, default_ttl=CACHE_L0_TTL)


def _hot_ttl() -> int:
    """随机过期时间：以 CACHE_L0_TTL 为中心 ±50% 抖动（默认 600 → 300-900），
    错开失效时刻防缓存雪崩。2026-09-30 校准修复（B2）：原先硬编码
    randint(300, 900)，CACHE_L0_TTL 成死旋钮——改 env 无感。"""
    return random.randint(int(CACHE_L0_TTL * 0.5), int(CACHE_L0_TTL * 1.5))


def _cache_l0_key(query: str, cache_ctx: str = "") -> str:
    """L0 热缓存 / 重建锁的定位键：query + 上下文维度（防跨上下文误命中）"""
    return f"{cache_ctx}\x1f{query}"


class SemanticCache:
    # 文本相似度阈值（从 config 读取，默认 0.85）
    SIMILARITY_THRESHOLD = CACHE_SIMILARITY_THRESHOLD

    @staticmethod
    def build_cache_ctx(username: str = "", persona_id: str = "",
                        user_profile: str = "", conversation_id: str = "",
                        user_query: str = "", lang: str = "",
                        model: str = "", user_perms=None) -> str:
        """构建语义缓存上下文维度（用户|人格|画像指纹|会话指纹|旅游约束|语言|模型|权限）

        背景（Bug #1）：缓存按 query 建键，而回答会注入人格/画像等个性化上下文，
        导致 A 用户的个性化回答被缓存命中给 B 用户。
        修复：回答进入缓存必须带上下文维度；画像/会话均用 SHA-256 前 16 位做指纹
        （画像原 8 位/32bit 在 ~10^5 用户量级生日碰撞概率≈1，碰撞对之间画像隔离
        失效，2026-09-10 审查对齐），既隔离不同画像/窗口，又不把明文写进缓存表。
        2026-09-12 修复（外部复核 P0）：会话指纹来自调用方可控的 conversation_id
        （客户端可传任意值），画像相同的两个账号复用会话 ID 即可互串缓存——
        增加不可变 username 作为首位分量，跨账号缓存彻底隔离。

        2026-09-17：旅游答案会被日期、天数、同行人、预算、主题直接改变，
        仅靠文本相似度会在 0.85 阈值下误命中。这里追加结构化约束指纹，
        约束不同的查询自然落到不同缓存分区，无需数据库迁移。

        2026-09-19 审查 core P2-5：再补语言与模型两个维度——同一条 query 文本在
        zh/en 界面指令下答案语言不同，缺 lang 会互相命中；换主模型后旧模型的答案
        也会继续命中。两者非 PII，明文并入键尾便于排障。

        2026-09-28 L5：补权限维度——检索按 user_perms 过滤（public/vip），键里
        没有它的话用户升级 vip 后 60 天内仍命中升级前按公开权限生成的回答。
        perms 排序后取指纹（顺序无关）；缺省 None 视同空权限。键尾全量变化使
        既有缓存行自然失联（60 天维护清理兜底），代价一次性。
        """
        user_fp = hashlib.sha256((username or "").encode("utf-8")).hexdigest()[:16]
        profile_fp = hashlib.sha256((user_profile or "").encode("utf-8")).hexdigest()[:16]
        conv_fp = hashlib.sha256((conversation_id or "").encode("utf-8")).hexdigest()[:16]
        perms_fp = hashlib.sha256(
            ",".join(sorted(user_perms or [])).encode("utf-8")).hexdigest()[:16]
        base = f"{user_fp}|{persona_id or ''}|{profile_fp}|{conv_fp}"
        force_travel = "travel" in (persona_id or "").lower() or "旅游" in (persona_id or "")
        constraint_fp = travel_constraint_fingerprint(user_query, force=force_travel)
        if constraint_fp:
            base = f"{base}|{constraint_fp}"
        return f"{base}|{lang or ''}|{model or ''}|{perms_fp}"

    @staticmethod
    async def get(query: str, cache_ctx: str = ""):
        """
        查询缓存（三级：L0 热缓存 → L1 精确匹配 → L2 语义匹配）
        命中时增加 hit_count，返回 response；未命中记录 miss。

        cache_ctx：个性化上下文维度（build_cache_ctx 生成）。
        不同上下文的回答不可互用，否则 A 用户含画像/位置的回答会泄露给 B 用户（Bug #1）。
        """
        l0_key = _cache_l0_key(query, cache_ctx)
        # L0: 进程内 LRU 热缓存（微秒级）
        l0_result = _hot_cache.get(l0_key)
        if l0_result is not None:
            semantic_cache_hits_total.inc()
            return l0_result

        pool = await get_pool()
        query_hash = hashlib.sha256(query.encode()).hexdigest()

        async with pool.acquire(timeout=5) as conn:
            # L1: 精确匹配（毫秒级）
            row = await conn.fetchrow(
                "SELECT response FROM semantic_cache "
                "WHERE query_hash = $1 AND cache_ctx = $2",
                query_hash, cache_ctx,
            )
            if row:
                # 命中计数无条件累加（P2 修复：原先 1/10 Python 抽样使热门条目计数停在
                # 0~1，与 clean_stale_cache 的 hit_count<2 判冷口径漂移，60 天后可能被误清；
                # 单行索引 UPDATE 的代价相对其省下的 LLM 调用可忽略）
                await conn.execute(
                    "UPDATE semantic_cache SET hit_count = hit_count + 1 "
                    "WHERE query_hash = $1 AND cache_ctx = $2",
                    query_hash, cache_ctx,
                )
                logger.info(f"精确缓存命中: {query[:30]}...")
                semantic_cache_hits_total.inc()
                # 回填 L0（随机过期防雪崩）
                _hot_cache.set(l0_key, row["response"], ttl=_hot_ttl())
                return row["response"]

            # L2: 相似度匹配（pg_trgm）
            # 用 % 运算符预筛候选（走 GIN 索引），再 similarity 确认，避免全表扫描（P0 #48）
            # 注意：需确保 semantic_cache.query_text 有 pg_trgm GIN 索引（见迁移）
            row = await conn.fetchrow(
                """
                SELECT id, response, similarity(query_text, $1) AS sim
                FROM semantic_cache
                WHERE cache_ctx = $2
                  AND query_text % $1 AND similarity(query_text, $1) > $3
                ORDER BY sim DESC
                LIMIT 1
                """,
                query,
                cache_ctx,
                SemanticCache.SIMILARITY_THRESHOLD,
            )
            if row:
                # 只累加命中计数，不改写 query_hash：改写会让行"易主"给最新相似查询，
                # 原查询的精确匹配 hash 丢失（下次又降级走 L2），且高并发下互相抢夺
                await conn.execute(
                    "UPDATE semantic_cache SET hit_count = hit_count + 1 WHERE id = $1",
                    row["id"],
                )
                logger.info(f"语义缓存命中 (相似度 {row['sim']:.2f}): {query[:30]}...")
                semantic_cache_hits_total.inc()
                # 回填 L0 热缓存（L1 为 PG 源数据不回写，147-148 设计如此）
                _hot_cache.set(l0_key, row["response"], ttl=_hot_ttl())
                return row["response"]

            # 未命中
            logger.info(f"缓存未命中: {query[:30]}...")
            semantic_cache_misses_total.inc()
            return None

    @staticmethod
    async def set(query: str, response: str, cache_ctx: str = "", ttl: Optional[int] = None):
        """
        写入缓存（同时写入 L0 热缓存 + PG）

        cache_ctx：个性化上下文维度（build_cache_ctx 生成），与 get 保持一致（Bug #1）。

        穿透防护：
        - 空/兜底内容不写入（避免把坏数据/兜底回复缓存，导致后续用户永远拿到兜底）
        - 支持自定义 ttl（穿透占位用短 TTL）
        """
        if response is None or not str(response).strip():
            logger.info(f"缓存写入跳过（空响应）: {query[:30]}...")
            return
        # 防止把兜底/错误回复写入缓存。兜底文案已按语言分中英两版（A 方案本地化），
        # 单值 == 比较会漏掉英文版 → 英文兜底被钉进缓存 60 天，故改为"命中任一变体即跳过"。
        from ..core.config import FALLBACK_MESSAGE_VARIANTS
        if str(response).strip() in FALLBACK_MESSAGE_VARIANTS:
            logger.info(f"缓存写入跳过（兜底回复）: {query[:30]}...")
            return
        # DFA 拦截的安全文案不写入（2026-09-09 审查 P1）：一次误伤即把该 query 钉成
        # 固定安全文案 60 天（命中计数累加还会躲过 clean_stale_cache），五处调用点在此单点收口。
        # 同理按变体集合判定：中文 get_filter().safe_message 与英文 SAFETY_FILTER_MESSAGE_EN 都要拦。
        from ..core.config import SAFETY_MESSAGE_VARIANTS
        if str(response).strip() in SAFETY_MESSAGE_VARIANTS:
            logger.info(f"缓存写入跳过（安全拦截文案）: {query[:30]}...")
            return

        # 先写入 L0（随机过期防雪崩）
        _hot_cache.set(_cache_l0_key(query, cache_ctx), response, ttl=ttl or _hot_ttl())

        pool = await get_pool()
        query_hash = hashlib.sha256(query.encode()).hexdigest()

        async with pool.acquire(timeout=5) as conn:
            await conn.execute(
                """
                INSERT INTO semantic_cache (query_hash, query_text, response, cache_ctx)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (query_hash, cache_ctx) DO UPDATE
                SET response = $3, query_text = $2
                """,
                query_hash,
                query,
                response,
                cache_ctx,
            )
            logger.info(f"缓存写入: {query[:30]}...")

    @staticmethod
    async def invalidate_exact(query: str, cache_ctx: str = "") -> int:
        """精确失效单条缓存：同时清 L0 热缓存和 PG 精确行。"""
        _hot_cache.invalidate(_cache_l0_key(query, cache_ctx))
        pool = await get_pool()
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        async with pool.acquire(timeout=5) as conn:
            row = await conn.fetchrow(
                "DELETE FROM semantic_cache "
                "WHERE query_hash=$1 AND cache_ctx=$2 RETURNING 1",
                query_hash, cache_ctx)
        return 1 if row else 0

    @staticmethod
    async def set_empty(query: str, cache_ctx: str = "", ttl: int = 30):
        """穿透占位：为未命中的 query 写入短暂空值标记（防穿透风暴）

        同一 query 在短时间内重复出现时，通过重建锁 + 该标记避免反复打底层。
        ttl 短（默认 30s），不污染长期缓存。
        仅写 L0 热缓存（带 TTL）而非 PG：PG 行的 __EMPTY__ 无过期机制，
        get 也不按时间过滤，会把占位钉死成“该 query 永久繁忙”（P1 修复）。
        跨实例互斥已由 Redis 重建锁承担，L0 短 TTL 足够缓解穿透。
        """
        _hot_cache.set(_cache_l0_key(query, cache_ctx), "__EMPTY__", ttl=ttl)
        logger.info(f"穿透占位写入: {query[:30]}... (ttl={ttl}s)")

    @staticmethod
    async def is_empty(response) -> bool:
        """判断缓存值是否为穿透占位"""
        return response is not None and str(response) == "__EMPTY__"

    # ===== 防击穿：互斥重建锁（2026-09-30 由 Redis SETNX 改进程内实现） =====
    # 缓存未命中后，同一 query 并发时只允许一个请求调用 LLM 重建，
    # 其余请求等待后重读（可能命中刚写入的缓存）。锁带 TTL 自愈，
    # 避免重建方异常退出导致死锁。
    #
    # ponytail: 进程内 dict 只在单 worker 内互斥——生产是单容器单 uvicorn
    # worker（deploy/ecs/docker-compose.yml 无 --workers 参数），语义成立，
    # 且 Redis 故障时锁仍可用。此前锁走 Redis 且 fail-closed：Redis 挂 =
    # 全站问答拒答"服务繁忙"，与缓存查询 fail-open（chat_stream_core:198）
    # 和限流四闸 fail-open 的哲学相悖。扩多副本/多 worker 时把这三个方法
    # 换回 Redis SETNX 实现，调用方签名不变。
    # 过期条目在下次同 key acquire 时被覆写；再无人访问的 key 留下小 tuple，
    # 上限 = 历史 distinct query 数，可接受。
    _REBUILD_LOCKS: dict = {}  # key -> (token, expires_at[time.monotonic()])

    @staticmethod
    def _lock_key(query: str, cache_ctx: str = "") -> str:
        """重建锁键：query + 上下文维度（防跨上下文串锁；与缓存键同构）"""
        return f"cache:rebuild:{hashlib.sha256(_cache_l0_key(query, cache_ctx).encode()).hexdigest()}"

    @staticmethod
    async def acquire_rebuild_lock(query: str, cache_ctx: str = "", ttl: int = 45) -> Optional[str]:
        """尝试获取重建锁；成功返回 token，已有重建进行中返回 None

        读-判-写全程无 await，单线程事件循环内天然原子，无需再加锁。
        """
        key = SemanticCache._lock_key(query, cache_ctx)
        now = time.monotonic()
        entry = SemanticCache._REBUILD_LOCKS.get(key)
        if entry is not None and entry[1] > now:
            return None  # 他人持有且未过期
        token = uuid.uuid4().hex
        SemanticCache._REBUILD_LOCKS[key] = (token, now + ttl)
        return token

    @staticmethod
    async def release_rebuild_lock(query: str, token: str, cache_ctx: str = ""):
        """释放重建锁（仅持有者能释放，防误删）"""
        if not token:
            return
        key = SemanticCache._lock_key(query, cache_ctx)
        entry = SemanticCache._REBUILD_LOCKS.get(key)
        if entry is not None and entry[0] == token:
            del SemanticCache._REBUILD_LOCKS[key]

    @staticmethod
    async def renew_rebuild_lock(query: str, token: str, cache_ctx: str = "", ttl: int = 45) -> bool:
        """续期重建锁（仅持有者可续；长任务重建中周期调用，防锁超时导致击穿，P0 #50）"""
        if not token:
            return False
        key = SemanticCache._lock_key(query, cache_ctx)
        entry = SemanticCache._REBUILD_LOCKS.get(key)
        if entry is None or entry[0] != token or entry[1] <= time.monotonic():
            return False
        SemanticCache._REBUILD_LOCKS[key] = (token, time.monotonic() + ttl)
        return True


# 模块级别名：兼容以 `from .semantic_cache import build_cache_ctx` 形式的外部导入
build_cache_ctx = SemanticCache.build_cache_ctx


async def safe_set(query: str, answer: str, cache_ctx: str = "") -> None:
    """写语义缓存的兜底封装：失败记日志不抛出。

    供 asyncio.create_task 后台写入使用——裸协程进后台任务后异常无人认领
    （RuntimeWarning 且静默），三处调用方（runner/router/chat_stream_ctx）统一走这里。
    """
    try:
        await SemanticCache.set(query, answer, cache_ctx=cache_ctx)
    except Exception as e:
        logger.warning(f"语义缓存写入失败: {e}")
