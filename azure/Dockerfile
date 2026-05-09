# Multi-stage build for the CTS Scoreboard Azure relay.
# Slim runtime image with non-root user.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

RUN pip install --upgrade pip build \
 && pip install --prefix=/install . \
 && pip install --prefix=/install --no-deps --force-reinstall "packaging>=24"

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HOME=/tmp

RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --no-create-home --home-dir /tmp app

COPY --from=builder /install /usr/local
COPY --from=builder /build/app /srv/app
WORKDIR /srv

USER app
EXPOSE 8000

# Uvicorn workers via gunicorn; one worker per CPU is standard, override via env.
CMD ["gunicorn", "app.main:asgi_app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--timeout", "60"]
