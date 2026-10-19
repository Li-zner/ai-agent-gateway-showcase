"""DB Agent 黄金评测夜跑（#6 观测闭环）：每天固定时刻自测一轮，报告落
app/rag_eval/reports/ 供「测评」tab 浏览，结果打 Prometheus Gauge。

- 启用开关：DBAGENT_NIGHTLY_EVAL=1（缺省不启动，避免意外 LLM 花费）
- 触发时刻：DBAGENT_NIGHTLY_EVAL_AT（默认 03:30，容器本地时区）
- 自测入口：复用 scripts/dbagent_golden_eval.py（--out 落 JSON），
  评测逻辑单一来源，不在本模块复制
- 保留最近 30 份 dbagent-golden-*.json，更早的删除
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from ...core.metrics import (
    background_loop_stopped_total,
    dbagent_nightly_eval_last_passed,
    dbagent_nightly_eval_last_run_timestamp_seconds,
    dbagent_nightly_eval_last_total,
)

EVAL_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "dbagent_golden_eval.py"
# 报告目录：容器里模块在 /app/app/services/...（多一层 app），镜像内基准是
# /app；宿主机仓库里则用 app/rag_eval/reports。用目录探测避免两处路径漂移。
_BASE = Path("/app") if Path("/app/app").is_dir() else Path(__file__).resolve().parents[2]
REPORTS_DIR = _BASE / "rag_eval" / "reports"
REPORT_KEEP = 30
SELF_BASE = os.getenv("DBAGENT_EVAL_BASE", "http://127.0.0.1:13150")
# 双实例防重键（会话级咨询锁）：多副本同时到点只跑一份，其余实例跳过。
# ponytail: 锁期间占住主池 1 条连接约 40 分钟；升级 = 专用单连接。
_NIGHTLY_LOCK_KEY = 0xDBA6E72


def _parse_at(value: str) -> tuple[int, int]:
    """"HH:MM" -> (H, M)；解析失败回退 03:30。"""
    try:
        h, m = value.split(":")
        return int(h) % 24, int(m) % 60
    except ValueError:
        return 3, 30


def _seconds_until(hour: int, minute: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _prune_old() -> None:
    reports = sorted(REPORTS_DIR.glob("dbagent-golden-*.json"))
    for old in reports[:-REPORT_KEEP]:
        old.unlink(missing_ok=True)


async def _run_once() -> None:
    """跑一轮评测并把结果写成测评 tab 可读的报告（e2e 形状）。

    直接写最终文件名——Windows bind mount 上 os.replace/rename 不可靠
    （容器可见、宿主丢失，2026-10-03 实测），直写则正常同步。
    多副本部署时用 PG 会话级咨询锁防重：另一实例在跑就整轮跳过
    （报告文件名分钟级同戳，双跑必互相覆盖，2026-10-04 双实例验收配套）。
    """
    from ...core.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as lock_conn:
        got = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", _NIGHTLY_LOCK_KEY)
        if not got:
            logger_keep("另一实例已在跑夜跑，本轮跳过")
            return
        try:
            await _run_once_locked()
        finally:
            await lock_conn.execute(
                "SELECT pg_advisory_unlock($1)", _NIGHTLY_LOCK_KEY)


async def _run_once_locked() -> None:
    """真正的单轮评测（调用方已持防重锁）。"""
    stamp = time.strftime("%Y%m%d-%H%M")
    out_path = REPORTS_DIR / f"dbagent-golden-{stamp}.json"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(EVAL_SCRIPT), SELF_BASE, "--out", str(out_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(EVAL_SCRIPT.parents[2]))
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2400)
    except asyncio.TimeoutError:
        proc.kill()
        logger_keep("夜跑超时（40 分钟）未完成，本轮放弃")
        return
    if not out_path.is_file():
        tail = (stdout or b"").decode("utf-8", "replace")[-400:]
        logger_keep(f"夜跑未产出报告（exit={proc.returncode}）输出尾：{tail}")
        try:
            from ...core.alerting import send_alert
            await send_alert(
                "DB Agent 黄金评测夜跑未产出报告",
                f"exit={proc.returncode}，输出尾：{tail}")
        except Exception as exc:  # noqa: BLE001
            logger_keep(f"夜跑未产报告警发送失败: {type(exc).__name__}: {str(exc)[:120]}")
        return
    data = json.loads(out_path.read_text(encoding="utf-8"))
    results = data.get("results") or []
    passed = sum(1 for r in results if r.get("rag_verdict") == "correct")
    _prune_old()
    dbagent_nightly_eval_last_passed.set(passed)
    dbagent_nightly_eval_last_total.set(len(results))
    dbagent_nightly_eval_last_run_timestamp_seconds.set(time.time())
    logger_keep(f"夜跑完成：通过 {passed}/{len(results)}，报告 {out_path.name}")
    if passed < len(results):
        failed_names = [r.get("id") for r in results
                        if r.get("rag_verdict") != "correct"]
        try:
            from ...core.alerting import send_alert
            await send_alert(
                "DB Agent 黄金评测夜跑存在失败",
                f"通过 {passed}/{len(results)}，失败用例：{'; '.join(map(str, failed_names[:5]))}")
        except Exception as exc:  # noqa: BLE001
            logger_keep(f"失败用例告警发送失败: {type(exc).__name__}: {str(exc)[:120]}")


def refresh_nightly_gauges() -> None:
    """从最新 dbagent-golden 报告刷新夜跑 Gauge（夜跑在子进程执行，其设置的
    值不回传主进程；由 metrics 端点生成前调用）。无报告时不动作。"""
    latest = max(REPORTS_DIR.glob("dbagent-golden-*.json"),
                 key=lambda p: p.stat().st_mtime, default=None)
    if not latest:
        return
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        results = data.get("results") or []
        passed = sum(1 for r in results if r.get("rag_verdict") == "correct")
        dbagent_nightly_eval_last_passed.set(passed)
        dbagent_nightly_eval_last_total.set(len(results))
        dbagent_nightly_eval_last_run_timestamp_seconds.set(latest.stat().st_mtime)
    except (OSError, ValueError):
        return


def logger_keep(message: str) -> None:
    """轻量日志：不引 logging 配置，走 print 由容器收集。"""
    print(f"[dbagent-nightly] {message}", flush=True)


async def start_nightly_loop() -> None:
    """常驻循环：每天到点跑一轮；异常不退出循环（下一轮再试）。"""
    hour, minute = _parse_at(os.getenv("DBAGENT_NIGHTLY_EVAL_AT", "03:30"))
    logger_keep(f"夜跑循环启动，每日 {hour:02d}:{minute:02d}，入口 {SELF_BASE}")
    try:
        while True:
            await asyncio.sleep(_seconds_until(hour, minute))
            try:
                await _run_once()
            except Exception as exc:  # noqa: BLE001 — 单轮失败不退循环
                logger_keep(f"夜跑异常：{type(exc).__name__}: {str(exc)[:200]}")
                try:
                    from ...core.alerting import send_alert
                    await send_alert("DB Agent 夜跑循环异常",
                                     f"{type(exc).__name__}: {str(exc)[:200]}")
                except Exception as alert_exc:  # noqa: BLE001
                    logger_keep(f"循环异常告警发送失败: "
                                f"{type(alert_exc).__name__}: {str(alert_exc)[:120]}")
    except asyncio.CancelledError:
        background_loop_stopped_total.labels("dbagent-nightly-eval", "cancelled").inc()
        raise
    except Exception as exc:  # noqa: BLE001
        background_loop_stopped_total.labels("dbagent-nightly-eval", "error").inc()
        logger_keep(f"循环退出：{type(exc).__name__}: {str(exc)[:150]}")
        raise
