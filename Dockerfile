FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY chatwoot_autoreply_bot/ ./chatwoot_autoreply_bot/
RUN uv sync --frozen --no-dev

RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --home-dir /app --no-create-home bot \
    && chown -R bot:bot /app
USER bot

EXPOSE 8000
ENV PORT=8000

ENTRYPOINT ["python", "-m", "chatwoot_autoreply_bot"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=3)" || exit 1
