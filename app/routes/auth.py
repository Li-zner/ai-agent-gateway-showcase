"""认证路由：登录 / 刷新 / 登出 / 修改密码

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
支持手机号作为账号登录（账号即手机号）；JWT 认证主体在 middleware/auth。
"""
from pydantic import BaseModel, Field
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse

from ..core.logging import setup_logging
from ..core.db import get_pool
from ..core.config import (
    PASSWORD_MAX_BYTES, TRUST_PROXY_HEADERS, is_plausible_client_ip,
    peer_is_trusted_proxy,
)
from ..core.auth_cookies import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    clear_console_cookies,
    is_console_cookie_request,
    set_console_cookies,
)
from ..core.password import hash_password
from ..middleware.auth import (
    get_current_user, authenticate_user, create_token_pair,
    refresh_access_token, revoke_refresh_token,
)

logger = setup_logging()

router = APIRouter()

# ---------- 登录防暴力破解（P2 修复：密码登录原先可无限尝试）----------
_LOGIN_FAIL_LIMIT = 5
_LOGIN_LOCK_SECONDS = 900

# 登录失败计数/锁定原子化（B-17）：INCR 首次即设 TTL；达上限写锁键并清计数
_LOGIN_FAIL_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
if tonumber(c) >= tonumber(ARGV[2]) then
  redis.call('SETEX', KEYS[2], ARGV[1], '1')
  redis.call('DEL', KEYS[1])
end
return c
"""


def _client_ip(request: Request) -> str:
    """取可信客户端 IP。Cloudflare 代理链路以 CF-Connecting-IP 为准。

    生产链路是 Cloudflare -> nginx；CF 头由边缘写入。没有该头时回退
    X-Forwarded-For（取法见函数内注释），不采信用户可控的链头值。

    RT-1（2026-09-19 审查）：转发头本身可被直连端口者伪造，伪造一次即绕
    登录失败锁 / 短信 IP 日限额 / oauth 限频。故默认只信 socket 对端地址，
    只有部署链路确为可信反代（TRUST_PROXY_HEADERS=1，与 observability 威胁
    模型口径一致）时才读头。本函数为 phone/map/oauth 共用，修这一处全覆盖。

    2026-09-22 审查 P1：上一轮"对端是回环/私网就算可信反代"太宽——网关跑在
    docker 里，直连源的地址本身就是 172.18.0.x，整个 RFC1918 当信任面等于没
    门禁：任意同网段容器自报 CF-Connecting-IP 就能把爆破锁/日限额甩给别人的
    IP 桶。现在对端必须精确命中 TRUSTED_PROXY_CIDRS（默认仅 127.0.0.1/32，
    生产按反代实际网段配置），判据与 HSTS 共用 core.config 同一处。
    2026-09-28 审查（ECS 直挂公网）：命中反代判据后原先**无条件**采信头值，而
    CF-Connecting-IP 可以是任意字符串（内网、组播、::ffff:、空）。现在过一道
    is_plausible_client_ip，脏值不再污染 IP 桶；被拒时退回 socket 对端而不是
    "unknown"——全站共用一个 unknown 桶等于给攻击者一个可投毒的公共桶，而对端
    地址由内核决定、访客伪造不了。
    2026-09-28 批1 审读 A-P1-1：同轮留下的 XFF 回退取的是**末跳**，而 nginx 用
    $proxy_add_x_forwarded_for 把自己的 socket 对端追加在链尾——现网那个对端正是
    CF 出口地址，于是命中回退的访客全被记成同一个桶（限流额度变全站共享）。改为
    从右往左跳过 TRUSTED_PROXY_CIDRS 里的地址，第一个非可信反代的跳即访客。
    """
    peer = request.client.host if request.client else ""
    if not (TRUST_PROXY_HEADERS or peer_is_trusted_proxy(peer)):
        return peer or "unknown"
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if is_plausible_client_ip(cf_ip):
        return cf_ip
    # XFF 要**从右往左**取第一个不在可信反代网段里的地址。末跳是错的：nginx 用
    # $proxy_add_x_forwarded_for 把自己的 socket 对端追加在链尾，而现网那个对端就是
    # CF 出口地址（2026-09-28 批1 审读 A-P1-1）——取末跳等于把命中这条回退的访客
    # 全塞进同一个桶，登录锁 5 次/15 分与短信 20 条/日都变成全站共享额度。
    # 也不取首跳：链头由最外层写入之前，任何中间环节都可伪造。
    xff = request.headers.get("x-forwarded-for") or ""
    for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
        if peer_is_trusted_proxy(hop):
            continue
        return hop if is_plausible_client_ip(hop) else (peer or "unknown")
    return peer or "unknown"


async def _check_login_lock(ip: str, username: str):
    """登录前检查失败锁：达上限的 IP+用户名组合 15 分钟内拒绝"""
    from ..core.redis import get_redis
    r = await get_redis()
    if await r.get(f"login_lock:{ip}:{username}"):
        raise HTTPException(429, "尝试次数过多，请 15 分钟后再试")


async def _record_login_failure(ip: str, username: str):
    """登录失败累计：达上限写入锁定键（TTL 即锁时长，自愈）。

    INCR/EXPIRE/锁写入收敛进单个 Lua 原子执行（2026-09-15 审查 B-17）：
    原三步分开，进程在 INCR 与 EXPIRE 之间崩溃会残留无 TTL 键（永不过期）。
    """
    from ..core.redis import get_redis
    r = await get_redis()
    await r.eval(
        _LOGIN_FAIL_LUA, 2,
        f"login_fail:{ip}:{username}", f"login_lock:{ip}:{username}",
        _LOGIN_LOCK_SECONDS, _LOGIN_FAIL_LIMIT,
    )


async def _clear_login_failures(ip: str, username: str):
    """登录成功清零失败计数"""
    from ..core.redis import get_redis
    r = await get_redis()
    await r.delete(f"login_fail:{ip}:{username}")


class LoginRequest(BaseModel):
    """登录请求（P2 #17：Pydantic 校验 + 自动生成 OpenAPI 文档）"""
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    """修改密码请求（限长与登录请求对称：bcrypt 截断在 72 字节之外无增益，
    超长串只膨胀日志与内存）"""
    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=6, max_length=256)


