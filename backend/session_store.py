"""Redis-backed session middleware with cookie fallback.

When ``REDIS_URL`` is set and a connection can be established, sessions are
stored server-side in Redis keyed by an opaque session-id cookie. The cookie
itself carries no user data — losing it just logs the user out, it can't
forge a session.

When ``REDIS_URL`` is unset (or Redis is unreachable at startup), callers
should fall back to ``starlette.middleware.sessions.SessionMiddleware``,
which signs the session payload into the cookie itself. The public
``build_session_middleware()`` helper picks the right one.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Optional

from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Lazy redis client — module-level so we only build once and only when needed.
_redis_client = None
_redis_attempted = False


def _redis() -> Optional[object]:
    """Return a connected redis client, or None when Redis is unconfigured/unreachable.
    The result is memoized: we only try once per process."""
    global _redis_client, _redis_attempted
    if _redis_attempted:
        return _redis_client
    _redis_attempted = True
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        import redis  # type: ignore
        client = redis.from_url(url, decode_responses=True, socket_timeout=2)
        client.ping()  # fail fast if Redis is unreachable
        _redis_client = client
    except Exception as e:  # pragma: no cover — depends on infra
        print(f"[session_store] Redis unavailable, falling back to cookies: {e}")
        _redis_client = None
    return _redis_client


def redis_available() -> bool:
    return _redis() is not None


class RedisSessionMiddleware:
    """ASGI middleware that stores ``scope['session']`` in Redis under an
    opaque cookie-borne session id.

    Shape-compatible with Starlette's ``SessionMiddleware``: downstream code
    keeps using ``request.session`` exactly the same way.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        client,
        session_cookie: str = "session",
        max_age: int = 60 * 60 * 24 * 30,
        same_site: str = "lax",
        https_only: bool = False,
        key_prefix: str = "ctx_sess:",
    ):
        self.app = app
        self.client = client
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.same_site = same_site
        self.https_only = https_only
        self.key_prefix = key_prefix

    def _load(self, sid: str) -> dict:
        try:
            raw = self.client.get(self.key_prefix + sid)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    def _save(self, sid: str, data: dict) -> None:
        try:
            self.client.setex(
                self.key_prefix + sid, self.max_age, json.dumps(data, default=str),
            )
        except Exception:
            pass

    def _delete(self, sid: str) -> None:
        try:
            self.client.delete(self.key_prefix + sid)
        except Exception:
            pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Parse cookies manually — the headers list is bytes pairs.
        cookies: dict[str, str] = {}
        for name, value in scope.get("headers") or []:
            if name == b"cookie":
                for pair in value.decode("latin-1").split(";"):
                    if "=" in pair:
                        k, _, v = pair.strip().partition("=")
                        cookies[k] = v

        sid = cookies.get(self.session_cookie) or ""
        data = self._load(sid) if sid else {}
        original_sid = sid
        original_snapshot = dict(data)
        scope["session"] = data

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                nonlocal sid
                # Decide what to do with the session after the handler ran.
                current = scope.get("session") or {}
                changed = current != original_snapshot
                if not current:
                    # Cleared — wipe the row and clear the cookie.
                    if original_sid:
                        self._delete(original_sid)
                    if original_sid:  # only clear cookie if one was set
                        _set_cookie(
                            message, self.session_cookie, "",
                            max_age=0, same_site=self.same_site,
                            secure=self.https_only,
                        )
                elif changed or not original_sid:
                    # New or modified session — mint sid if needed and persist.
                    if not sid:
                        sid = secrets.token_urlsafe(32)
                    self._save(sid, current)
                    _set_cookie(
                        message, self.session_cookie, sid,
                        max_age=self.max_age, same_site=self.same_site,
                        secure=self.https_only,
                    )
                else:
                    # Touch the TTL so active users don't get logged out.
                    if original_sid:
                        self._save(original_sid, current)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _set_cookie(
    message: Message,
    name: str,
    value: str,
    *,
    max_age: int,
    same_site: str,
    secure: bool,
) -> None:
    headers = MutableHeaders(scope=message)
    parts = [
        f"{name}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        f"SameSite={same_site.capitalize()}",
    ]
    if secure:
        parts.append("Secure")
    headers.append("set-cookie", "; ".join(parts))


def build_session_middleware_args() -> tuple[type, dict]:
    """Return ``(middleware_class, kwargs)`` for the active session backend.

    Callers do ``cls, kw = build_session_middleware_args(); app.add_middleware(cls, **kw)``.
    """
    client = _redis()
    if client is None:
        # Not configured / unavailable — caller will use cookie-signed sessions.
        return SessionMiddleware, {}
    return RedisSessionMiddleware, {"client": client}
