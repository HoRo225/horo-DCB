FROM debian:bookworm-slim AS download

ARG CODEX_VERSION=0.165.0
ARG CODEX_SHA256=e97c4463d7f146692a955d7b0e1c8cf48f97cd4d616cd45c5dcab2e8d9743316
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/codex.tar.gz "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-package-x86_64-unknown-linux-musl.tar.gz" \
    && echo "${CODEX_SHA256}  /tmp/codex.tar.gz" | sha256sum -c - \
    && mkdir /opt/codex \
    && tar -xzf /tmp/codex.tar.gz -C /opt/codex \
    && rm /tmp/codex.tar.gz

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY --from=download /opt/codex /opt/codex
RUN ln -s /opt/codex/bin/codex /usr/local/bin/codex \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /app/codex /workspace \
    && chown app:app /app/codex

ENV CODEX_HOME=/app/codex
WORKDIR /workspace
USER app
