import logging
import sys
import os
import contextvars
from datetime import datetime, timezone
from .jfast import dumps as _fast_dumps

# 请求级 trace_id：middleware 入口设置一次，本请求所有日志直接读取（P1 #24 减少重复 OTel 调用）
_trace_id_var: contextvars.ContextVar = contextvars.ContextVar("trace_id", default=None)


def set_trace_id(trace_id: str):
    """在请求入口设置 trace_id（middleware 调用），供本请求内所有日志读取"""
    _trace_id_var.set(trace_id)


def _get_trace_id():
    """优先读请求级 contextvar；未设置（后台任务等）再回退 OTel API"""
    cached = _trace_id_var.get()
    if cached:
        return cached
    try:
        if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
            return None
        from opentelemetry import trace
        current_span = trace.get_current_span()
        if current_span:
            span_context = current_span.get_span_context()
            if span_context and span_context.is_valid:
                return format(span_context.trace_id, '032x')
    except Exception:
        # 这里绝不能打日志：本函数被 JsonFormatter.format() 调用，
        # 再发一条记录会再次进同一个 format()，缺 opentelemetry 时直接自递归到
        # RecursionError（Bug日志 65）。取不到 trace_id 就当没有。
        return None
    return None


def get_trace_id() -> str:
    """返回当前日志上下文中的 trace_id，供监测模块关联 Tempo。"""
    return _get_trace_id() or ""


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            # 用 record.created 而非重新取当前时间，减少系统调用（P1 #25）
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "trace_id": _get_trace_id()
        }
        if hasattr(record, 'extra_fields'):
            log_entry.update(record.extra_fields)
        return _fast_dumps(log_entry)


_configured = False


def setup_logging():
    global _configured
    logger = logging.getLogger()
    # 第三方 HTTP 客户端在 INFO 级会打印完整 URL；高德等接口把 Key 放在 query，
    # 必须压到 WARNING，防止密钥随日志持久化。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # 幂等守卫（P3 修复）：原先每次调用清空重加 root handlers，30 个模块 import 时
    # 反复重置，且会误伤其他模块自定义的 handler
    if _configured and logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    _configured = True
    return logger
