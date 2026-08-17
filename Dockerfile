FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN python -m pip install --no-cache-dir uv==0.10.0

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

COPY horo_dcb ./horo_dcb

RUN useradd --create-home --uid 10001 bot
USER bot

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "horo_dcb"]
