from __future__ import annotations

import copy
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

from src.core.app.config import DATA_DIR

DB_PATH = DATA_DIR / "horo.db"
SETTING_DEFAULTS: dict[str, object] = {
    "feature.ai": False,
    "feature.temp_voice": False,
    "feature.steam_free_games": False,
    "feature.ai_text_display": True,
    "ai.channel_ids": [],
    "ai.role_ids": [],
    "ai.models": None,
    "voice.entry_channel_id": None,
    "steam.channel_id": None,
    "steam.role_ids": [],
    "games.channel_id": None,
    "games.role_ids": [],
    "games.hub_message": None,
}


MIGRATIONS: list[str] = [
    """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS balances (
    user_id INTEGER PRIMARY KEY,
    points INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
    last_checkin TEXT
);
-- One row per open table: the primary key also enforces one table per player.
CREATE TABLE IF NOT EXISTS escrow (
    user_id INTEGER PRIMARY KEY,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    channel_id INTEGER,
    message_id INTEGER,
    game TEXT NOT NULL DEFAULT 'blackjack'
);
-- Tickets of draws not yet held; a draw deletes its period's rows when it pays out.
CREATE TABLE IF NOT EXISTS lottery_tickets (
    period TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    tickets INTEGER NOT NULL CHECK (tickets BETWEEN 1 AND 10),
    PRIMARY KEY (period, user_id)
);
CREATE TABLE IF NOT EXISTS voice_rooms (channel_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS steam_posted (app_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS lfg_cards (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    game TEXT NOT NULL,
    size INTEGER NOT NULL,
    note TEXT,
    members TEXT NOT NULL,  -- JSON list of user IDs, host first
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER PRIMARY KEY,
    job TEXT,
    job_changed_at INTEGER,
    xp INTEGER NOT NULL DEFAULT 0,
    last_work TEXT,
    birthday TEXT,  -- MM-DD
    birthday_paid TEXT  -- YYYY of the last birthday gift
);
""",
    """
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
CREATE TABLE wallet_ledger (
    entry_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    balance INTEGER NOT NULL CHECK (balance >= 0),
    reason TEXT NOT NULL,
    operation_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX wallet_ledger_user ON wallet_ledger (user_id, entry_id);
-- Only the balance at migration time is known; this is not historical income.
INSERT INTO wallet_ledger (user_id, delta, balance, reason)
SELECT user_id, points, points, 'opening_balance' FROM balances;
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
""",
    """
-- 樂奈金庫：單列、可負數；收支記在 wallet_ledger 的 user_id = 0。
CREATE TABLE house (id INTEGER PRIMARY KEY CHECK (id = 1), points INTEGER NOT NULL);
INSERT INTO house (id, points) VALUES (1, 100000);
-- 重建收支表，讓金庫列（user_id = 0）的餘額可為負數。
CREATE TABLE wallet_ledger_v3 (
    entry_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    balance INTEGER NOT NULL CHECK (balance >= 0 OR user_id = 0),
    reason TEXT NOT NULL,
    operation_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
INSERT INTO wallet_ledger_v3 (entry_id, user_id, delta, balance, reason, operation_id, metadata, created_at)
SELECT entry_id, user_id, delta, balance, reason, operation_id, metadata, created_at FROM wallet_ledger;
DROP TABLE wallet_ledger;
ALTER TABLE wallet_ledger_v3 RENAME TO wallet_ledger;
CREATE INDEX wallet_ledger_user ON wallet_ledger (user_id, entry_id);
CREATE TABLE inventory (
    user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (user_id, item)
);
-- 已發出的遊戲規則卡，重啟後清掉殘留。
CREATE TABLE rules_cards (message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL);
""",
    """
-- 上班：連續天數、未決定的事件（JSON）、最後加班日。
ALTER TABLE profiles ADD COLUMN streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE profiles ADD COLUMN pending_event TEXT;
ALTER TABLE profiles ADD COLUMN overtime_day TEXT;
""",
    """
-- 抽卡：作品（Bangumi subject ID）、卡片（card_id 順序＝匯入時收藏數排名）、收藏星數、換卡。
CREATE TABLE gacha_works (work_id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE gacha_cards (
    card_id INTEGER PRIMARY KEY,
    bangumi_id INTEGER NOT NULL,
    work_id INTEGER NOT NULL REFERENCES gacha_works (work_id),
    name TEXT NOT NULL,
    rarity TEXT NOT NULL CHECK (rarity IN ('N', 'R', 'SR', 'SSR')),
    image_url TEXT NOT NULL,
    UNIQUE (work_id, bangumi_id)
);
CREATE TABLE gacha_owned (
    user_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL REFERENCES gacha_cards (card_id),
    stars INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    PRIMARY KEY (user_id, card_id)
);
CREATE TABLE gacha_trades (
    trade_id INTEGER PRIMARY KEY,
    from_user INTEGER NOT NULL,
    to_user INTEGER NOT NULL,
    give_card INTEGER NOT NULL,
    take_card INTEGER NOT NULL,
    from_ok INTEGER NOT NULL DEFAULT 0,
    to_ok INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled')),
    channel_id INTEGER,
    message_id INTEGER,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
""",
    """
-- 每日任務：領過獎勵的日子（台北日期）。
CREATE TABLE quest_claims (user_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY (user_id, day));
""",
    """
-- 賽馬下注：race_id 是公開賽馬卡的訊息 ID，user_id 0 是樂奈（金庫出資）；結算或退款後刪除，不存戰績。
CREATE TABLE race_bets (
    race_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    horse INTEGER NOT NULL CHECK (horse BETWEEN 1 AND 5),
    amount INTEGER NOT NULL CHECK (amount > 0),
    PRIMARY KEY (race_id, user_id)
);
""",
    """
-- 每日任務只查當日收支，不隨玩家的完整歷史增加掃描量。
CREATE INDEX wallet_ledger_user_time ON wallet_ledger (user_id, created_at);
""",
    """
-- 帳本時間索引：樂透彩池 pot() 與任務 unclaimed() 只掃當天。
CREATE INDEX wallet_ledger_time ON wallet_ledger (created_at);
-- 清空舊卡池，不補償；帳本保留原紀錄。
DELETE FROM gacha_trades; DELETE FROM gacha_owned; DELETE FROM gacha_cards; DELETE FROM gacha_works;
-- 作品上架時間：抽卡頁預設最新上架、控制台依上架時間排序。
ALTER TABLE gacha_works ADD COLUMN listed_at INTEGER NOT NULL DEFAULT 0;
-- 簽到殘留；舊帳本列的「簽到」顯示文字保留。
ALTER TABLE balances DROP COLUMN last_checkin;
-- 揪團卡上樂奈最新的加油，重啟後仍在。
ALTER TABLE lfg_cards ADD COLUMN cheer TEXT;
""",
    """
-- 規則卡已移除，C1 部署時已清掉殘留，紀錄表不再需要。
DROP TABLE rules_cards;
""",
    """
CREATE TABLE game_tables (
    user_id INTEGER PRIMARY KEY,
    game TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
INSERT INTO game_tables (user_id, game, channel_id, message_id)
SELECT user_id, game, channel_id, message_id FROM escrow WHERE channel_id IS NOT NULL AND message_id IS NOT NULL;
DROP TABLE gacha_trades;
DROP TABLE gacha_owned;
DROP TABLE gacha_cards;
DROP TABLE gacha_works;
DROP TABLE race_bets;
DROP TABLE lottery_tickets;
DROP TABLE quest_claims;
DROP TABLE inventory;
DROP TABLE profiles;
DROP TABLE escrow;
DROP TABLE balances;
DROP TABLE house;
DROP TABLE wallet_ledger;
DELETE FROM settings WHERE key IN ('birthday.channel_id', 'quests.board_message', 'ai.speech_channel_id');
UPDATE discord_effects SET status = 'skipped', error = 'feature_removed', updated_at = unixepoch()
WHERE kind IN ('birthday.announce', 'lottery.announce') AND status IN ('pending', 'unknown', 'failed');
""",
    """
-- 揪團已移除：卡片表不再需要，未完成的揪團送出項目與操作紀錄改為略過，之後照常清理。
DROP TABLE lfg_cards;
UPDATE discord_effects SET status = 'skipped', error = 'feature_removed', updated_at = unixepoch()
WHERE kind IN ('lfg_create', 'lfg_edit', 'lfg_depart') AND status IN ('pending', 'unknown', 'failed');
UPDATE operations SET status = 'skipped' WHERE kind IN ('lfg_create', 'lfg_action') AND status = 'running';
""",
    """
CREATE TABLE codex_threads (
    conversation_key TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    parent_channel_id INTEGER
);
CREATE TABLE codex_pending_archives (thread_id TEXT PRIMARY KEY);
""",
]


def open_db(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    (version,) = db.execute("PRAGMA user_version").fetchone()
    if version > len(MIGRATIONS):
        db.close()
        raise RuntimeError(f"{path} is schema v{version}, newer than this code (v{len(MIGRATIONS)})")
    try:
        for n, sql in enumerate(MIGRATIONS[version:], version + 1):
            try:
                db.executescript(f"BEGIN; {sql}\nPRAGMA user_version = {n}; COMMIT;")
            finally:
                if db.in_transaction:
                    db.execute("ROLLBACK")
    except BaseException:
        db.close()
        raise
    return db


def hold_lock(path: Path = DATA_DIR / "horo.lock") -> IO[str]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        logging.critical("Another bot process holds %s; exiting.", path)
        raise SystemExit(1) from None
    return lock


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    if db.in_transaction:
        raise RuntimeError("transaction() does not nest")
    db.execute("BEGIN")
    try:
        yield
        db.execute("COMMIT")
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")


def setting(db: sqlite3.Connection, key: str) -> Any:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else copy.deepcopy(SETTING_DEFAULTS[key])


def set_setting(db: sqlite3.Connection, key: str, value: object) -> None:
    if key not in SETTING_DEFAULTS:
        raise KeyError(key)
    with transaction(db):
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
