"""Structured JSON logging for production.

In production (``DATABASE_URL`` set) every log record is serialized as a
single JSON object — handy for log aggregators (Railway, Datadog, etc).
In dev we leave Python's default human-readable formatter alone.

``request_log_middleware`` attaches a per-request ID to log records and emits
one summary line per request with ``request_id``, ``user_id``, ``method``,
``path``, ``status``, ``duration``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import secrets
import sys
import time
from typing import Optional

from fastapi import Request


# Per-request context for the JSON formatter.
_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_user_id_ctx: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("user_id", default=None)


def current_request_id() -> str:
    return _request_id_ctx.get()


def current_user_id() -> Optional[int]:
    return _user_id_ctx.get()


class JsonFormatter(logging.Formatter):
    """Emit log records as one-line JSON. Attaches request/user context when
    available so a single log line is enough to debug a failure."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = _request_id_ctx.get()
        if rid:
            payload["request_id"] = rid
        uid = _user_id_ctx.get()
        if uid is not None:
            payload["user_id"] = uid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Pick up any user-supplied extras (request fields, etc.).
        for key in (
            "method", "path", "status", "duration_ms",
            "remote_ip", "user_agent",
        ):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        return json.dumps(payload, default=str)


def configure_logging(production: bool) -> None:
    """Install handlers on the root logger. Idempotent — safe to call from
    startup more than once."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    if production:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
        ))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # uvicorn already logs requests in human-readable form; in production we
    # rely on our middleware's JSON summary instead, so silence its access log
    # to avoid duplicate lines.
    if production:
        logging.getLogger("uvicorn.access").handlers.clear()
        logging.getLogger("uvicorn.access").propagate = False


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


async def request_log_middleware(request: Request, call_next):
    """Per-request structured access log. Wraps ``call_next`` so failures get
    a log line too (with ``status = 500``)."""
    rid = request.headers.get("x-request-id") or secrets.token_hex(8)
    rid_token = _request_id_ctx.set(rid)
    uid_token = None
    try:
        uid = request.session.get("user_id") if hasattr(request, "session") else None
        if uid:
            uid_token = _user_id_ctx.set(int(uid))
    except (AssertionError, AttributeError, ValueError):
        pass

    start = time.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        duration_ms = int((time.time() - start) * 1000)
        logger = logging.getLogger("cardtax.request")
        # Only emit the summary line in production; dev uses uvicorn's own
        # access log which is plenty.
        if os.environ.get("DATABASE_URL"):
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": duration_ms,
                    "remote_ip": _client_ip(request),
                    "user_agent": (request.headers.get("user-agent") or "")[:200],
                },
            )
        _request_id_ctx.reset(rid_token)
        if uid_token is not None:
            _user_id_ctx.reset(uid_token)