async def resolve_login_username(username: str) -> str:
    """登录名归一：裸手机号按 users.phone 查到 username（新版=手机号本身，旧版
    phone_ 前缀行兼容解析）；非手机号原样返回（GitHub 登录名直登）"""
    import re
    if username.startswith("phone_") or not re.match(r'^1\d{10}$', username):
        return username
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow("SELECT username FROM users WHERE phone=$1", username)
        if row:
            return row["username"]
    return username


@router.post("/api/login")
async def login(payload: LoginRequest, request: Request):
    """登录（Pydantic 校验自动文档；失败计数防暴力破解，成功清零）"""
    username = payload.username
    password = payload.password
    client_ip = _client_ip(request)
    # 先解析再落锁（2026-09-07 审查 P1）：原实现检查用原始输入（手机号）、
    # 失败记账用解析后 username，两者键不一致 → 手机号登录可无限试密码
    username = await resolve_login_username(username)
    await _check_login_lock(client_ip, username)
    # 支持手机号作为账号登录（账号即手机号）
    user = await authenticate_user(username, password)
    if not user:
        await _record_login_failure(client_ip, username)
        raise HTTPException(401, "Invalid username or password")
    await _clear_login_failures(client_ip, username)
    pair = create_token_pair(username)
    # 审计：登录成功
    from ..core.audit import audit
    await audit(username, "login", {"method": "password"})
    cookie_mode = is_console_cookie_request(request)
    response = JSONResponse({
        "access_token": "" if cookie_mode else pair["access_token"],
        "refresh_token": "" if cookie_mode else pair["refresh_token"],
        "token_type": "bearer",
        "cookie_auth": cookie_mode,
    })
    return set_console_cookies(response, pair, request)


@router.post("/api/refresh")
async def refresh_token(request: Request):
    """独立 refresh token 换新 token 对（轮换：旧 refresh 进黑名单）"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
    else:
        token = request.cookies.get(REFRESH_COOKIE, "")
    if not token:
        raise HTTPException(401, "Missing token")
    result = await refresh_access_token(token)
    cookie_mode = is_console_cookie_request(request)
    response = JSONResponse({
        "access_token": "" if cookie_mode else result["access_token"],
        "refresh_token": "" if cookie_mode else result["refresh_token"],
        "token_type": "bearer",
        "cookie_auth": cookie_mode,
    })
    return set_console_cookies(response, result, request)


@router.post("/api/logout")
async def logout(request: Request):
    """登出：撤销 refresh token（其 jti 进黑名单）
    显式校验格式（P0 #11：格式错误返回 401，杜绝"无效成功"）"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
    else:
        token = request.cookies.get(REFRESH_COOKIE, "")
    if not token:
        raise HTTPException(401, "Invalid token format")
    revoked = await revoke_refresh_token(token)
    # revoked=False 含"token 本就无效"（幂等成功）与"Redis 异常未落地"（服务端已
    # warning），字段透出供前端在异常场景提示（2026-09-10 审查 P2 假登出修复）
    response = JSONResponse({"success": True, "revoked": revoked})
    return clear_console_cookies(response)


@router.post("/api/user/change-password")
async def change_password(payload: ChangePasswordRequest, current_user: dict = Depends(get_current_user)):
    """修改当前账号密码：需校验旧密码，bcrypt 更新（A24）"""
    old_password = payload.old_password
    new_password = payload.new_password
    if len(new_password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise HTTPException(400, f"密码长度不能超过 {PASSWORD_MAX_BYTES} 字节")  # C7：策略上限（SHA-256 预处理）
    if new_password == old_password:
        raise HTTPException(400, "新密码不能与旧密码相同")
    username = current_user["username"]
    user = await authenticate_user(username, old_password)
    if not user:
        raise HTTPException(400, "旧密码不正确")
    # 改密吊销存量会话（2026-09-07 审查 P2）：记录改密时刻，refresh 流程发现
    # token 签发早于该时刻即拒绝——旧 refresh token 最长 30 天有效的窗口被关闭
    import time
    from ..core.redis import get_redis
    from ..core.config import REFRESH_TOKEN_EXPIRE_DAYS
    r = await get_redis()
    try:
        await r.set(
            f"auth:pwd_changed:{username}", str(int(time.time())),
            ex=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        )
    except Exception as e:
        logger.error(f"改密吊销标记写入失败，拒绝改密: {e}")
        raise HTTPException(503, "认证状态暂不可用，请稍后重试")
    new_hashed = hash_password(new_password)
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        await conn.execute(
            "UPDATE users SET hashed_password=$1 WHERE username=$2",
            new_hashed, username
        )
    logger.info(f"密码已修改: username={username}")
    return {"message": "密码修改成功"}
