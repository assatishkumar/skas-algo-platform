# The sibling market-data package `skas-data` is pulled in as a NAMED build context
# (docker-compose `additional_contexts: skas-data: ../skas-data`, or
# `docker build --build-context skas-data=../skas-data`). The MAIN context stays this repo,
# so .dockerignore here keeps skas_algo.db / .env / node_modules out of the build. Only each
# package's pyproject + source are copied — no secrets, no local data dirs
# (skas-data/data is ~400 MB). The market-data CACHE (~/.skas_data, ~650 MB, changes daily)
# is NOT baked in — mount it as a volume (see docker-compose.yaml). Requires BuildKit
# (default in Docker / Compose v2).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN pip install --upgrade pip

# 1) skas-data (market-data / broker abstraction) — from the named context. Source only.
COPY --from=skas-data pyproject.toml README.md ./skas-data/
COPY --from=skas-data src ./skas-data/src
RUN pip install ./skas-data

# 2) skas-algo (the platform) — from the main context. Source + Alembic migrations.
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install .

EXPOSE 8080

# uvicorn directly (single process; no --reload). Schema is created on startup
# (create_all); run `alembic upgrade head` on the VPS for column migrations.
CMD ["uvicorn", "skas_algo.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
