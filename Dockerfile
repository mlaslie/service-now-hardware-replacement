FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
ENV PORT=8080 PATH="/app/.venv/bin:$PATH"
CMD exec uvicorn app.server:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 75
