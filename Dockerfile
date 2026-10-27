FROM python:3.14-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-color-emoji fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --uid 10001 --create-home app && mkdir -p /app/data /app/codex /app/codex-workspace && chown app:app /app/data /app/codex /app/codex-workspace
COPY codex/requirements.toml /etc/codex/requirements.toml
COPY src ./src
RUN python -m compileall -q src

ENV PYTHONUNBUFFERED=1
STOPSIGNAL SIGINT
USER app
CMD ["python", "-m", "src.bot"]
