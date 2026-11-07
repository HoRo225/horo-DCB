FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy CODEX_HOME=/data/codex HOME=/home/codex
WORKDIR /opt/runtime
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra codex --no-install-project && /opt/runtime/.venv/bin/python -c "from pathlib import Path; from codex_cli_bin import bundled_codex_path; Path('/usr/local/bin/codex').symlink_to(bundled_codex_path())" && mkdir -p /home/codex /data/codex /run/codex /tmp/codex-daemon-1000 && chown -R 1000:1000 /home/codex /data/codex /run/codex /tmp/codex-daemon-1000
WORKDIR /home/codex
USER 1000:1000
ENTRYPOINT ["sh", "-c", "exec codex \"$@\" >/dev/null 2>&1", "horo-codex"]
