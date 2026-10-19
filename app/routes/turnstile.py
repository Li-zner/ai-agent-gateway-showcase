"""Cloudflare Turnstile 人机校验（A-P1-4，2026-09-29 自 phone.py 拆出防 600 行门禁）。

env 门控：TURNSTILE_SITE_KEY/TURNSTILE_SECRET 留空 = 功能关闭，本地零感知。
"""
from pydantic import BaseModel, Field

from ..core.logging import setup_logging

logger = setup_logging()


class TurnstileTokenRequest(BaseModel):
    """发码请求里的人机令牌字段：服务端启用时必填，未启用时忽略"""
    turnstile_token: str = Field(default="", max_length=4096)


def turnstile_enabled() -> bool:
    """双 key 齐全才启用（只配一半按关闭处理，配置端点同口径防呆）。

    经模块属性读取（`cfg.X`）而非函数内 `from ..core.config import X`——
    后者是 import 时的值拷贝，运行期改配置（测试 monkeypatch/热更新）不生效。
    """
    from ..core import config as cfg
    return bool(cfg.TURNSTILE_SITE_KEY and cfg.TURNSTILE_SECRET)


async def verify_turnstile(token: str, remote_ip: str = "") -> bool:
    """Cloudflare Turnstile 服务端校验；失败/网络异常一律 False（fail-closed）。"""
    import httpx
    from ..core.config import TURNSTILE_SECRET, HTTP_TIMEOUT_SHORT
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SHORT) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": TURNSTILE_SECRET, "response": token,
                      **({"remoteip": remote_ip} if remote_ip else {})})
            return bool(resp.json().get("success"))
    except Exception as ts_err:
        logger.warning(f"Turnstile 校验请求失败（fail-closed 拒发）: {ts_err}")
        return False


async def turnstile_config_payload() -> dict:
    """公开配置载荷：前端据此决定是否渲染人机挂件（site_key 空 = 功能关闭）"""
    from ..core import config as cfg
    enabled = turnstile_enabled()
    return {"site_key": cfg.TURNSTILE_SITE_KEY if enabled else "", "enabled": enabled}
