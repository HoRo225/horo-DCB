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
MIGRATIONS: tuple[str, ...] = (
    """
DELETE FROM settings WHERE key NOT IN (
    'feature.ai', 'ai.channel_ids', 'ai.role_ids', 'ai.models',
    'feature.temp_voice', 'voice.entry_channel_id',
    'feature.steam_free_games', 'steam.channel_id', 'steam.role_ids',
    'games.channel_id', 'games.role_ids', 'games.hub_message'
);
DELETE FROM requests;
INSERT OR IGNORE INTO steam_posted (app_id)
SELECT json_extract(payload, '$.offer.app_id') FROM discord_effects
WHERE kind = 'steam.announce' AND status IN ('pending', 'unknown', 'failed');
DELETE FROM discord_effects
WHERE status NOT IN ('pending', 'unknown', 'failed') OR kind NOT IN ('voice.create', 'voice.delete');
DELETE FROM operations
WHERE operation_id NOT IN (SELECT operation_id FROM discord_effects WHERE operation_id IS NOT NULL);
DELETE FROM game_tables WHERE game NOT IN ('blackjack', 'slots');
INSERT OR IGNORE INTO codex_pending_archives (thread_id)
SELECT thread_id FROM codex_threads WHERE conversation_key GLOB 'guild:*:thread:*' AND parent_channel_id IS NULL;
DELETE FROM codex_threads WHERE conversation_key GLOB 'guild:*:thread:*' AND parent_channel_id IS NULL;
""",
)
