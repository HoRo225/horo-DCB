from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

DEFAULT_DB_PATH = Path("/app/data/server_activity.sqlite3")
RETENTION_SECONDS = 30 * 24 * 60 * 60
QUEUE_MAX = 5000
BATCH_MAX = 100
_STOP = object()


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    guild_id: int
    occurred_at: int
    source: str
    category: str
    event_type: str
    actor_id: int | None = None
    target_id: int | None = None
    channel_id: int | None = None
    message_id: int | None = None
    audit_entry_id: int | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ServerActivityStatus:
    available: bool
    queue_size: int
    queue_capacity: int
    dropped_event_count: int


@dataclass(frozen=True, slots=True)
class ActivitySummary:
    total: int = 0
    admin: int = 0
    member: int = 0
    message: int = 0
    voice: int = 0
    other: int = 0


@dataclass(frozen=True, slots=True)
class StoredActivityEvent:
    occurred_at: int
    event_type: str
    actor_id: int | None
    target_id: int | None
    channel_id: int | None
    message_id: int | None


def _id(value: Any) -> int | None:
    value = getattr(value, "id", value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bounded(value: Any, limit: int = 100) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _timestamp(value: Any) -> int | None:
    if value is None:
        return None
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            result = timestamp()
        except (OverflowError, TypeError, ValueError):
            return None
        return int(result) if isinstance(result, (int, float)) and not isinstance(result, bool) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _enum_name(value: Any, *, default: str = "unknown", limit: int = 40) -> str:
    name = _bounded(getattr(value, "name", None), limit)
    if name is not None:
        return name
    raw = getattr(value, "value", value)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw)[:limit]
    if isinstance(raw, str):
        return raw[:limit] or default
    return default


