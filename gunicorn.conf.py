"""Gunicorn configuration for the production CardTax deploy.

Run with::

    gunicorn -c gunicorn.conf.py backend.main:app

Defaults are tuned for a small instance (1–2 vCPU). Override via env vars in
the deployment platform — ``WEB_CONCURRENCY`` overrides ``workers``, ``PORT``
overrides the bind address.
"""

from __future__ import annotations

import multiprocessing
import os


# --- networking ------------------------------------------------------------

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# --- workers ---------------------------------------------------------------

# 2 * CPU + 1 is the classic gunicorn recommendation. ``WEB_CONCURRENCY`` is
# the standard escape hatch (Heroku/Railway honor it) for platforms that
# already know how much CPU the container has.
_default_workers = multiprocessing.cpu_count() * 2 + 1
workers = int(os.environ.get("WEB_CONCURRENCY", str(_default_workers)))

# FastAPI is async — use the uvicorn worker so each worker can serve many
# concurrent in-flight requests on its single event loop.
worker_class = "uvicorn.workers.UvicornWorker"

# --- timeouts --------------------------------------------------------------

# Hard limit per request. Tax-report generation can be slow on huge accounts,
# so we give each request 60 s before the master kills the worker.
timeout = 60

# When SIGTERM arrives, wait this long for in-flight requests to finish
# before sending SIGKILL. Matches typical platform shutdown windows
# (Railway/Kubernetes/ECS all give ~30 s by default).
graceful_timeout = 30

# Keep-alive for connections held open by the load balancer.
keepalive = 5

# --- logging ---------------------------------------------------------------

# Send access + error logs to stdout/stderr so the platform's log shipper
# picks them up without us managing files.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)ss'
)

# --- process management ----------------------------------------------------

# Recycle workers periodically to mitigate slow leaks (especially psycopg2
# memory growth). 1000 requests * tiny jitter keeps the rotation staggered.
max_requests = 1000
max_requests_jitter = 100

# Pre-load the app in the master so each worker starts faster and we share
# the same code pages via copy-on-write. The cost is that startup errors
# crash the master immediately — which is what we want.
preload_app = True
