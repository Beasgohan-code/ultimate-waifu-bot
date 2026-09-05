# Ultimate Waifu Bot — production image.
# Multi-stage: build wheels in the builder, ship a slim runtime with a non-root user.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libjpeg-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY waifu ./waifu
# Wheels are installed into /install so the runtime stage can copy a clean tree.
RUN pip wheel --wheel-dir /install/wheels .


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/var/lib/waifu

RUN apt-get update \
 && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -r waifu && useradd -r -g waifu -d /var/lib/waifu waifu

WORKDIR /app
COPY --from=builder /install/wheels /tmp/wheels
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels

COPY waifu ./waifu
COPY scripts ./scripts
RUN mkdir -p /var/lib/waifu/data && chown -R waifu:waifu /var/lib/waifu /app

USER waifu
EXPOSE 8081
VOLUME ["/var/lib/waifu/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${WEBHOOK_LISTEN_PORT:-8081}/healthz" || exit 1

# `bot` = polling, `webhook` = webhook + aiohttp site, `migrate` = one-shot schema apply.
CMD ["python", "-m", "waifu", "bot"]
