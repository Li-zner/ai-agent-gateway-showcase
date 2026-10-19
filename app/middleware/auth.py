import httpx
import uuid
import json
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Request, HTTPException, Depends, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware

from ..core.config import (
    SECRET_KEY, SECRET_KEY_OLD, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, SESSION_SECRET_KEY
)
from ..core.logging import setup_logging
from ..core.password import hash_password, is_legacy_hash, pwd_context, verify_password
from ..core.redis import get_redis
from ..core.auth_cookies import ACCESS_COOKIE

logger = setup_logging()
security = HTTPBearer(auto_error=False)

# 假哈希缓存（见 _dummy_hash）：空串 = 尚未生成
_DUMMY_HASH: str = ""

# ---------- 数据库用户操作 ----------
import asyncpg
from ..core.config import POSTGRES_DSN

async def get_user(username: str):
    from ..core.db import get_pool
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "SELECT username, hashed_password, role, tenant_id, permissions, "
            "phone, quota_limited, used_requests, is_active FROM users WHERE username=$1",
            username
        )
        if row:
            return {
                "username": row["username"],
                "hashed_password": row["hashed_password"],
                "role": row["role"] or "user",
                "tenant_id": row["tenant_id"] or 1,
                "permissions": row["permissions"] or [],
                "phone": row["phone"],
                "quota_limited": bool(row["quota_limited"]) if row["quota_limited"] is not None else False,
                "used_requests": row["used_requests"] or 0,
                "is_active": bool(row["is_active"]) if row["is_active"] is not None else True,  # A8
            }
        return None

async def authenticate_user(username: str, password: str):
    user = await get_user(username)
    if not user:
        # 丁（2026-10-18）：账号不存在也要付出与"密码错误"等价的哈希开销，
        # 否则"不存在"分支少一次 bcrypt（数十毫秒）——错误文案虽然是通用的，
        # 响应时延不是，访客可据此把库里的用户名一个个筛出来。
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, user["hashed_password"]):
        return None
    # 禁用账号拒绝登录（A8：管理员封禁后禁止登录）
    if not user.get("is_active", True):
        return None
    # C7：存量（$2b$）哈希登录成功后惰性升级为新 SHA-256 方案（失败不阻塞登录）
    if is_legacy_hash(user["hashed_password"]):
        await _upgrade_legacy_hash(username, password)
    return user


def _dummy_hash() -> str:
    """时间对齐用的假哈希：随机口令经同一方案生成，永不可能被猜中

    惰性生成（首次用到才算）：bcrypt 一次约数十毫秒，放 import 期会让启动变慢，
    而这条路径只在"账号不存在"时走；生成后缓存复用，后续 miss 成本恒定。
    """
    global _DUMMY_HASH
    if not _DUMMY_HASH:
        _DUMMY_HASH = hash_password(secrets.token_hex(32))
    return _DUMMY_HASH


async def _upgrade_legacy_hash(username: str, password: str):
    """把存量 bcrypt 哈希升级为 bcrypt_sha256 方案（惰性迁移，无需批处理）

    - WHERE 追加 `hashed_password LIKE '$2b$%'`：仅当仍是存量前缀才更新，
      防止与并发改密竞态覆盖掉更新的哈希。
    - 升级是尽力而为：失败仅记日志，登录不受影响（下次成功登录再触发）。
    """
    try:
        from ..core.db import get_pool
        pool = await get_pool()
        new_hashed = hash_password(password)
        async with pool.acquire(timeout=5) as conn:
            await conn.execute(
                "UPDATE users SET hashed_password=$1 "
                "WHERE username=$2 AND hashed_password LIKE '$2b$%'",
                new_hashed, username,
            )
        logger.info("legacy_password_upgraded", extra={"extra_fields": {"username": username}})
    except Exception as exc:  # noqa: BLE001 — 升级失败不允许阻塞登录，故兜底记录
        logger.warning(
            "legacy_password_upgrade_failed",
            extra={"extra_fields": {"username": username, "error": type(exc).__name__}},
        )

