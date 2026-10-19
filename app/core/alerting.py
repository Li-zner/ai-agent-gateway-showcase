"""告警出网（#7）：预警从页面推到 IM/webhook，不再等人盯屏。

配置（全部环境变量，缺省关闭）：
- ALERT_WEBHOOK_URL   webhook 地址（空 = 关闭）
- ALERT_WEBHOOK_KIND  dingtalk（默认，企业微信同构）/ feishu / generic
- ALERT_ALLOW_PRIVATE 1 = 允许内网/环回 webhook（内网 IM 场景；默认按 SSRF
  约束拒绝私有与保留地址，仅放行公网 http/https）

设计：
- fail-safe：发送失败只记日志与计数，绝不影响监测/评测主流程；
- 去重节流：同 title 10 分钟窗口内只发一次（事件按 verdict 批量累积，
  不节流会随 occurrence 刷屏）；
- fire-and-forget：调用方 spawn 后不管结果。
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from urllib.parse import urlparse

import httpx

_DEDUP_WINDOW = 600.0  # 同 title 节流窗口（秒）
_last_sent: dict[str, float] = {}
_SPAWNED: set = set()  # spawn_alert 后台任务强引用（防 GC 中途回收）


async def _host_allowed(host: str) -> bool:
    """SSRF 约束：拒绝环回/私有/保留地址，除非显式放行内网。

    2026-10-06 审查加固（P3）：此前只校验 host 字符串——解析到内网 IP 的
    域名可绕过（SSRF）。现对域名解析并逐一校验全部地址；解析失败
    fail-closed。残余 TOCTOU（解析后重绑定）超出本通道威胁模型（URL 来自
    业主配置而非外部输入），注释在案。
    2026-10-07 审查修复（P1-5）：解析走 asyncio.to_thread——send_alert 跑在
    事件循环里，而告警常在 DNS 抖动时才触发，同步 getaddrinfo 的超时会冻结
    整个 loop（全站请求排队），比丢一条告警严重得多。
    """
    if os.getenv("ALERT_ALLOW_PRIVATE", "") == "1":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(".internal"):
            return False
        import socket
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except (socket.gaierror, UnicodeError):
            return False  # 解析失败：发送必然失败，fail-closed 给出更清晰的配置错误
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if ip.is_loopback or ip.is_private or ip.is_reserved \
                    or ip.is_link_local or ip.is_multicast:
                return False
        return True
    return not (ip.is_loopback or ip.is_private or ip.is_reserved
                or ip.is_link_local or ip.is_multicast)


async def _validate_url(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"协议 {parsed.scheme or '(空)'} 不允许，仅 http/https"
    host = parsed.hostname or ""
    if not host or not await _host_allowed(host):
        return False, (f"host {host} 为环回/私有/保留地址——内网 IM 请设 "
                       "ALERT_ALLOW_PRIVATE=1")
    return True, ""


def _payload(kind: str, title: str, text: str) -> dict:
    content = f"{title}\n{text}"
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": content}}
    if kind == "generic":
        return {"title": title, "text": text, "severity_hint": title}
    # dingtalk / wework 同构
    return {"msgtype": "text", "text": {"content": content}}


def _throttled(title: str) -> bool:
    now = time.monotonic()
    # 哨兵用 None 而非 0.0：time.monotonic() 在 Linux 上从开机起算，
    # 若把"从没发过"当成 0.0，低 uptime 主机首条会误判 now-0<窗口 被静默丢弃。
    last = _last_sent.get(title)
    if last is not None and now - last < _DEDUP_WINDOW:
        return True
    _last_sent[title] = now
    # 防字典无限增长：超过 200 键清一次过期项
    if len(_last_sent) > 200:
        for k in [k for k, v in _last_sent.items() if now - v >= _DEDUP_WINDOW]:
            _last_sent.pop(k, None)
    return False


async def send_alert(title: str, text: str) -> bool:
    """发一条告警到配置的 webhook；返回是否真正发出（关闭/节流/失败= False）。

    fail-safe：任何异常吞掉只打日志，调用方 spawn 后无须等待。
    """
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return False
    if _throttled(title):
        return False
    ok, reason = await _validate_url(url)
    if not ok:
        # 配置错误是持续性的：只记一次日志（借节流），不发重试风暴
        print(f"[alerting] webhook 配置不可用（{reason}），告警未发送: {title}",
              flush=True)
        return False
    kind = os.getenv("ALERT_WEBHOOK_KIND", "dingtalk").strip().lower()
    payload = _payload(kind, title, text)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        ok = resp.status_code < 400
        from .metrics import alerting_sent_total
        alerting_sent_total.labels("sent" if ok else "failed").inc()
        if not ok:
            print(f"[alerting] webhook 返回 {resp.status_code}: {title}", flush=True)
        return ok
    except Exception as exc:  # noqa: BLE001 — 告警失败绝不影响主流程
        from .metrics import alerting_sent_total
        alerting_sent_total.labels("failed").inc()
        print(f"[alerting] 发送失败（{type(exc).__name__}）: {title} {str(exc)[:120]}",
              flush=True)
        return False


def spawn_alert(title: str, text: str) -> None:
    """fire-and-forget 入口：调用方即返回，发送在后台任务完成。

    2026-10-06 审查修复（P2）：此前入口先查一次 _throttled（标记标题时刻），
    send_alert 内部再查一次 → 刚标记必被判节流 → 本入口的告警永远发不出
    （rag_monitor 质量告警实际被静默吞掉）。节流以 send_alert 内部单次
    判定为准，入口不再预查。
    """
    if not os.getenv("ALERT_WEBHOOK_URL", "").strip():
        return

    async def _send():
        await send_alert(title, text)

    task = asyncio.create_task(_send())
    # spawn 持强引用防 GC 中途回收（同 audit 的做法）
    _SPAWNED.add(task)
    task.add_done_callback(_SPAWNED.discard)
