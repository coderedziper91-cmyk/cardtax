"""Tiny in-memory IP+key rate limiter.

No extra deps. Single-process only — fine for a single-server deploy. Keeps a
deque of hit timestamps per (key, ip) bucket and prunes anything older than
the window on each check. Used to slow brute-force attempts on auth endpoints.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

from fastapi import HTTPException, Request


# bucket[(key, ip)] -> deque[float timestamps]
_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_lock = Lock()


def _client_ip(request: Request) -> str:
    # Trust X-Forwarded-For if present (Railway/Heroku/etc terminate TLS upstream).
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # First entry is the original client per convention.
        return fwd.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def check(
    request: Request,
    key: str,
    *,
    limit: int,
    window_seconds: int,
    message: Optional[str] = None,
) -> None:
    """Raise 429 if the (key, client-ip) bucket exceeded `limit` in `window_seconds`.

    Call this at the *top* of a route handler. The bucket is pruned in place.
    """
    ip = _client_ip(request)
    now = time.time()
    cutoff = now - window_seconds
    bucket_key = (key, ip)

    with _lock:
        bucket = _buckets[bucket_key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            # Retry-after = seconds until the oldest hit falls out of the window.
            retry_after = max(1, int(bucket[0] + window_seconds - now))
            raise HTTPException(
                status_code=429,
                detail=message or (
                    f"Too many attempts. Try again in {retry_after} seconds."
                ),
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


def reset(key: str, ip: Optional[str] = None) -> None:
    """Clear hits for a key (and optionally a single IP). Useful after a
    successful login so a previously-locked-out user can keep going."""
    with _lock:
        if ip is None:
            for k in list(_buckets.keys()):
                if k[0] == key:
                    del _buckets[k]
        else:
            _buckets.pop((key, ip), None)


# ---------------------------------------------------------------------------
# Global rate limiter — applied per-request via middleware
# ---------------------------------------------------------------------------


_global_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_global_lock = Lock()


def consume_global(identity: str, *, limit: int, window_seconds: int) -> Optional[int]:
    """Charge one request against the global bucket for ``identity``.

    Returns ``None`` when the request is allowed; returns the Retry-After
    seconds when the bucket is full. Unlike ``check()`` this does not raise —
    the middleware shapes the 429 response itself.
    """
    now = time.time()
    cutoff = now - window_seconds
    key = (f"global:{window_seconds}", identity)
    with _global_lock:
        bucket = _global_buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return max(1, int(bucket[0] + window_seconds - now))
        bucket.append(now)
    return None


def client_ip(request: Request) -> str:
    """Public re-export of the IP extractor for middleware use."""
    return _client_ip(request)


def reset_global() -> None:
    """Clear all global buckets — used in tests so request counts don't
    accumulate across cases."""
    with _global_lock:
        _global_buckets.clear()
