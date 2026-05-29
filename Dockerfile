FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps:
# - libjpeg/zlib for Pillow source builds on uncommon arches
# - libpq5 is the PostgreSQL client lib that psycopg2-binary links against
# - curl for healthcheck scripts
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY alembic/ ./alembic/
COPY alembic.ini gunicorn.conf.py ./

# data/ holds the SQLite DB (dev fallback) and uploads. On Railway, mount a
# persistent volume here.
RUN mkdir -p ./data/uploads

EXPOSE 8000

# Run pending migrations, then start the app under gunicorn with uvicorn
# workers. ``alembic upgrade head`` is idempotent — re-running it on an
# already-current database is a no-op.
CMD ["sh", "-c", "alembic upgrade head && exec gunicorn -c gunicorn.conf.py backend.main:app"]
