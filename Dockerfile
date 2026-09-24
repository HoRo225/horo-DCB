FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY src/ ./src/

# Seed the codex volume with the target for the read-only bind mount.
RUN mkdir -p /app/codex /app/data /app/codex-workspace \
    && touch /app/codex/base_instructions.txt