def _safe_change_value(key: str, value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if key == "roles":
        return sorted(role_id for role_id in (_id(item) for item in value or ()) if role_id is not None)
    if key in {
        "channel_id", "owner_id", "afk_channel_id", "system_channel_id",
        "rules_channel_id", "public_updates_channel_id",
    }:
        return _id(value)
    if key == "type":
        return _enum_name(value)
    return None


def _json_safe_copy(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_copy(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_safe_copy(item) for key, item in value.items()}
    raise TypeError("activity details must contain only JSON-safe primitives")


class ServerActivityMonitor:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH, *, queue_max: int = QUEUE_MAX, batch_max: int = BATCH_MAX) -> None:
        if isinstance(queue_max, bool) or not isinstance(queue_max, int) or queue_max <= 0:
            raise ValueError("queue_max must be a positive integer")
        if isinstance(batch_max, bool) or not isinstance(batch_max, int) or batch_max <= 0:
            raise ValueError("batch_max must be a positive integer")
        self.db_path = Path(db_path)
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_max)
        self._batch_max = batch_max
        self._available = False
        self._accepting = False
        self._dropped = 0
        self._writer: asyncio.Task[None] | None = None
        self._writer_stopped = asyncio.Event()
        self._writer_stopped.set()
        self._start_lock = asyncio.Lock()
        self._db_mutation_lock = asyncio.Lock()
        self._guild_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._blocked_guild_ids: set[int] = set()
        self._pending_counts: dict[int, int] = {}
        self._pending_empty: dict[int, asyncio.Event] = {}
        self._pending_enable_guild_ids: set[int] = set()

    def _pending_event(self, guild_id: int) -> asyncio.Event:
        event = self._pending_empty.get(guild_id)
        if event is None:
            event = asyncio.Event()
            if self._pending_counts.get(guild_id, 0) == 0:
                event.set()
            self._pending_empty[guild_id] = event
        return event

    def _mark_accepted(self, event: ActivityEvent) -> None:
        guild_id = event.guild_id
        self._pending_counts[guild_id] = self._pending_counts.get(guild_id, 0) + 1
        self._pending_event(guild_id).clear()

    def _finish_activity(self, event: ActivityEvent) -> None:
        guild_id = event.guild_id
        pending = self._pending_counts[guild_id] - 1
        if pending:
            self._pending_counts[guild_id] = pending
            return
        del self._pending_counts[guild_id]
        self._pending_event(guild_id).set()
        if guild_id in self._pending_enable_guild_ids:
            self._pending_enable_guild_ids.remove(guild_id)
            self._blocked_guild_ids.discard(guild_id)

    async def start(self) -> None:
        async with self._start_lock:
            if self._writer is not None and not self._writer.done():
                return
            try:
                await asyncio.to_thread(self._initialize)
            except Exception:
                logging.error("Server activity storage initialization failed.")
                self._available = False
                self._accepting = False
                return
            self._available = True
            self._accepting = True
            self._writer_stopped.clear()
            self._writer = asyncio.create_task(
                self._writer_loop(),
                name="server-activity-writer",
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(journal_mode).lower() != "wal":
                    raise RuntimeError("server activity database did not enable WAL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS server_activity_events(
                        id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                        occurred_at INTEGER NOT NULL, source TEXT NOT NULL,
                        category TEXT NOT NULL, event_type TEXT NOT NULL,
                        actor_id INTEGER, target_id INTEGER, channel_id INTEGER,
                        message_id INTEGER, audit_entry_id INTEGER,
                        details_json TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_server_activity_audit_entry
                        ON server_activity_events(audit_entry_id) WHERE audit_entry_id IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS ix_server_activity_guild_time
                        ON server_activity_events(guild_id, occurred_at DESC);
                    CREATE INDEX IF NOT EXISTS ix_server_activity_guild_category_time
                        ON server_activity_events(guild_id, category, occurred_at DESC);
                """)
                connection.execute("DELETE FROM server_activity_events WHERE occurred_at < ?", (int(time.time()) - RETENTION_SECONDS,))
        os.chmod(self.db_path, 0o600)

    def get_runtime_status(self) -> ServerActivityStatus:
        return ServerActivityStatus(self._available, self._queue.qsize(), self._queue.maxsize, self._dropped)

    def _record(self, event: ActivityEvent) -> None:
        if not self._accepting or event.guild_id in self._blocked_guild_ids:
            return
        event = ActivityEvent(
            event.guild_id,
            event.occurred_at,
            event.source,
            event.category,
            event.event_type,
            event.actor_id,
            event.target_id,
            event.channel_id,
            event.message_id,
            event.audit_entry_id,
            _json_safe_copy(event.details) if event.details is not None else None,
        )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
        else:
            self._mark_accepted(event)

    async def _writer_loop(self) -> None:
        last_cleanup = time.monotonic()
        try:
            while True:
                item = await self._queue.get()
                if item is _STOP:
                    self._queue.task_done()
                    break
                batch = [item]
                stop_seen = False
                while len(batch) < self._batch_max:
                    try:
                        item = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is _STOP:
                        self._queue.task_done()
                        stop_seen = True
                        break
                    batch.append(item)
                cleanup = time.monotonic() - last_cleanup >= 24 * 60 * 60
                try:
                    async with self._db_mutation_lock:
                        eligible = [
                            event for event in batch
                            if event.guild_id not in self._blocked_guild_ids
                        ]
                        if eligible:
                            await asyncio.to_thread(self._write_batch, eligible, cleanup)
                            if cleanup:
                                last_cleanup = time.monotonic()
                finally:
                    for event in batch:
                        self._finish_activity(event)
                        self._queue.task_done()
                if stop_seen:
                    break
        except Exception:
            logging.error("Server activity writer failed.")
            self._available = False
            self._accepting = False
        finally:
            self._writer_stopped.set()

    def _write_batch(self, batch: list[ActivityEvent], cleanup: bool) -> None:
        rows = [
            (
                event.guild_id,
                event.occurred_at,
                event.source,
                event.category,
                event.event_type,
                event.actor_id,
                event.target_id,
                event.channel_id,
                event.message_id,
                event.audit_entry_id,
                json.dumps(
                    event.details or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            for event in batch
        ]
        with closing(self._connect()) as connection:
            with connection:
                connection.executemany("INSERT OR IGNORE INTO server_activity_events(guild_id,occurred_at,source,category,event_type,actor_id,target_id,channel_id,message_id,audit_entry_id,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
                if cleanup:
                    connection.execute("DELETE FROM server_activity_events WHERE occurred_at < ?", (int(time.time()) - RETENTION_SECONDS,))

    def _delete_guild(self, guild_id: int) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM server_activity_events WHERE guild_id = ?",
                    (guild_id,),
                )

    async def _wait_pending_or_writer_stop(self, guild_id: int) -> None:
        if self._pending_counts.get(guild_id, 0) == 0:
            return
        writer = self._writer
        if writer is None or writer.done():
            return
        pending_wait = asyncio.create_task(self._pending_event(guild_id).wait())
        writer_wait = asyncio.create_task(self._writer_stopped.wait())
        tasks = {pending_wait, writer_wait}
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            await asyncio.gather(*done)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def delete_guild(self, guild_id: int) -> None:
        async with self._guild_locks[guild_id]:
            self._pending_enable_guild_ids.discard(guild_id)
            self._blocked_guild_ids.add(guild_id)
            try:
                await self._wait_pending_or_writer_stop(guild_id)
                async with self._db_mutation_lock:
                    if self.db_path.exists():
                        await asyncio.to_thread(self._delete_guild, guild_id)
            except Exception:
                logging.error("Server activity guild cleanup failed.")
                raise RuntimeError("server activity guild cleanup failed") from None

    async def enable_guild(self, guild_id: int) -> None:
        async with self._guild_locks[guild_id]:
            if self._pending_counts.get(guild_id, 0) == 0:
                self._pending_enable_guild_ids.discard(guild_id)
                self._blocked_guild_ids.discard(guild_id)
                return
            self._pending_enable_guild_ids.add(guild_id)
            writer = self._writer
            if writer is None or writer.done():
                return
            await self._pending_event(guild_id).wait()
            self._pending_enable_guild_ids.discard(guild_id)
            self._blocked_guild_ids.discard(guild_id)

    async def close(self) -> None:
        self._accepting = False
        writer = self._writer
        if writer is None:
            return
        stop_put: asyncio.Task[None] | None = None
        try:
            if not writer.done():
                stop_put = asyncio.create_task(
                    self._queue.put(_STOP),
                    name="server-activity-stop-putter",
                )
                await self._writer_stopped.wait()
            await writer
        finally:
            if stop_put is not None and not stop_put.done():
                stop_put.cancel()
            if stop_put is not None:
                await asyncio.gather(stop_put, return_exceptions=True)
            if writer.done():
                try:
                    while True:
                        try:
                            item = self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if isinstance(item, ActivityEvent):
                            self._finish_activity(item)
                        self._queue.task_done()
                finally:
                    if self._writer is writer:
                        self._writer = None

    async def get_summary(self, guild_id: int, since: int | None = None) -> ActivitySummary:
        now = int(time.time())
        cutoff = now - RETENTION_SECONDS
        since = now - 24 * 60 * 60 if since is None else max(int(since), cutoff)
        return await asyncio.to_thread(self._get_summary, guild_id, since)

    def _get_summary(self, guild_id: int, since: int) -> ActivitySummary:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT source,category,COUNT(*) FROM server_activity_events WHERE guild_id=? AND occurred_at>=? GROUP BY source,category", (guild_id, since)).fetchall()
        counts = {"admin": 0, "member": 0, "message": 0, "voice": 0, "other": 0}
        total = 0
        for source, category, count in rows:
            total += count
            key = "admin" if source == "audit" else category if category in counts and category != "admin" else "other"
            counts[key] += count
        return ActivitySummary(total, counts["admin"], counts["member"], counts["message"], counts["voice"], counts["other"])

    async def get_recent_events(self, guild_id: int, filter_key: str = "all", limit: int = 10) -> list[StoredActivityEvent]:
        cutoff = int(time.time()) - RETENTION_SECONDS
        if filter_key not in {"all", "admin", "member", "message", "voice"}:
            filter_key = "all"
        if isinstance(limit, bool) or not isinstance(limit, int):
            limit = 10
        return await asyncio.to_thread(self._get_recent, guild_id, filter_key, max(1, min(limit, 10)), cutoff)

    def _get_recent(self, guild_id: int, filter_key: str, limit: int, cutoff: int) -> list[StoredActivityEvent]:
        where, parameters = "guild_id=? AND occurred_at>=?", [guild_id, cutoff]
        if filter_key == "admin":
            where += " AND source='audit'"
        elif filter_key != "all":
            where += " AND category=? AND source<>'audit'"
            parameters.append(filter_key)
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(f"SELECT occurred_at,event_type,actor_id,target_id,channel_id,message_id FROM server_activity_events WHERE {where} ORDER BY occurred_at DESC,id DESC LIMIT ?", parameters).fetchall()
        return [StoredActivityEvent(*row) for row in rows]

    def _emit(self, guild: Any, category: str, event_type: str, **values: Any) -> None:
        guild_id = _id(guild)
        if guild_id is not None:
            self._record(ActivityEvent(guild_id, int(time.time()), values.pop("source", "gateway"), category, event_type, **values))

    def record_audit(self, entry: Any) -> None:
        action = getattr(entry, "action", None)
        action_name = _enum_name(action)
        changes: dict[str, dict[str, Any]] = {}
        safe = {"roles", "channel_id", "owner_id", "afk_channel_id", "system_channel_id", "rules_channel_id", "public_updates_channel_id", "archived", "locked", "enabled", "type"}
        audit_changes = getattr(entry, "changes", None)
        before = getattr(audit_changes, "before", None)
        after = getattr(audit_changes, "after", None)
        for key in safe:
            before_value = _safe_change_value(key, getattr(before, key, None))
            after_value = _safe_change_value(key, getattr(after, key, None))
            if before_value is not None or after_value is not None:
                changes[key] = {"before": before_value, "after": after_value}
        self._emit(getattr(entry, "guild", None), "other", action_name, source="audit", actor_id=_id(getattr(entry, "user", None)), target_id=_id(getattr(entry, "target", None)), audit_entry_id=_id(entry), details={"reason_present": bool(getattr(entry, "reason", None)), "changes": changes})

    def record_member(self, event_type: str, before: Any, after: Any | None = None) -> None:
        member = after if after is not None else before
        details: dict[str, Any] = {}
        if after is not None:
            before_roles = {
                role_id
                for role_id in (_id(role) for role in getattr(before, "roles", ()))
                if role_id is not None
            }
            after_roles = {
                role_id
                for role_id in (_id(role) for role in getattr(after, "roles", ()))
                if role_id is not None
            }
            details = {
                "roles_added": sorted(after_roles - before_roles),
                "roles_removed": sorted(before_roles - after_roles),
                "timeout": _timestamp(getattr(after, "timed_out_until", None)),
                "pending": _boolean(getattr(after, "pending", None)),
            }
        self._emit(getattr(member, "guild", None), "member", event_type, target_id=_id(member), details=details)

    def record_raw_member_remove(self, payload: Any) -> None:
        target = getattr(payload, "user", None) or getattr(payload, "user_id", None)
        self._emit(
            getattr(payload, "guild_id", None),
            "member",
            "member_remove",
            target_id=_id(target),
        )

    def record_message(self, event_type: str, value: Any) -> None:
        guild = getattr(value, "guild", None)
        guild_id = _id(guild) or _id(getattr(value, "guild_id", None))
        if guild_id is None:
            return
        cached_message = getattr(value, "cached_message", None)
        metadata = cached_message if cached_message is not None else value
        author = getattr(metadata, "author", None)
        if getattr(author, "bot", False) is True or getattr(metadata, "webhook_id", None) is not None:
            return
        attachment_count = len(getattr(metadata, "attachments", ()) or ())
        self._record(ActivityEvent(guild_id, int(time.time()), "gateway", "message", event_type, actor_id=_id(author), channel_id=_id(getattr(value, "channel", None)) or _id(getattr(value, "channel_id", None)), message_id=_id(value) or _id(getattr(value, "message_id", None)), details={"attachment_count": attachment_count}))

    def record_bulk_message_delete(self, value: Any) -> None:
        message_ids = getattr(value, "message_ids", ()) or ()
        self._emit(
            getattr(value, "guild_id", None),
            "message",
            "message_bulk_delete",
            channel_id=_id(getattr(value, "channel_id", None)),
            details={"message_count": len(message_ids)},
        )

    def record_reaction(self, event_type: str, payload: Any) -> None:
        emoji = getattr(payload, "emoji", None)
        self._emit(getattr(payload, "guild_id", None), "message", event_type, actor_id=_id(getattr(payload, "user_id", None)), channel_id=_id(getattr(payload, "channel_id", None)), message_id=_id(getattr(payload, "message_id", None)), details={"emoji_id": _id(emoji)})

    def record_poll_vote(self, event_type: str, payload: Any) -> None:
        self._emit(getattr(payload, "guild_id", None), "message", event_type, actor_id=_id(getattr(payload, "user_id", None)), channel_id=_id(getattr(payload, "channel_id", None)), message_id=_id(getattr(payload, "message_id", None)), target_id=_id(getattr(payload, "answer_id", None)))

    def record_voice(self, member: Any, before: Any, after: Any) -> None:
        before_channel, after_channel = getattr(before, "channel", None), getattr(after, "channel", None)
        event_type = "move" if before_channel and after_channel and _id(before_channel) != _id(after_channel) else "join" if after_channel and not before_channel else "leave" if before_channel and not after_channel else "state_update"
        flags = ("self_mute", "self_deaf", "mute", "deaf", "self_stream", "self_video", "suppress")
        details = {
            key: after_value
            for key in flags
            if (before_value := _boolean(getattr(before, key, None))) is not None
            and (after_value := _boolean(getattr(after, key, None))) is not None
            and before_value != after_value
        }
        if before_channel: details["before_channel_id"] = _id(before_channel)
        self._emit(getattr(member, "guild", None), "voice", event_type, actor_id=_id(member), channel_id=_id(after_channel) or _id(before_channel), details=details)

    def record_thread(self, event_type: str, value: Any) -> None:
        self._emit(
            getattr(value, "guild", None) or getattr(value, "guild_id", None),
            "other",
            event_type,
            channel_id=_id(value) or _id(getattr(value, "thread_id", None)),
            details={
                "archived": _boolean(getattr(value, "archived", None)),
                "locked": _boolean(getattr(value, "locked", None)),
                "parent_id": _id(getattr(value, "parent", None)) or _id(getattr(value, "parent_id", None)),
            },
        )

    def record_scheduled_subscriber(self, event_type: str, event: Any, user: Any) -> None:
        self._emit(getattr(event, "guild", None) or getattr(event, "guild_id", None), "member", event_type, actor_id=_id(user), target_id=_id(event))

    def record_automod(self, execution: Any) -> None:
        action = getattr(execution, "action", None)
        self._emit(getattr(execution, "guild", None) or getattr(execution, "guild_id", None), "other", "automod_action", actor_id=_id(getattr(execution, "user_id", None)), target_id=_id(getattr(execution, "rule_id", None)), channel_id=_id(getattr(execution, "channel_id", None)), message_id=_id(getattr(execution, "message_id", None)), details={"action_type": _enum_name(getattr(action, "type", None))})
