FROM python:3.14.7-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f

ARG VCS_REF
LABEL org.opencontainers.image.source="https://github.com/HoRo225/horo-DCB" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip check \
    && python -m pip uninstall --yes pip \
    && rm -rf /usr/local/bin/pip* /usr/local/lib/python3.14/site-packages/pip*

RUN ln -s "$(python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')" \
    /usr/local/bin/codex

RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin bot \
    && mkdir -p /app/data /app/codex /app/codex-workspace \
    && chown bot:bot /app/data /app/codex /app/codex-workspace \
    && chmod 700 /app/data /app/codex \
    && chmod 500 /app/codex-workspace
COPY --chown=bot:bot src ./src
COPY --chown=bot:bot tests ./tests

USER bot
CMD ["python", "-m", "src.bot"]
