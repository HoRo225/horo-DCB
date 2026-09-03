# Contributing

## Development flow

Use an isolated branch or worktree. Keep the diff focused, preserve unrelated user changes, and write the smallest test that proves each non-trivial behavior before implementation.

The supported build and test path is Docker; do not install project packages on the workstation:

~~~sh
sh scripts/setup.sh
docker compose config --quiet
docker compose build bot
docker compose run --rm -T --no-deps bot python -m unittest discover -s tests -v
docker compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/pycache bot python -m compileall -q src tests
~~~

Runtime code must continue to work as UID/GID 10001 with a read-only root filesystem and without pip.

## Architecture rules

- src/config.py is the only Bot environment parser.
- src/codex_bridge_client.py is the only Bot-to-Codex HTTP boundary.
- src/codex_bridge.py owns SDK lifecycle, OAuth readiness and persistent thread mapping.
- Bridge URL remains internal and fixed; do not publish port 8765.
- Bot must not mount codex_data or receive CODEX_HOME.
- Sidecar must not mount bot_data or receive DISCORD_TOKEN.
- Never log prompts, images, Bearer tokens, OAuth email or raw SDK/RPC errors.
- Keep the Discord Guild／Channel／User allowlist fail closed.
- Do not add API-key, 9Router or automatic retry fallback.
- Do not add MCP, local memories, shell, write actions or extra agents without a separate reviewed design.
- Calendar and Steam remain UI／scheduler features, not natural-language tools.

## Dependencies

requirements.txt is the only Python dependency manifest. Direct and transitive packages are fully pinned; do not add a second requirements file or lock file.

Changes to openai-codex, openai-codex-cli-bin, Python or the base-image digest require:

1. official release and security review;
2. a clean shared-image build and pip check;
3. the full automated suite;
4. SDK and bundled runtime version verification;
5. a disposable ChatGPT device-login gate covering new turn, resume, restart resume, live Web Search and JPEG／PNG／WebP;
6. Compose isolation checks.

Never assume a ChatGPT plan supports a capability solely from documentation. If the target account gate fails, report the blocker.

## Tests

Use fake SDK and aiohttp test servers in unit tests. CI must not contact OpenAI, Discord or Steam. Security-boundary tests should cover malformed input and verify that protected calls are not made.

When changing a non-AI feature, retain its focused regression tests. When deleting a runtime feature, delete its dead adapter and tests rather than leaving dormant abstractions.

## Documentation and deployment

Update README.md, horo-DCB.md and SECURITY.md when trust boundaries, data retention, user-visible features or rollout steps change.

Production horo-laptop is image-only. Source builds occur on horo-server, and images move as complete OCI archives. Do not deploy from an uncommitted tree, delete rollback artifacts inside the seven-day window, or clean old Volumes without a separate explicit confirmation.
