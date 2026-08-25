# Security Policy

## Supported versions

Until this project publishes versioned releases, security fixes target the current default branch only. Older commits and locally modified 9Router bundles are not supported.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability.

After the repository enables GitHub private vulnerability reporting, submit a private report from the repository **Security** page. Before that feature is enabled, contact the repository owner through a verified private channel.

Include:

- affected commit or image digest;
- impact and required attacker access;
- minimal reproduction steps;
- whether Discord, 9Router, Tavily, or another provider is involved;
- suggested mitigation, when known.

Never include live credentials, Discord message contents, SQLite databases, Docker volumes, or provider responses in a report. Replace all sensitive values with `[REDACTED_SECRET]`.

## Immediate containment

When a credential may have been exposed, stop the affected deployment and rotate the relevant values rather than only editing Git history:

- Discord Bot token;
- 9Router API key and provider credentials;
- 9Router JWT, API-key signing secret, initial password, and machine salt;
- Tavily or other web-provider credentials.

Review container logs and revoke compromised sessions before restarting.

## Security boundaries

- 9Router is bound to `127.0.0.1:20128` by default. Do not expose it publicly without a reviewed reverse proxy, TLS, authentication, and trusted proxy-header configuration.
- Semantic Memory stores selected Discord message text and embeddings in the `bot_data` Docker volume. It is disabled in fresh setups and should be enabled only after privacy, retention, and access requirements are defined.
- `9router/Dockerfile` directly inherits an official immutable image and must not patch `.next` or minified bundles. Every upstream digest update requires isolated Volume compatibility checks, full CI, and the 9Router smoke test before deployment.
- Generated `.env` values and Docker volumes are deployment secrets and must not be committed or attached to bug reports.

## Upstream vulnerabilities

A vulnerability in discord.py, aiohttp, Python, Node.js, Docker, or 9Router may affect this project even when local code is unchanged. Dependabot proposals are review inputs, not automatic deployment approval; changes must pass the project test suite and the 9Router smoke test.
