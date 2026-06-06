FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the package
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8080

CMD ["uvicorn", "skas_algo.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
