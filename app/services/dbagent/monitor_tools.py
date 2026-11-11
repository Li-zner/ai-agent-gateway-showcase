"""数据监控工具的处理器（批次 4）：列表 / 执行一次 / 创建停用草稿。

与 agent_tools 同约定：返回回喂模型的观察文本；不直接抛库错。
agent 只能建草稿，启用必须由人在页面完成（持续查询生产库的授权边界）。
"""
from __future__ import annotations

from datetime import datetime

from ...core.audit import ACT_DBAGENT_MONITOR_CREATE, audit
from . import monitors as monitor_svc


def _format_number(value: object) -> str:
    """Decimal/数值 -> 稳定十进制文本；缺失显示破折号。"""
    number = monitor_svc.to_number(value)
    return "—" if number is None else format(number.normalize(), "f")


def _format_time(value: object) -> str:
    """采集时间给到秒；从未采集显示破折号。"""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return "—"


def _find_monitor(rows: list[dict], key: str) -> dict | None:
    """按数字 id 或完整名称定位监控项。"""
    if key.isdigit():
        return next((row for row in rows if row["id"] == int(key)), None)
    return next((row for row in rows if row["name"] == key), None)


async def _tool_list_monitors(args: dict, ctx) -> str:
    """列出监控项：值来自最近一次采集，明确标注不是实时值。"""
    try:
        rows = await monitor_svc.list_monitors()
    except Exception as exc:  # noqa: BLE001 — 工具失败只回喂模型
        return f"监控列表读取失败（{type(exc).__name__}），请稍后重试。"
    if not rows:
        return ("当前没有监控项。可用 create_monitor 建停用草稿，"
                "再由人在数据监控页面手动启用。")
    lines = ["监控项（最近一次采集值，不是实时查询）："]
    for row in rows:
        unit = f" {row['unit']}" if row["unit"] else ""
        state = "已启用" if row["enabled"] else "停用草稿"
        lines.append(
            f"- #{row['id']} {row['name']}："
            f"{_format_number(row['last_value'])}{unit}"
            f"｜状态 {row['last_status']}｜采集 {_format_time(row['last_run_at'])}"
            f"｜阈值 {row['op']} {_format_number(row['threshold'])}｜{state}")
    return "\n".join(lines)


async def _tool_run_monitor_once(args: dict, ctx) -> str:
    """按名称或 id 立即执行一次监控，并把结果落库。"""
    key = str(args.get("monitor") or "").strip()
    if not key:
        return "请给出监控名或数字 id。"
    try:
        rows = await monitor_svc.list_monitors()
    except Exception as exc:  # noqa: BLE001 — 工具失败只回喂模型
        return f"监控列表读取失败（{type(exc).__name__}），请稍后重试。"
    row = _find_monitor(rows, key)
    if row is None:
        return f"未找到监控项 {key!r}。先用 list_monitors 查看可用监控。"
    result = await monitor_svc.run_monitor_by_id(row["id"])
    if result is None:
        return f"监控项 {key!r} 已不存在。"
    if result["status"] == "unknown":
        return (f"监控 #{row['id']}「{row['name']}」本次采集失败（unknown）："
                f"{result.get('error')}。旧值保留在页面上，不要当成最新值。")
    unit = f" {row['unit']}" if row["unit"] else ""
    return (f"监控 #{row['id']}「{row['name']}」本次值 "
            f"{_format_number(result['value'])}{unit}，状态 {result['status']}"
            f"（阈值 {row['op']} {_format_number(row['threshold'])}）。")


async def _tool_create_monitor(args: dict, ctx) -> str:
    """创建停用草稿；返回 id 并明确提示需要人工启用。"""
    try:
        row = await monitor_svc.create_monitor(
            name=args.get("name"),
            sql=args.get("sql"),
            op=args.get("op"),
            threshold=args.get("threshold"),
            interval_sec=args.get(
                "interval_sec", monitor_svc.MONITOR_DEFAULT_INTERVAL),
            unit=args.get("unit"),
            actor=getattr(ctx, "actor", ""),
        )
    except ValueError as exc:
        return f"创建被拒：{exc}。请按规则改写后重试。"
    except Exception as exc:  # noqa: BLE001 — 工具失败只回喂模型
        return f"创建失败（{type(exc).__name__}），请检查数据库连接后重试。"
    actor = getattr(ctx, "actor", "")
    await audit(actor, ACT_DBAGENT_MONITOR_CREATE, {
        "monitor_id": row["id"],
        "name": row["name"],
        "sql_summary": str(row["sql"])[:120],
        "source": "agent",
    })
    return (f"已创建停用草稿 #{row['id']}「{row['name']}」。"
            "它不会自动采集；需要在数据监控页面由人手动启用后才按间隔运行。")