def create_access_token(data: dict):
    """签发 access token。
    注（A13 决策）：jti 保留为无状态唯一标识（不持久化）；主动踢人需配套 Redis 黑名单，
    会破坏无状态 JWT，当前登出/轮换由 refresh jti 黑名单覆盖，故不实现 access 黑名单。"""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)  # P2 #12：废弃 datetime.utcnow
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": now, "type": "access", "jti": uuid.uuid4().hex})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict):
    """签发独立 refresh token（长时效；type=refresh 区分，jti 用于黑名单）"""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)  # P2 #12：废弃 datetime.utcnow
    expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.setdefault("family", uuid.uuid4().hex)
    to_encode.update({"exp": expire, "iat": now, "type": "refresh", "jti": uuid.uuid4().hex})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_token_pair(username: str) -> dict:
    """签发 access + refresh 一对 token（登录/注册/第三方回调统一入口）"""
    family = uuid.uuid4().hex
    return {
        "access_token": create_access_token({"sub": username}),
        "refresh_token": create_refresh_token({"sub": username, "family": family}),
    }


# 回滚黑名单占位：仅当值仍为本次写入的 "1" 才删（防误删登出写入的同键标记）
_RELEASE_BLACKLIST_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _decode_token(token: str, verify_exp: bool = True) -> dict:
    """用当前密钥 + 旧密钥依次尝试解码（支持轮换并行期）

    轮换后新 token 用新密钥验，旧 token 用 JWT_SECRET_OLD 仍可验，
    保证并行期内会话不断。全部失败才抛错。
    """
    keys = [SECRET_KEY]
    if SECRET_KEY_OLD:
        keys.append(SECRET_KEY_OLD)
    last_err = None
    for idx, key in enumerate(keys):
        try:
            payload = jwt.decode(
                token, key, algorithms=[ALGORITHM],
                options={"verify_exp": verify_exp},
            )
            if idx > 0:
                # 旧密钥成功解码：轮换并行期正常，但记录便于排查（P3 #25）
                logger.warning("使用旧密钥 JWT_SECRET_OLD 成功解码 token（密钥轮换并行期）")
            return payload
        except JWTError as e:
            last_err = e
    raise last_err or JWTError("token decode failed")

async def refresh_access_token(token: str) -> dict:
    """用独立 refresh token 换新 token 对（轮换：旧 refresh 进黑名单）

    返回 {"username", "access_token", "refresh_token"}；
    超过 REFRESH_MAX_DAYS 天未活跃或已撤销则拒绝。
    """
    from ..core.config import REFRESH_MAX_DAYS
    try:
        # verify_exp=True：强制校验 refresh token 自身有效期（30 天硬边界）
        payload = _decode_token(token, verify_exp=True)
        if payload.get("type") != "refresh":
            raise HTTPException(401, "Invalid refresh token")
        jti = payload.get("jti")
        username = payload.get("sub")
        if not username:
            raise HTTPException(401, "Invalid token")
        iat = payload.get("iat")
        if iat:
            age_days = (datetime.now(timezone.utc) - datetime.fromtimestamp(iat, tz=timezone.utc)).days
            if age_days > REFRESH_MAX_DAYS:
                raise HTTPException(401, "会话已过期，请重新登录")
        user = await get_user(username)
        if user is None:
            raise HTTPException(401, "User not found")
        if not user.get("is_active", True):
            raise HTTPException(401, "账号已被禁用")
        # 改密后旧会话失效（2026-09-07 审查 P2）：token 签发早于改密时刻的
        # refresh 一律拒绝（改密时写入 auth:pwd_changed:{username}，TTL 与
        # refresh 有效期一致，过期后无需再比）
        iat_ts = payload.get("iat")
        if iat_ts:
            r = await get_redis()
            changed = await r.get(f"auth:pwd_changed:{username}")
            if changed and int(iat_ts) < int(changed):
                raise HTTPException(401, "密码已修改，请重新登录")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid token")
    r = await get_redis()
    family = payload.get("family") or jti
    family_key = f"auth:refresh_family_blacklist:{family}" if family else None
    if family_key and await r.get(family_key):
        raise HTTPException(401, "Token family revoked")
    blacklist_key = f"auth:refresh_blacklist:{jti}" if jti else None
    claim_val = uuid.uuid4().hex
    if blacklist_key:
        # 原子抢占（SET NX）：同一 refresh token 只能成功兑换一次。
        # 并发重放（旧 token 被盗后与真实客户端同时兑换）第二个请求直接 401。
        claimed = await r.set(blacklist_key, claim_val, nx=True, ex=REFRESH_TOKEN_EXPIRE_DAYS * 86400)
        if not claimed:
            if family_key:
                await r.set(
                    family_key, "1",
                    ex=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
                )
            raise HTTPException(401, "Token replay detected")
    try:
        # 先校验与抢占全部成功后才签发，签发异常时回滚抢占（不让用户被锁死）
        new_pair = {
            "username": username,
            "access_token": create_access_token({"sub": username}),
            "refresh_token": create_refresh_token({"sub": username, "family": family}),
        }
        return new_pair
    except Exception:
        if blacklist_key:
            # 仅回滚自己写入的占位（值不匹配说明已被登出覆盖，保留撤销语义）
            await r.eval(_RELEASE_BLACKLIST_LUA, 1, blacklist_key, claim_val)
        raise


