import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.server_activity import (
    ActivityEvent,
    RETENTION_SECONDS,
    ServerActivityMonitor,
)


def obj(**values):
    return SimpleNamespace(**values)


class ServerActivityMonitorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "activity.sqlite3"
        self.monitor = ServerActivityMonitor(self.db_path, batch_max=3)

    async def asyncTearDown(self):
        await self.monitor.close()
        self.tempdir.cleanup()

    async def test_schema_wal_permissions_and_indexes(self):
        await self.monitor.start()
        with closing(sqlite3.connect(self.db_path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(server_activity_events)")}
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(server_activity_events)")}
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(journal_mode, "wal")
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)
        self.assertEqual(columns, {"id", "guild_id", "occurred_at", "source", "category", "event_type", "actor_id", "target_id", "channel_id", "message_id", "audit_entry_id", "details_json"})
        self.assertEqual(indexes, {"ux_server_activity_audit_entry", "ix_server_activity_guild_time", "ix_server_activity_guild_category_time"})

    def test_constructor_rejects_nonpositive_queue_and_batch_sizes(self):
        for keyword in ("queue_max", "batch_max"):
            for value in (0, -1, False, 1.5):
                with self.subTest(keyword=keyword, value=value):
                    with self.assertRaises(ValueError):
                        ServerActivityMonitor(self.db_path, **{keyword: value})

    async def test_start_is_idempotent(self):
        await asyncio.gather(self.monitor.start(), self.monitor.start())
        writer = self.monitor._writer
        await self.monitor.start()
        self.assertIs(self.monitor._writer, writer)

    async def test_initialization_failure_is_unavailable_and_log_is_generic(self):
        broken = ServerActivityMonitor(Path(self.tempdir.name))
        with patch("src.server_activity.logging.error") as log:
            await broken.start()
        self.assertFalse(broken.get_runtime_status().available)
        self.assertEqual(log.call_args.args, ("Server activity storage initialization failed.",))

    async def test_wal_result_is_verified(self):
        connection = unittest.mock.MagicMock()
        connection.execute.return_value.fetchone.return_value = ("delete",)
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        with patch.object(self.monitor, "_connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "WAL"):
                self.monitor._initialize()

    async def test_close_flushes_batch_when_stop_is_drained_with_events(self):
        await self.monitor.start()
        for number in range(2):
            self.monitor._record(ActivityEvent(1, number + 1, "gateway", "message", f"event-{number}"))
        await asyncio.wait_for(self.monitor.close(), timeout=2)
        with closing(sqlite3.connect(self.db_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM server_activity_events").fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(self.monitor._queue._unfinished_tasks, 0)

    async def test_batches_never_exceed_configured_maximum(self):
        batches = []
        original = self.monitor._write_batch

        def capture(batch, cleanup):
            batches.append(len(batch))
            original(batch, cleanup)

        with patch.object(self.monitor, "_write_batch", side_effect=capture):
            await self.monitor.start()
            for number in range(8):
                self.monitor._record(
                    ActivityEvent(1, number + 1, "gateway", "message", "event")
                )
            await self.monitor.close()

        self.assertEqual(sum(batches), 8)
        self.assertTrue(all(size <= 3 for size in batches))

    async def test_record_message_handles_10000_event_burst_without_drops(self):
        monitor = ServerActivityMonitor(
            self.db_path,
            queue_max=10_100,
            batch_max=100,
        )
        guild = obj(id=1)
        channel = obj(id=2)
        author = obj(id=3, bot=False)

        await monitor.start()
        for message_id in range(1, 10_001):
            monitor.record_message(
                "message_create",
                obj(
                    id=message_id,
                    guild=guild,
                    channel=channel,
                    author=author,
                ),
            )
        await monitor.close()

        self.assertEqual(monitor.get_runtime_status().dropped_event_count, 0)
        self.assertEqual(monitor._queue._unfinished_tasks, 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM server_activity_events"
            ).fetchone()[0]
        self.assertEqual(count, 10_000)

    async def test_queue_full_increments_dropped_count(self):
        monitor = ServerActivityMonitor(self.db_path, queue_max=1)
        monitor._accepting = True
        event = ActivityEvent(1, 1, "gateway", "other", "event")
        monitor._record(event)
        monitor._record(event)
        self.assertEqual(monitor.get_runtime_status().dropped_event_count, 1)

    async def test_record_makes_json_safe_copy_and_rejects_raw_objects(self):
        self.monitor._accepting = True
        details = {"nested": [{"value": "before"}]}
        self.monitor._record(
            ActivityEvent(1, 1, "gateway", "other", "event", details=details)
        )
        details["nested"][0]["value"] = "after"

        stored = self.monitor._queue.get_nowait()
        self.monitor._queue.task_done()
        self.assertEqual(stored.details, {"nested": [{"value": "before"}]})

        with self.assertRaisesRegex(TypeError, "JSON-safe primitives"):
            self.monitor._record(
                ActivityEvent(
                    1,
                    1,
                    "gateway",
                    "other",
                    "unsafe",
                    details={"raw": object()},
                )
            )
        self.assertTrue(self.monitor._queue.empty())

    async def test_duplicate_audit_entry_is_ignored(self):
        await self.monitor.start()
        for event_type in ("one", "two"):
            self.monitor._record(ActivityEvent(1, int(time.time()), "audit", "other", event_type, audit_entry_id=9))
        await self.monitor.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM server_activity_events").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_start_removes_events_older_than_retention(self):
        await self.monitor.start()
        await self.monitor.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("INSERT INTO server_activity_events(guild_id,occurred_at,source,category,event_type,details_json) VALUES(1,?,'gateway','other','old','{}')", (int(time.time()) - RETENTION_SECONDS - 1,))
        await self.monitor.start()
        with closing(sqlite3.connect(self.db_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM server_activity_events").fetchone()[0]
        self.assertEqual(count, 0)

    async def test_writer_failure_marks_unavailable_without_logging_event_data(self):
        await self.monitor.start()
        secret = "raw-secret-marker"
        with (
            patch.object(self.monitor, "_write_batch", side_effect=RuntimeError(secret)),
            patch("src.server_activity.logging.error") as log,
        ):
            self.monitor._record(ActivityEvent(1, 1, "gateway", "other", "event", details={"raw": secret}))
            await asyncio.wait_for(self.monitor._writer, timeout=2)
        self.assertFalse(self.monitor.get_runtime_status().available)
        self.assertFalse(self.monitor._accepting)
        self.assertEqual(log.call_args.args, ("Server activity writer failed.",))
        self.assertNotIn(secret, str(log.call_args))

    async def test_delete_orders_purge_after_inflight_batch_and_blocks_later_events(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.monitor._write_batch

        def blocked_write(batch, cleanup):
            entered.set()
            release.wait()
            original(batch, cleanup)

        with patch.object(self.monitor, "_write_batch", side_effect=blocked_write):
            await self.monitor.start()
            self.monitor._record(ActivityEvent(1, 1, "gateway", "other", "guild-1"))
            self.monitor._record(ActivityEvent(2, 2, "gateway", "other", "guild-2"))
            await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
            purge = asyncio.create_task(self.monitor.delete_guild(1))
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(purge, timeout=2)
            self.monitor._record(ActivityEvent(1, 3, "gateway", "other", "blocked"))
            await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT guild_id,event_type FROM server_activity_events ORDER BY id"
            ).fetchall()
        self.assertEqual(rows, [(2, "guild-2")])
        self.assertEqual(self.monitor._queue._unfinished_tasks, 0)

    async def test_failed_writer_restart_skips_stale_event_then_enables_guild(self):
        monitor = ServerActivityMonitor(self.db_path, batch_max=1)
        try:
            await asyncio.wait_for(monitor.start(), timeout=2)
            with patch.object(
                monitor, "_write_batch", side_effect=RuntimeError("private")
            ):
                monitor._record(ActivityEvent(2, 1, "gateway", "other", "fails"))
                monitor._record(ActivityEvent(1, 2, "gateway", "other", "stale"))
                await asyncio.wait_for(monitor._writer, timeout=2)

            with closing(sqlite3.connect(self.db_path)) as connection:
                connection.execute(
                    "INSERT INTO server_activity_events(guild_id,occurred_at,source,category,event_type,details_json) "
                    "VALUES(1,1,'gateway','other','existing','{}')"
                )
                connection.commit()
            await asyncio.wait_for(monitor.delete_guild(1), timeout=2)
            await asyncio.wait_for(monitor.enable_guild(1), timeout=2)
            self.assertIn(1, monitor._blocked_guild_ids)

            await asyncio.wait_for(monitor.start(), timeout=2)
            await asyncio.wait_for(monitor._pending_event(1).wait(), timeout=2)
            self.assertNotIn(1, monitor._blocked_guild_ids)
            monitor._record(ActivityEvent(1, 3, "gateway", "other", "fresh"))
            await asyncio.wait_for(monitor.close(), timeout=2)

            with closing(sqlite3.connect(self.db_path)) as connection:
                rows = connection.execute(
                    "SELECT guild_id,event_type FROM server_activity_events ORDER BY id"
                ).fetchall()
            self.assertEqual(rows, [(1, "fresh")])
            self.assertEqual(monitor._queue._unfinished_tasks, 0)
        finally:
            await monitor.close()

    async def test_delete_missing_db_and_stopped_writer_is_safe(self):
        await self.monitor.delete_guild(1)
        self.assertIn(1, self.monitor._blocked_guild_ids)

    async def test_delete_failure_is_generic_and_keeps_guild_blocked(self):
        await self.monitor.start()
        secret = "delete-secret"
        with (
            patch.object(self.monitor, "_delete_guild", side_effect=RuntimeError(secret)),
            patch("src.server_activity.logging.error") as log,
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await self.monitor.delete_guild(1)
        self.assertEqual(log.call_args.args, ("Server activity guild cleanup failed.",))
        self.assertNotIn(secret, str(log.call_args))
        self.assertIn(1, self.monitor._blocked_guild_ids)

    async def test_close_does_not_deadlock_when_full_queue_writer_fails(self):
        monitor = ServerActivityMonitor(self.db_path, queue_max=1, batch_max=1)
        writer_entered = threading.Event()
        release_writer = threading.Event()
        stop_put_started = asyncio.Event()
        original_put = monitor._queue.put

        def fail_write(batch, cleanup):
            writer_entered.set()
            release_writer.wait()
            raise RuntimeError("write failed")

        async def signal_then_put(item):
            stop_put_started.set()
            await original_put(item)

        close_task = None
        try:
            with (
                patch.object(monitor, "_write_batch", new=fail_write),
                patch.object(monitor._queue, "put", new=signal_then_put),
                patch("src.server_activity.logging.error") as log,
            ):
                await monitor.start()
                monitor._record(ActivityEvent(1, 1, "gateway", "other", "first"))
                await asyncio.wait_for(
                    asyncio.to_thread(writer_entered.wait), timeout=2
                )
                monitor._record(ActivityEvent(1, 2, "gateway", "other", "second"))

                close_task = asyncio.create_task(monitor.close())
                await asyncio.wait_for(stop_put_started.wait(), timeout=2)
                self.assertEqual(len(monitor._queue._putters), 1)
                release_writer.set()
                await asyncio.wait_for(close_task, timeout=2)

                self.assertEqual(
                    log.call_args.args, ("Server activity writer failed.",)
                )
                self.assertNotIn("write failed", str(log.call_args))

            self.assertFalse(monitor.get_runtime_status().available)
            self.assertIsNone(monitor._writer)
            self.assertEqual(len(monitor._queue._putters), 0)
            self.assertEqual(monitor._queue._unfinished_tasks, 0)
            self.assertTrue(monitor._queue.empty())
            self.assertFalse(
                any(
                    task.get_name() == "server-activity-stop-putter"
                    and not task.done()
                    for task in asyncio.all_tasks()
                )
            )

            await monitor.start()
            monitor._record(
                ActivityEvent(1, 3, "gateway", "other", "post-restart")
            )
            await asyncio.wait_for(monitor.close(), timeout=2)

            self.assertEqual(monitor._queue._unfinished_tasks, 0)
            self.assertTrue(monitor._queue.empty())
            with closing(sqlite3.connect(self.db_path)) as connection:
                event_types = connection.execute(
                    "SELECT event_type FROM server_activity_events ORDER BY id"
                ).fetchall()
            self.assertEqual(event_types, [("post-restart",)])
        finally:
            release_writer.set()
            if close_task is not None and not close_task.done():
                close_task.cancel()
                await asyncio.gather(close_task, return_exceptions=True)
            await monitor.close()

    async def test_summary_recent_and_filters_are_guild_scoped(self):
        await self.monitor.start()
        now = int(time.time())
        events = [
            ActivityEvent(1, now, "audit", "other", "admin"),
            ActivityEvent(1, now + 1, "gateway", "member", "join"),
            ActivityEvent(1, now + 2, "gateway", "message", "create"),
            ActivityEvent(1, now + 3, "gateway", "voice", "join"),
            ActivityEvent(1, now + 4, "gateway", "other", "thread"),
            ActivityEvent(2, now, "gateway", "message", "foreign"),
        ]
        for event in events:
            self.monitor._record(event)
        await self.monitor.close()

        summary = await self.monitor.get_summary(1, since=now)
        self.assertEqual((summary.total, summary.admin, summary.member, summary.message, summary.voice, summary.other), (5, 1, 1, 1, 1, 1))
        self.assertEqual([item.event_type for item in await self.monitor.get_recent_events(1, "admin")], ["admin"])
        self.assertEqual([item.event_type for item in await self.monitor.get_recent_events(1, "message")], ["create"])
        self.assertEqual(len(await self.monitor.get_recent_events(1, "invalid", limit=100)), 5)

    async def test_summary_and_recent_events_hide_expired_rows(self):
        now = 2_000_000_000
        with patch("src.server_activity.time.time", return_value=now):
            await self.monitor.start()
            await self.monitor.close()
            with closing(sqlite3.connect(self.db_path)) as connection:
                connection.executemany(
                    "INSERT INTO server_activity_events("
                    "guild_id,occurred_at,source,category,event_type,details_json"
                    ") VALUES(1,?,?,?,?, '{}')",
                    [
                        (now - 1, "gateway", "member", "recent"),
                        (now - RETENTION_SECONDS - 1, "gateway", "member", "expired"),
                    ],
                )
                connection.commit()

            summary = await self.monitor.get_summary(1, since=0)
            self.assertEqual((summary.total, summary.member), (1, 1))
            for filter_key in ("all", "member"):
                self.assertEqual(
                    [
                        event.event_type
                        for event in await self.monitor.get_recent_events(
                            1, filter_key
                        )
                    ],
                    ["recent"],
                )
            for filter_key in ("admin", "message", "voice"):
                self.assertEqual(
                    await self.monitor.get_recent_events(1, filter_key), []
                )

    async def test_normalizers_store_only_safe_metadata(self):
        await self.monitor.start()
        guild = obj(id=1)
        secret = "RAW-SECRET invite.gg/code https://attachment.invalid webhook-token"
        audit_name = "DISTINCTIVE-AUDIT-NAME"
        audit_nick = "DISTINCTIVE-AUDIT-NICK"
        member_name_before = "DISTINCTIVE-MEMBER-BEFORE"
        member_name_after = "DISTINCTIVE-MEMBER-AFTER"
        emoji_name = "DISTINCTIVE-EMOJI-NAME"
        audit = obj(
            id=10, guild=guild, action=obj(name="channel_update"), user=obj(id=2), target=obj(id=3),
            reason=secret,
            changes=obj(
                before=obj(name=audit_name, nick=audit_nick, roles=[obj(id=5)], owner_id=obj(id=8), topic=secret),
                after=obj(name=audit_name + "-AFTER", nick=audit_nick + "-AFTER", roles=[obj(id=6)], owner_id=obj(id=9), topic=secret),
            ),
        )
        self.monitor.record_audit(audit)
        self.monitor.record_member("member_update", obj(id=2, guild=guild, display_name=member_name_before, roles=[obj(id=5)], timed_out_until=None, pending=False), obj(id=2, guild=guild, display_name=member_name_after, roles=[obj(id=6)], timed_out_until=123, pending=True))
        self.monitor.record_message("message_create", obj(id=20, guild=guild, author=obj(id=2), channel=obj(id=4), content=secret, attachments=[obj(url=secret)], embeds=[secret]))
        self.monitor.record_reaction("reaction_add", obj(guild_id=1, user_id=2, channel_id=4, message_id=20, emoji=obj(id=7, name=emoji_name)))
        self.monitor.record_poll_vote("poll_vote_add", obj(guild_id=1, user_id=2, channel_id=4, message_id=20, answer_id=8, poll=secret))
        self.monitor.record_voice(obj(id=2, guild=guild), obj(channel=None, self_mute=False), obj(channel=obj(id=4), self_mute=True))
        self.monitor.record_thread("thread_create", obj(id=30, guild=guild, archived=False, locked=False, name=secret))
        self.monitor.record_scheduled_subscriber("scheduled_add", obj(id=40, guild=guild, name=secret), obj(id=2))
        self.monitor.record_automod(obj(guild_id=1, user_id=2, rule_id=50, channel_id=4, message_id=20, action=obj(type=obj(name="block_message")), matched_content=secret, matched_keyword=secret))
        await self.monitor.close()

        database_bytes = self.db_path.read_bytes()
        self.assertNotIn(secret.encode(), database_bytes)
        for minimized_value in (
            audit_name,
            audit_nick,
            member_name_before,
            member_name_after,
            emoji_name,
        ):
            self.assertNotIn(minimized_value.encode(), database_bytes)
        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute("SELECT event_type,actor_id,target_id,channel_id,message_id,audit_entry_id,details_json FROM server_activity_events ORDER BY id").fetchall()
        self.assertEqual(len(rows), 9)
        audit_details = json.loads(rows[0][6])
        self.assertTrue(audit_details["reason_present"])
        self.assertEqual(audit_details["changes"]["roles"], {"before": [5], "after": [6]})
        self.assertEqual(audit_details["changes"]["owner_id"], {"before": 8, "after": 9})
        member_details = json.loads(rows[1][6])
        self.assertEqual(member_details, {
            "roles_added": [6],
            "roles_removed": [5],
            "timeout": 123,
            "pending": True,
        })
        self.assertEqual(json.loads(rows[2][6]), {"attachment_count": 1})
        self.assertEqual(json.loads(rows[3][6]), {"emoji_id": 7})
        self.assertEqual(json.loads(rows[-1][6]), {"action_type": "block_message"})
        decoded_details = " ".join(row[6] for row in rows)
        for minimized_value in (
            audit_name,
            audit_nick,
            member_name_before,
            member_name_after,
            emoji_name,
        ):
            self.assertNotIn(minimized_value, decoded_details)

    async def test_message_normalizer_ignores_dm_and_handles_raw_payload(self):
        await self.monitor.start()
        self.monitor.record_message(
            "message_create",
            obj(id=1, guild=None, channel=obj(id=2), content="private"),
        )
        self.monitor.record_message(
            "message_delete",
            obj(guild_id=3, channel_id=4, message_id=5, content="secret"),
        )
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT guild_id,actor_id,channel_id,message_id,details_json "
                "FROM server_activity_events"
            ).fetchall()
        self.assertEqual(rows, [(3, None, 4, 5, '{"attachment_count":0}')])

    async def test_raw_message_ignores_cached_bot_and_webhook_metadata(self):
        await self.monitor.start()
        message_id = 1
        for event_type in ("message_edit", "message_delete"):
            for cached_message in (
                obj(author=obj(id=6, bot=True), attachments=[]),
                obj(author=obj(id=7, bot=False), webhook_id=8, attachments=[]),
            ):
                self.monitor.record_message(
                    event_type,
                    obj(
                        guild_id=3,
                        channel_id=4,
                        message_id=message_id,
                        cached_message=cached_message,
                    ),
                )
                message_id += 1
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM server_activity_events"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_raw_message_uses_cached_author_and_attachments(self):
        await self.monitor.start()
        self.monitor.record_message(
            "message_delete",
            obj(
                guild_id=3,
                channel_id=4,
                message_id=5,
                cached_message=obj(
                    author=obj(id=6),
                    attachments=[obj(id=7), obj(id=8)],
                ),
            ),
        )
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT actor_id,details_json FROM server_activity_events"
            ).fetchone()
        self.assertEqual(row, (6, '{"attachment_count":2}'))

    async def test_bulk_message_delete_stores_count_without_ids_or_content(self):
        await self.monitor.start()
        self.monitor.record_bulk_message_delete(
            obj(
                guild_id=1,
                channel_id=2,
                message_ids={3, 4, 5},
                cached_messages=[obj(id=3, content="do-not-store")],
            )
        )
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT event_type,actor_id,target_id,channel_id,message_id,details_json "
                "FROM server_activity_events"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:5], ("message_bulk_delete", None, None, 2, None))
        self.assertEqual(json.loads(rows[0][5]), {"message_count": 3})
        self.assertNotIn(b"do-not-store", self.db_path.read_bytes())

    async def test_member_join_remove_and_update_metadata(self):
        guild = obj(id=1)
        before = obj(
            id=2,
            guild=guild,
            display_name="before",
            roles=[obj(id=10), obj(id=11)],
            timed_out_until=None,
            pending=False,
        )
        after = obj(
            id=2,
            guild=guild,
            display_name="after",
            roles=[obj(id=11), obj(id=12)],
            timed_out_until=123,
            pending=True,
        )
        await self.monitor.start()
        self.monitor.record_member("member_join", after)
        self.monitor.record_member("member_remove", before)
        self.monitor.record_member("member_update", before, after)
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT event_type,target_id,details_json "
                "FROM server_activity_events ORDER BY id"
            ).fetchall()
        self.assertEqual([row[:2] for row in rows], [
            ("member_join", 2),
            ("member_remove", 2),
            ("member_update", 2),
        ])
        self.assertEqual(json.loads(rows[2][2]), {
            "roles_added": [12],
            "roles_removed": [10],
            "timeout": 123,
            "pending": True,
        })

    async def test_reaction_clear_poll_and_scheduled_normalizers(self):
        guild = obj(id=1)
        await self.monitor.start()
        self.monitor.record_reaction(
            "reaction_clear",
            obj(guild_id=1, channel_id=2, message_id=3, emoji=None),
        )
        self.monitor.record_reaction(
            "reaction_clear_emoji",
            obj(
                guild_id=1,
                channel_id=2,
                message_id=3,
                emoji=obj(id=4, name="x" * 150),
            ),
        )
        self.monitor.record_poll_vote(
            "poll_vote_remove",
            obj(guild_id=1, user_id=5, channel_id=2, message_id=3, answer_id=6),
        )
        self.monitor.record_scheduled_subscriber(
            "scheduled_remove", obj(id=7, guild=guild), obj(id=5)
        )
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT event_type,actor_id,target_id,details_json "
                "FROM server_activity_events ORDER BY id"
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [
            "reaction_clear",
            "reaction_clear_emoji",
            "poll_vote_remove",
            "scheduled_remove",
        ])
        self.assertEqual(json.loads(rows[1][3]), {"emoji_id": 4})
        self.assertEqual(rows[2][1:3], (5, 6))
        self.assertEqual(rows[3][1:3], (5, 7))

    async def test_raw_member_remove_normalizer(self):
        await self.monitor.start()
        self.monitor.record_raw_member_remove(obj(guild_id=1, user=obj(id=2)))
        self.monitor.record_raw_member_remove(obj(guild_id=1, user_id=3))
        self.monitor.record_raw_member_remove(obj(guild_id=None, user=obj(id=4)))
        self.monitor.record_raw_member_remove(obj(guild_id="invalid", user_id=5))
        self.monitor.record_raw_member_remove(obj(user_id=6))
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT event_type,target_id,details_json "
                "FROM server_activity_events ORDER BY id"
            ).fetchall()
        self.assertEqual(rows, [
            ("member_remove", 2, "{}"),
            ("member_remove", 3, "{}"),
        ])

    async def test_voice_thread_and_automod_normalizers(self):
        guild = obj(id=1)
        member = obj(id=2, guild=guild)
        empty = obj(
            channel=None,
            self_mute=False,
            self_deaf=False,
            mute=False,
            deaf=False,
            self_stream=False,
            self_video=False,
            suppress=False,
        )
        joined = obj(**{**vars(empty), "channel": obj(id=3), "self_mute": True})
        moved = obj(**{**vars(joined), "channel": obj(id=4)})
        await self.monitor.start()
        self.monitor.record_voice(member, empty, joined)
        self.monitor.record_voice(member, joined, moved)
        self.monitor.record_voice(member, moved, empty)
        self.monitor.record_voice(member, moved, obj(**{**vars(moved), "deaf": True}))
        self.monitor.record_thread(
            "thread_update", obj(id=8, guild=guild, archived=True, locked=False)
        )
        self.monitor.record_thread(
            "thread_delete", obj(thread_id=9, guild_id=1)
        )
        self.monitor.record_automod(
            obj(
                guild_id=1,
                user_id=2,
                rule_id=10,
                channel_id=4,
                message_id=11,
                action=obj(type=7),
                matched_content="do-not-store",
                matched_keyword="do-not-store",
            )
        )
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT event_type,channel_id,details_json "
                "FROM server_activity_events ORDER BY id"
            ).fetchall()
        self.assertEqual([row[0] for row in rows[:4]], [
            "join", "move", "leave", "state_update"
        ])
        self.assertEqual(json.loads(rows[0][2]), {"self_mute": True})
        self.assertEqual(json.loads(rows[1][2]), {"before_channel_id": 3})
        self.assertEqual(rows[2][1], 4)
        self.assertEqual(json.loads(rows[4][2]), {
            "archived": True,
            "locked": False,
            "parent_id": None,
        })
        self.assertEqual(rows[5][1], 9)
        self.assertEqual(json.loads(rows[6][2]), {"action_type": "7"})
        self.assertNotIn(b"do-not-store", self.db_path.read_bytes())

    async def test_thread_stores_parent_id_when_present(self):
        await self.monitor.start()
        self.monitor.record_thread(
            "thread_create",
            obj(id=8, guild=obj(id=1), parent_id=9),
        )
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            details_json = connection.execute(
                "SELECT details_json FROM server_activity_events"
            ).fetchone()[0]
        self.assertEqual(json.loads(details_json), {
            "archived": None,
            "locked": None,
            "parent_id": 9,
        })

    async def test_audit_unknown_action_and_safe_change_allowlist(self):
        secret = "invite-code-and-webhook-token"
        await self.monitor.start()
        self.monitor.record_audit(obj(
            id=9,
            guild=obj(id=1),
            action=obj(value=987),
            user=obj(id=2),
            target=obj(id=3),
            reason=secret,
            changes=obj(
                before=obj(owner_id=obj(id=4), enabled=False, topic=secret),
                after=obj(owner_id=obj(id=5), enabled=True, topic=secret),
            ),
        ))
        await self.monitor.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            event_type, details_json = connection.execute(
                "SELECT event_type,details_json FROM server_activity_events"
            ).fetchone()
        self.assertEqual(event_type, "987")
        self.assertEqual(json.loads(details_json), {
            "reason_present": True,
            "changes": {
                "enabled": {"before": False, "after": True},
                "owner_id": {"before": 4, "after": 5},
            },
        })
        self.assertNotIn(secret.encode(), self.db_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
