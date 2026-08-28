from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import math
import os
from pathlib import Path
import sqlite3
import struct
import time
from typing import Awaitable, Callable

from src.ai_client import AIClient

DEFAULT_DB_PATH = Path("/app/data/semantic_memory.sqlite3")
SCHEMA_VERSION = "1"
EMBEDDING_BATCH_SIZE = 16
SEARCH_CANDIDATE_LIMIT = 25
SEARCH_RESULT_LIMIT = 5
MAX_MEMORY_CONTENT_CHARS = 1000
_RETRY_DELAYS = (5.0, 10.0, 20.0, 40.0, 60.0, 300.0, 900.0, 3600.0)


class SemanticMemoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryVerification:
    status: str
    content: str = ""
    author_name: str = ""


@dataclass(frozen=True, slots=True)
class MemoryScope:
    channel_id: int
    verify_message: Callable[[int], Awaitable[MemoryVerification]]


@dataclass(frozen=True, slots=True)
class _Candidate:
    message_id: int
    author_name: str
    source_hash: str
    vector: tuple[float, ...]
    created_at: int
    similarity: float


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _normalize_vector(vector: tuple[float, ...]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise SemanticMemoryError("invalid embedding vector")
    return tuple(value / norm for value in vector)


def _pack_vector(vector: tuple[float, ...]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes, dimensions: int) -> tuple[float, ...]:
    expected_size = dimensions * 4
    if len(blob) != expected_size:
        raise SemanticMemoryError("invalid stored embedding")
    try:
        result = tuple(float(value) for value in struct.unpack(f"<{dimensions}f", blob))
    except struct.error as exc:
        raise SemanticMemoryError("invalid stored embedding") from exc
    if any(not math.isfinite(value) for value in result):
        raise SemanticMemoryError("invalid stored embedding")
    return result


class SemanticMemory:
    def __init__(
        self,
        ai_client: AIClient,
        *,
        embedding_model: str,
        embedding_dimensions: int,
        db_path: Path = DEFAULT_DB_PATH,
    ) -> None:
        if not embedding_model.strip():
            raise ValueError("embedding_model must be non-empty")
        if type(embedding_dimensions) is not int or embedding_dimensions <= 0:
            raise ValueError("embedding_dimensions must be positive")
        self.ai_client = ai_client
        self.embedding_model = embedding_model.strip()
        self.embedding_dimensions = embedding_dimensions
        self.db_path = Path(db_path)
        self.available = False
        self._worker_task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._mutation_lock = asyncio.Lock()
        self._closing = False

    async def start(self) -> None:
        if self.available:
            return
        try:
            await asyncio.to_thread(self._initialize_db)
        except Exception:
            logging.error("Semantic memory initialization failed.")
            self.available = False
            return
        self.available = True
        self._closing = False
        self._worker_task = asyncio.create_task(
            self._guard_worker(),
            name="semantic-memory-worker",
        )
        self._wake_event.set()

    async def close(self) -> None:
        self._closing = True
        self.available = False
        self._wake_event.set()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.available = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise SemanticMemoryError("semantic memory WAL mode unavailable")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_memory_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_messages ("
                "message_id INTEGER PRIMARY KEY, "
                "guild_id INTEGER NOT NULL, "
                "channel_id INTEGER NOT NULL, "
                "author_name TEXT NOT NULL, "
                "source_hash TEXT NOT NULL, "
                "pending_text TEXT, "
                "embedding BLOB, "
                "state TEXT NOT NULL CHECK(state IN ('pending','ready')), "
                "created_at INTEGER NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_embedding_failures ("
                "message_id INTEGER PRIMARY KEY, "
                "source_hash TEXT NOT NULL, "
                "failure_count INTEGER NOT NULL, "
                "retry_at REAL NOT NULL, "
                "FOREIGN KEY(message_id) REFERENCES semantic_messages(message_id) "
                "ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_semantic_failure_retry "
                "ON semantic_embedding_failures(retry_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_semantic_channel_state "
                "ON semantic_messages(channel_id, state)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_semantic_guild "
                "ON semantic_messages(guild_id)"
            )
            existing = dict(
                connection.execute(
                    "SELECT key, value FROM semantic_memory_meta"
                ).fetchall()
            )
            expected = {
                "schema_version": SCHEMA_VERSION,
                "embedding_model": self.embedding_model,
                "embedding_dimensions": str(self.embedding_dimensions),
            }
            if existing:
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise SemanticMemoryError("semantic memory metadata mismatch")
            else:
                connection.executemany(
                    "INSERT INTO semantic_memory_meta(key, value) VALUES(?, ?)",
                    list(expected.items()),
                )
        os.chmod(self.db_path, 0o600)

    async def capture_message(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_name: str,
        content: str,
        created_at: int | None = None,
    ) -> None:
        if not self.available:
            return
        normalized = content.strip()
        if not normalized:
            return
        timestamp = int(time.time()) if created_at is None else int(created_at)
        async with self._mutation_lock:
            await asyncio.to_thread(
                self._capture_sync,
                message_id,
                guild_id,
                channel_id,
                author_name[:80] or "unknown",
                normalized,
                timestamp,
            )
        self._wake_event.set()

    def _capture_sync(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_name: str,
        content: str,
        created_at: int,
    ) -> None:
        now = int(time.time())
        source_hash = _content_hash(content)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO semantic_messages("
                "message_id,guild_id,channel_id,author_name,source_hash,pending_text,"
                "embedding,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,NULL,'pending',?,?) "
                "ON CONFLICT(message_id) DO UPDATE SET "
                "guild_id=excluded.guild_id, channel_id=excluded.channel_id, "
                "author_name=excluded.author_name, source_hash=excluded.source_hash, "
                "pending_text=excluded.pending_text, embedding=NULL, state='pending', "
                "updated_at=excluded.updated_at",
                (
                    message_id,
                    guild_id,
                    channel_id,
                    author_name,
                    source_hash,
                    content,
                    created_at,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM semantic_embedding_failures WHERE message_id=?",
                (message_id,),
            )

    async def contains_message(self, message_id: int) -> bool:
        if not self.available:
            return False
        return await asyncio.to_thread(self._contains_sync, message_id)

    def _contains_sync(self, message_id: int) -> bool:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT 1 FROM semantic_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return row is not None

    async def update_existing_message(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_name: str,
        content: str,
    ) -> bool:
        if not self.available:
            return False
        normalized = content.strip()
        async with self._mutation_lock:
            updated = await asyncio.to_thread(
                self._update_existing_sync,
                message_id,
                guild_id,
                channel_id,
                author_name[:80] or "unknown",
                normalized,
            )
        if updated and normalized:
            self._wake_event.set()
        return updated

    def _update_existing_sync(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_name: str,
        content: str,
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM semantic_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if exists is None:
                return False
            if not content:
                connection.execute(
                    "DELETE FROM semantic_messages WHERE message_id=?",
                    (message_id,),
                )
                return True
            connection.execute(
                "UPDATE semantic_messages SET guild_id=?, channel_id=?, author_name=?, "
                "source_hash=?, pending_text=?, embedding=NULL, state='pending', updated_at=? "
                "WHERE message_id=?",
                (
                    guild_id,
                    channel_id,
                    author_name,
                    _content_hash(content),
                    content,
                    int(time.time()),
                    message_id,
                ),
            )
            connection.execute(
                "DELETE FROM semantic_embedding_failures WHERE message_id=?",
                (message_id,),
            )
            return True

    async def delete_message(self, message_id: int) -> None:
        if self.db_path.is_file():
            async with self._mutation_lock:
                await asyncio.to_thread(self._delete_message_sync, message_id)

    def _delete_message_sync(self, message_id: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM semantic_messages WHERE message_id=?",
                (message_id,),
            )

    async def delete_messages(self, message_ids: set[int] | list[int]) -> None:
        if not message_ids or not self.db_path.is_file():
            return
        async with self._mutation_lock:
            await asyncio.to_thread(self._delete_messages_sync, list(message_ids))

    def _delete_messages_sync(self, message_ids: list[int]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "DELETE FROM semantic_messages WHERE message_id=?",
                [(message_id,) for message_id in message_ids],
            )

    async def delete_channel(self, channel_id: int) -> None:
        if self.db_path.is_file():
            async with self._mutation_lock:
                await asyncio.to_thread(
                    self._delete_scope_sync,
                    "channel_id",
                    channel_id,
                )

    async def delete_guild(self, guild_id: int) -> None:
        if self.db_path.is_file():
            async with self._mutation_lock:
                await asyncio.to_thread(
                    self._delete_scope_sync,
                    "guild_id",
                    guild_id,
                )

    def _delete_scope_sync(self, column: str, value: int) -> None:
        if column not in {"channel_id", "guild_id"}:
            raise ValueError("invalid semantic memory scope")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"DELETE FROM semantic_messages WHERE {column}=?",
                (value,),
            )

    async def _guard_worker(self) -> None:
        try:
            await self._worker_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.available = False
            logging.exception("Semantic memory background worker stopped unexpectedly.")

    async def _worker_loop(self) -> None:
        while not self._closing:
            self._wake_event.clear()
            rows = await asyncio.to_thread(self._load_pending_sync)
            if not rows:
                retry_delay = await asyncio.to_thread(self._next_retry_delay_sync)
                try:
                    if retry_delay is None:
                        await self._wake_event.wait()
                    else:
                        await asyncio.wait_for(
                            self._wake_event.wait(),
                            timeout=max(0.01, retry_delay),
                        )
                except TimeoutError:
                    pass
                continue

            texts = [row["pending_text"] for row in rows]
            try:
                vectors = await self.ai_client.embed(
                    texts,
                    model=self.embedding_model,
                    dimensions=self.embedding_dimensions,
                )
                normalized = [_normalize_vector(vector) for vector in vectors]
                await asyncio.to_thread(self._apply_embeddings_sync, rows, normalized)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.warning("Embedding batch failed; probing the first row.")
                first_row = rows[0]
                try:
                    vector = (
                        await self.ai_client.embed(
                            [first_row["pending_text"]],
                            model=self.embedding_model,
                            dimensions=self.embedding_dimensions,
                        )
                    )[0]
                    normalized = _normalize_vector(vector)
                    await asyncio.to_thread(
                        self._apply_embeddings_sync,
                        [first_row],
                        [normalized],
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.error("Embedding row unavailable; retry deferred.")

                delay = await asyncio.to_thread(self._defer_pending_sync, first_row)
                if len(rows) > 1:
                    continue
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=max(0.01, delay),
                    )
                except TimeoutError:
                    pass

    def _load_pending_sync(self) -> list[sqlite3.Row]:
        now = time.time()
        with closing(self._connect()) as connection, connection:
            return connection.execute(
                "SELECT m.message_id, m.source_hash, m.pending_text "
                "FROM semantic_messages AS m "
                "LEFT JOIN semantic_embedding_failures AS f "
                "ON f.message_id=m.message_id AND f.source_hash=m.source_hash "
                "WHERE m.state='pending' AND m.pending_text IS NOT NULL "
                "AND (f.message_id IS NULL OR f.retry_at<=?) "
                "ORDER BY CASE WHEN f.message_id IS NULL THEN 0 ELSE 1 END, "
                "m.updated_at, m.message_id LIMIT ?",
                (now, EMBEDDING_BATCH_SIZE),
            ).fetchall()

    def _next_retry_delay_sync(self) -> float | None:
        now = time.time()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT MIN(f.retry_at) "
                "FROM semantic_embedding_failures AS f "
                "JOIN semantic_messages AS m "
                "ON m.message_id=f.message_id AND m.source_hash=f.source_hash "
                "WHERE m.state='pending' AND m.pending_text IS NOT NULL "
                "AND f.retry_at>?",
                (now,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return max(0.0, float(row[0]) - now)

    def _defer_pending_sync(self, row: sqlite3.Row) -> float:
        message_id = int(row["message_id"])
        source_hash = str(row["source_hash"])
        now = time.time()
        with closing(self._connect()) as connection, connection:
            current = connection.execute(
                "SELECT source_hash, state FROM semantic_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if (
                current is None
                or current["source_hash"] != source_hash
                or current["state"] != "pending"
            ):
                return 0.01

            failure = connection.execute(
                "SELECT failure_count FROM semantic_embedding_failures "
                "WHERE message_id=? AND source_hash=?",
                (message_id, source_hash),
            ).fetchone()
            failure_count = 1 if failure is None else int(failure[0]) + 1
            delay = _RETRY_DELAYS[min(failure_count - 1, len(_RETRY_DELAYS) - 1)]
            connection.execute(
                "INSERT INTO semantic_embedding_failures("
                "message_id,source_hash,failure_count,retry_at) VALUES(?,?,?,?) "
                "ON CONFLICT(message_id) DO UPDATE SET "
                "source_hash=excluded.source_hash, "
                "failure_count=excluded.failure_count, retry_at=excluded.retry_at",
                (message_id, source_hash, failure_count, now + delay),
            )
        return delay

    def _apply_embeddings_sync(
        self,
        rows: list[sqlite3.Row],
        vectors: list[tuple[float, ...]],
    ) -> None:
        if len(rows) != len(vectors):
            raise SemanticMemoryError("embedding batch size mismatch")
        now = int(time.time())
        with closing(self._connect()) as connection, connection:
            for row, vector in zip(rows, vectors):
                cursor = connection.execute(
                    "UPDATE semantic_messages SET embedding=?, pending_text=NULL, "
                    "state='ready', updated_at=? WHERE message_id=? AND source_hash=? "
                    "AND state='pending'",
                    (
                        _pack_vector(vector),
                        now,
                        row["message_id"],
                        row["source_hash"],
                    ),
                )
                if cursor.rowcount == 1:
                    connection.execute(
                        "DELETE FROM semantic_embedding_failures "
                        "WHERE message_id=? AND source_hash=?",
                        (row["message_id"], row["source_hash"]),
                    )

    async def search(
        self,
        query: str,
        scope: MemoryScope,
    ) -> list[dict[str, object]]:
        if not self.available:
            raise SemanticMemoryError("semantic memory unavailable")
        normalized_query = query.strip()
        if not normalized_query:
            return []
        query_vector = _normalize_vector(
            (
                await self.ai_client.embed(
                    [normalized_query],
                    model=self.embedding_model,
                    dimensions=self.embedding_dimensions,
                )
            )[0]
        )
        candidates = await asyncio.to_thread(
            self._rank_candidates_sync,
            scope.channel_id,
            query_vector,
        )

        verified: list[dict[str, object]] = []
        for candidate in candidates:
            verification = await scope.verify_message(candidate.message_id)
            if verification.status == "deleted":
                await self.delete_message(candidate.message_id)
                continue
            if verification.status != "current":
                continue

            content = verification.content.strip()
            if not content:
                await self.delete_message(candidate.message_id)
                continue
            author_name = verification.author_name[:80] or candidate.author_name
            source_hash = _content_hash(content)
            vector = candidate.vector
            if source_hash != candidate.source_hash:
                vector = _normalize_vector(
                    (
                        await self.ai_client.embed(
                            [content],
                            model=self.embedding_model,
                            dimensions=self.embedding_dimensions,
                        )
                    )[0]
                )
                updated = await asyncio.to_thread(
                    self._update_verified_sync,
                    candidate.message_id,
                    candidate.source_hash,
                    source_hash,
                    author_name,
                    vector,
                )
                if not updated:
                    continue
            elif author_name != candidate.author_name:
                await asyncio.to_thread(
                    self._update_author_sync,
                    candidate.message_id,
                    author_name,
                )

            similarity = sum(a * b for a, b in zip(query_vector, vector))
            verified.append(
                {
                    "author": author_name,
                    "content": content[:MAX_MEMORY_CONTENT_CHARS],
                    "created_at": datetime.fromtimestamp(
                        candidate.created_at, timezone.utc
                    ).isoformat(),
                    "similarity": round(float(similarity), 4),
                }
            )

        verified.sort(key=lambda item: float(item["similarity"]), reverse=True)
        return verified[:SEARCH_RESULT_LIMIT]

    def _rank_candidates_sync(
        self,
        channel_id: int,
        query_vector: tuple[float, ...],
    ) -> list[_Candidate]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT message_id, author_name, source_hash, embedding, created_at "
                "FROM semantic_messages WHERE channel_id=? AND state='ready' "
                "AND embedding IS NOT NULL",
                (channel_id,),
            ).fetchall()
        candidates = []
        for row in rows:
            vector = _unpack_vector(row["embedding"], self.embedding_dimensions)
            similarity = sum(a * b for a, b in zip(query_vector, vector))
            candidates.append(
                _Candidate(
                    message_id=row["message_id"],
                    author_name=row["author_name"],
                    source_hash=row["source_hash"],
                    vector=vector,
                    created_at=row["created_at"],
                    similarity=similarity,
                )
            )
        candidates.sort(key=lambda candidate: candidate.similarity, reverse=True)
        return candidates[:SEARCH_CANDIDATE_LIMIT]

    def _update_verified_sync(
        self,
        message_id: int,
        old_hash: str,
        new_hash: str,
        author_name: str,
        vector: tuple[float, ...],
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE semantic_messages SET author_name=?, source_hash=?, embedding=?, "
                "pending_text=NULL, state='ready', updated_at=? "
                "WHERE message_id=? AND source_hash=?",
                (
                    author_name,
                    new_hash,
                    _pack_vector(vector),
                    int(time.time()),
                    message_id,
                    old_hash,
                ),
            )
            return cursor.rowcount == 1

    def _update_author_sync(self, message_id: int, author_name: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE semantic_messages SET author_name=?, updated_at=? WHERE message_id=?",
                (author_name, int(time.time()), message_id),
            )
