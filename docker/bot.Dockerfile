FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && mkdir -p /data /run/codex /tmp/codex-daemon-1000 && chown -R 1000:1000 /app /data /run/codex /tmp/codex-daemon-1000
USER 1000:1000
CMD ["python", "-m", "horo_dcb"]