async def revoke_refresh_token(token: str) -> bool:
    """登出：把 refresh token 的 jti 与整个 family 加入黑名单。

    返回 False 表示"token 无效"（幂等 OK）或"Redis 异常"（吊销未落地）；
    调用方可据此向客户端提示登出未完全生效——原先吞掉一切异常只留 debug 日志，
    Redis 故障时用户以为已登出、token 实际仍可用（假登出，2026-09-10 审查 P2）。
    """
    try:
        payload = _decode_token(token, verify_exp=False)
    except Exception:
        return False  # token 无效：无 jti 可吊销，幂等成功语义
    jti = payload.get("jti")
    if not jti:
        return False
    try:
        r = await get_redis()
        family = payload.get("family") or jti
        await r.set(
            f"auth:refresh_blacklist:{jti}", "1",
            ex=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        )
        await r.set(
            f"auth:refresh_family_blacklist:{family}", "1",
            ex=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        )
        return True
    except Exception as e:
        logger.warning(f"登出撤销 refresh token 失败（吊销未落地）: {e}")
        return False

async def get_cached_user(username: str):
    """带 Redis 缓存读取用户信息（TTL 5 分钟；不含 hashed_password，P1 #2 防每次查库）

    仅用于鉴权（角色/权限）；登录/改密等需要密码哈希的场景仍走 get_user（DB）。
    缓存键前缀单源定义在 core/quota（USER_INFO_CACHE_PREFIX），quota 字段变更后
    由变更方调用 invalidate_user_cache 失效，防止额度判断读到旧值（P1 修复）。
    """
    from ..core.quota import USER_INFO_CACHE_PREFIX
    r = await get_redis()
    cache_key = f"{USER_INFO_CACHE_PREFIX}{username}"
    try:
        raw = await r.get(cache_key)
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.debug(f"用户缓存读取失败（回源 DB）: {e}")
    user = await get_user(username)
    if user:
        user.pop("hashed_password", None)  # 缓存不含密码哈希，降低泄露面
        try:
            await r.setex(cache_key, 300, json.dumps(user, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"用户缓存写入失败: {e}")
    return user


async def get_current_user(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    token = credentials.credentials if credentials else request.cookies.get(
        ACCESS_COOKIE, "")
    if not token:
        raise HTTPException(401, "Missing token")
    try:
        payload = _decode_token(token)
        # 独立 refresh token 不能当 access 用（缺 type 视为 access，兼容旧 token）
        if payload.get("type") not in (None, "access"):
            raise JWTError("not an access token")
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(401, "Invalid token")
    except JWTError:
        raise HTTPException(401, "Invalid token")
    # 走 Redis 缓存，避免每次请求查询数据库（P1 #2）
    user = await get_cached_user(username)
    if user is None:
        raise HTTPException(401, "User not found")
    # 禁用账号拒绝访问（A8：is_active=False；缓存 5 分钟内生效）
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="账号已被禁用")
    iat = payload.get("iat")
    if iat:
        try:
            r = await get_redis()
            changed = await r.get(f"auth:pwd_changed:{username}")
            if changed and int(iat) < int(changed):
                raise HTTPException(status_code=401, detail="密码已修改，请重新登录")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"改密状态检查失败，拒绝旧 access token: {e}")
            raise HTTPException(status_code=503, detail="认证状态暂不可用")
    return user


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """路由级管理员门禁：供 `APIRouter(dependencies=[Depends(require_admin)])` 使用。

    背景（2026-09-19 审查 routes/rag F-P2-2）：控制面的 role 判断此前逐个端点手写
    （rag_admin.py 17 处 + 状态迁移 helper 1 处），实测覆盖是满的，但**新增端点漏写
    不会有任何报错**——管理员分区的鉴权不该依赖人的记性。挂到 router 上之后默认拒绝，
    端点内既有的显式判断继续生效（同一请求内 get_current_user 结果被 FastAPI 缓存，
    不会多跑一次认证）。
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return current_user


oauth = OAuth()
oauth.register(
    name="github",
    client_id=GITHUB_CLIENT_ID,
    client_secret=GITHUB_CLIENT_SECRET,
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "user:email"},
    client_auth_method="client_secret_basic",
)
