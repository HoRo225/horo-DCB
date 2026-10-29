BASELINE_VERSION = 14
BASELINE = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE voice_rooms (channel_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL UNIQUE);
CREATE TABLE steam_posted (app_id INTEGER PRIMARY KEY);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    message_id INTEGER NOT NULL UNIQUE,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted', 'done', 'failed', 'interrupted')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    inputs TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed', 'skipped')),
    result TEXT NOT NULL,
    resource_id TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE discord_effects (
    effect_id TEXT PRIMARY KEY,
    operation_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'unknown', 'done', 'failed', 'skipped')),
    channel_id INTEGER,
    message_id INTEGER,
    nonce TEXT,
    payload TEXT NOT NULL,
    error TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX discord_effects_status ON discord_effects (status, kind);
CREATE TABLE game_tables (
    user_id INTEGER PRIMARY KEY,
    game TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE TABLE codex_threads (
    conversation_key TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    parent_channel_id INTEGER
);
CREATE TABLE codex_pending_archives (thread_id TEXT PRIMARY KEY);
"""
MIGRATIONS: tuple[str, ...] = ()
