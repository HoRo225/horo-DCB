# Security Policy

## Supported version

Until versioned releases exist, security fixes target the current default branch. Locally modified runtime images and expired rollback artifacts are unsupported.

## Reporting

Report vulnerabilities privately to the repository owner. Include the affected commit, deployment topology, reproduction steps, impact, and whether Discord, the Codex bridge or OpenAI is involved.

Do not include live credentials, OAuth files, prompts, images, private Discord content or Volume archives in issues.

## Secrets

The following are secrets and must never be committed, logged or pasted into support channels:

- DISCORD_TOKEN
- CODEX_BRIDGE_TOKEN
- everything stored in codex_data, including OAuth credentials and thread state
- any retained legacy provider or 9Router credentials during the rollback window

.env must remain mode 0600. Rotate the Discord or bridge token after suspected exposure. If Codex OAuth data may be exposed, stop the sidecar, revoke the affected account session through the account controls, remove the confirmed codex_data Volume, and complete device login again.

## Security boundaries

Codex access is permanently restricted by an exact Guild, persisted parent Channels and one or more non-default Role IDs. Legacy exact User IDs may authorize only until the first Role set is persisted; they must never bypass an existing Role allowlist. DM and non-allowlisted events must not call the bridge. Role changes must archive existing Guild conversations before the new access set becomes active. Programmatic execution must not be exposed as a public Bot feature.

The bridge:

- is reachable only on the private Compose network;
- authenticates every /v1 endpoint with a 64-character Bearer secret;
- leaves only /healthz unauthenticated and returns no account details there;
- validates JSON shape, IDs, text length, image media type and total size;
- uses constant-time token comparison;
- returns fixed error codes and never raw SDK or RPC content.

Bot and sidecar have separate Volumes and secrets. Both run non-root with a read-only root filesystem and no-new-privileges. The sidecar uses a read-only Codex sandbox, deny-all approvals, no inherited shell environment, no MCP, no local memories, no connectors, no subagents and no shell or write actions.

## Data handling

Direct mention／reply text and accepted images are sent to OpenAI. Live Web Search queries and fetched results are handled by the Codex runtime. Inform server members before enabling the feature and do not submit secrets or regulated data.

codex_data contains sensitive OAuth cache and persistent thread mapping. Backup, transport and diagnostic copies require the same protection as credentials. Clearing only the mapping does not guarantee remote deletion; channel／guild cleanup uses best-effort thread archive. Full local removal requires deleting codex_data and then logging in again.

bot_data may contain Codex Channel／Role access state plus Calendar, Steam, voice and Server Activity state. Legacy Semantic Memory files can remain during rollback but the current image must not open or modify them.

All members with an allowlisted Role intentionally share the same persistent Codex context inside one Discord Thread. Assigning that Role grants access to that shared AI context; normal Channels remain separated by User ID.

## Dependency and image updates

Python, discord.py, aiohttp, openai-codex, the bundled Codex runtime and Docker may affect security without local source changes. Dependabot output is review input, not deployment approval.

A runtime dependency or image digest update requires a clean build, pip check, complete automated tests, Compose isolation verification and a disposable live Codex gate. Production receives the complete verified OCI image; do not rebuild on the image-only host.

## Incident containment

If the bridge behaves unexpectedly:

1. stop bot and codex;
2. preserve logs without copying prompts or OAuth files;
3. rotate CODEX_BRIDGE_TOKEN;
4. revoke and recreate Codex authentication when credential exposure is possible;
5. restore the last verified compose and images if within rollback;
6. verify Discord, Calendar, Steam, voice and Server Activity before reopening access.

Do not delete bot_data, codex_data or legacy rollback Volumes during containment unless the exact destructive target and recovery impact have been confirmed.
