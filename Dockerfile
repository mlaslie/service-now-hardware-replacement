FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY config ./config
# Run as an unprivileged user: the agent only reads its code and config, and writes nothing to disk.
RUN useradd --system --create-home --uid 10001 agent
USER agent
ENV PORT=8080 PATH="/app/.venv/bin:$PATH" HOME=/home/agent
CMD exec uvicorn app.server:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 75
