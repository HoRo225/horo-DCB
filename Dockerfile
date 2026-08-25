FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip check \
    && python -m pip uninstall --yes pip \
    && rm -rf /usr/local/bin/pip* /usr/local/lib/python3.14/site-packages/pip*

RUN addgroup -g 10001 bot \
    && adduser -D -u 10001 -G bot bot \
    && mkdir -p /app/data \
    && chown bot:bot /app/data \
    && chmod 700 /app/data
COPY --chown=bot:bot src ./src
COPY --chown=bot:bot tests ./tests

USER bot
CMD ["python", "-m", "src.bot"]
