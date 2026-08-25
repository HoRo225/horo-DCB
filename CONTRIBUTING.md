# Contributing

## Development setup

1. Install Docker Engine or Docker Desktop with Docker Compose v2.
2. Run `sh scripts/setup.sh`.
3. Replace the remaining `[REDACTED_SECRET]` values in `.env`.
4. Run `sh scripts/check-env.sh`.
5. Build with `docker compose build bot 9router`.

Do not commit `.env`, Docker volumes, SQLite files, Discord payloads, provider responses, or local `.ai-bridge` state.

## Required checks

Run the same checks as CI before opening a pull request:

```sh
docker compose config --quiet
docker compose build bot 9router
docker compose run --rm --no-deps bot python -m unittest discover -s tests -v
docker compose run --rm --no-deps bot python -m src.preflight
docker compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/pycache bot python -m compileall -q src tests
```

The Bot image runs `pip check` during `docker compose build`; `pip` is then removed from the runtime image to reduce the executable and vulnerability surface.

Do not restart or recreate an existing production stack as part of development verification. Use CI or an isolated Compose project for live smoke tests.

## Architecture rules

- `src/bot.py` owns Discord event routing and dependency composition, not provider protocol parsing, image validation, or Discord output formatting.
- `src/discord_images.py` owns Discord attachment validation and conversion.
- `src/discord_output.py` owns Discord response splitting and Components V2 output construction.
- `src/chat.py` owns short-term conversation state and the bounded agent loop.
- `src/agent_tools.py` owns tool schemas, argument validation, and safe tool execution.
- `src/ai_client.py` is the sole 9Router HTTP boundary.
- `src/semantic_memory.py` owns durable memory persistence, embedding retries, ranking, and deletion synchronization.
- Optional dependencies such as Semantic Memory are wired explicitly by `src/bot.py::main()` through constructors; do not add two-stage attach methods or cross-object dependency discovery.
- Feature modules must expose narrow public interfaces. Do not read another module's underscore-prefixed fields.
- Discord callbacks must fail closed at external boundaries and must not expose exception details or credentials to users.

Prefer a focused module change with direct tests over a cross-cutting rewrite. A new feature must document its Discord permissions, persisted data, external costs, and shutdown behavior.

## Tests

Tests use the standard library `unittest` runner and must be deterministic. Network, Discord, Steam, web-search, and LLM interactions must be replaced by bounded fakes or mocks.

Add regression coverage for:

- malformed and oversized input;
- permission and HTTP failures;
- cancellation and shutdown;
- restart recovery and stale persisted state;
- duplicate events and retry behavior;
- privacy and deletion synchronization;
- any newly introduced feature flag.

## Dependency updates

`requirements.txt` is the single complete pinned runtime dependency manifest used by Docker builds, CI, GitHub dependency graph, and Dependabot. The direct runtime dependencies are currently `discord.py` and `aiohttp`; the remaining entries are pinned transitive dependencies. Dependabot's root `pip` configuration updates this same file. Manual dependency changes must keep the complete resolved set pinned and must include the full build and test result. Do not introduce a second parallel dependency manifest or lock file unless the dependency strategy is deliberately redesigned.

The Python base image and 9Router image are pinned by digest. A digest update is not a formatting-only change: inspect upstream release notes and security advisories, rebuild both images, and rerun every test.

## 9Router upstream updates

`9router/Dockerfile` must directly inherit one official immutable digest. Do not add source rebuilds or patches against `.next`, minified strings, or Webpack modules.

For an upstream update:

1. inspect upstream release notes and security advisories;
2. change only the official digest and Compose image tag;
3. copy the current `9router_data` to a temporary Volume and boot the candidate image in isolation;
4. verify health, version, `/v1/models`, Chat, Embedding, Search, and Fetch contracts;
5. rebuild both project images and run the complete test suite plus the 9Router smoke test;
6. document compatibility changes, provider limitations, and rollback evidence.

## Pull request scope

A pull request should state:

- the user-visible behavior changed;
- persistence or migration impact;
- required Discord or provider configuration;
- tests executed;
- rollback procedure;
- any unresolved risk.

The repository owner must select and add an explicit license before accepting outside contributions or describing the repository as open source.
