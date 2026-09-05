# Security Policy

## Supported version

Security fixes target the current default branch and the recorded deployed revision. Deployments must use an immutable CI-verified image whose source commit is available in this repository. Unrecorded local runtime modifications and expired rollback artifacts are unsupported.

## Reporting

Report vulnerabilities privately to the repository owner. Include the affected commit, deployment topology, reproduction steps, impact, and whether Discord, the Codex bridge or OpenAI is involved.

Do not include live credentials, OAuth files, prompts, images, private Discord content or Volume archives in issues.

## Secrets

The following are secrets and must never be committed, logged or pasted into support channels:

- DISCORD_TOKEN
- CODEX_BRIDGE_TOKEN
- everything stored in codex_data, including OAuth credentials and thread state

.env must remain mode 0600. Rotate the Discord or bridge token after suspected exposure. If Codex OAuth data may be exposed, stop the sidecar, revoke the affected account session through the account controls, remove the confirmed codex_data Volume, and complete device login again.

## Security boundaries

Codex access is permanently restricted by an exact Guild, persisted parent Channels and one or more non-default Role IDs. Legacy exact User IDs apply only to explicit v1/v2 bootstrap state; v3 always uses roles, including an empty set that denies everyone. A corrupt or unreadable state must remain in denied role mode until both channels and roles are repaired. Removing a state file is not a repair procedure; keep AI disabled while restoring valid authorization. DM and non-allowlisted events must not call the bridge. Role changes must serialize through one shared mutation lock, invalidate accepted generations, cancel and wait for old work, then durably detach mappings and best-effort archive before the new access set becomes active. Member roles must be refreshed when cache freshness is unavailable. The privileged Members intent is used only for immediate Codex revocation after a member role change; those events must not be persisted. Success and error output remain registered and revocation-aware until delivery ends. Programmatic execution must not be exposed as a public Bot feature.

The bridge:

- is reachable only on the private Compose network;
- authenticates every /v1 endpoint with a 64-character Bearer secret;
- leaves only /healthz unauthenticated and returns no account details there;
- validates JSON shape, IDs, text length, image media type and total size;
- uses constant-time token comparison;
- returns fixed error codes and never raw SDK or RPC content;
- bounds active/queued work, total deadlines and cancellation cleanup;
- refuses further chat writes after mapping persistence failure while retaining the original durable state;
- handles disconnected callers and unresponsive RPC workers without replaying prompts.

Bot and sidecar have separate Volumes and secrets. Both run non-root with a read-only root filesystem and no-new-privileges. The sidecar uses a read-only Codex sandbox, deny-all approvals, no inherited shell environment, no MCP, no local memories, no connectors, no subagents and no shell or write actions.

## Data handling

Direct mention／reply text and accepted images are sent to OpenAI. Live Web Search queries and fetched results are handled by the Codex runtime. Inform server members before enabling the feature and do not submit secrets or regulated data.

codex_data contains sensitive OAuth cache and persistent thread mapping. Backup, transport and diagnostic copies require the same protection as credentials. Clearing only the mapping does not guarantee remote deletion; channel／guild cleanup uses best-effort thread archive. Full local removal requires deleting codex_data and then logging in again.

bot_data may contain Codex Channel／Role access state plus Calendar, Steam and voice state. The Bot must not persist Guild activity metadata or create app-owned SQLite databases. Semantic Memory, Server Activity and 9Router data and rollback points are retired.

All members with an allowlisted Role intentionally share the same persistent Codex context inside one Discord Thread. Assigning that Role grants access to that shared AI context; normal Channels remain separated by User ID.

## Dependency and image updates

Python, discord.py, aiohttp, openai-codex, the bundled Codex runtime and Docker may affect security without local source changes. Dependabot output is review input, not deployment approval.

A runtime dependency or image digest update requires a clean build, pip check, complete automated tests, Compose isolation verification and a disposable live Codex gate. Production receives the exact image exported by the successful isolated CI run, including source/revision labels and a verified archive checksum; do not rebuild on the image-only host.

## Incident containment

If the bridge behaves unexpectedly:

1. stop bot and codex;
2. preserve logs without copying prompts or OAuth files;
3. rotate CODEX_BRIDGE_TOKEN;
4. revoke and recreate Codex authentication when credential exposure is possible;
5. restore the last verified compose and images if within rollback, keeping AI disabled if missing/corrupt/empty-role state could trigger an older image's legacy fallback;
6. verify Discord, Calendar, Steam and voice before reopening access.

Do not delete bot_data or codex_data during containment unless the exact destructive target and recovery impact have been confirmed.
