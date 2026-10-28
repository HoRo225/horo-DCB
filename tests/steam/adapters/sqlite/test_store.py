import json
import sqlite3
import unittest

from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.steam.adapters.sqlite.store import SqliteSteamStore
from src.steam.app.ports import StoreError
from src.steam.domain.offers import ANNOUNCE, OFFER_ENDED, Announcement
from tests.support.steam import offer

CH = 10


class SqliteSteamStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.store = SqliteSteamStore(self.db)
        self.records = OperationStore(self.db)

    def queue(self, effect_id: str, app_id: int, *, status: str = "pending", error: str | None = None) -> None:
        self.store.queue(effect_id, CH, Announcement(offer(app_id), (3,)).payload(), f"nonce-{effect_id}")
        if status != "pending":
            with transaction(self.db):
                self.records.update_effect(effect_id, status if status != "done" else "unknown", error=error)
                if status == "done":
                    self.records.update_effect(effect_id, "done")

    def statuses(self) -> dict[str, tuple[str, str | None]]:
        return {effect.effect_id: (effect.status, effect.error) for effect in self.records.effects(kind=ANNOUNCE)}

    def reject(self, table: str) -> None:
        self.db.execute(f"CREATE TRIGGER reject BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'disk full'); END")

    def test_settings_start_from_their_defaults_and_persist(self) -> None:
        self.assertEqual((self.store.enabled(), self.store.channel_id(), self.store.role_ids()), (False, None, []))
        self.assertTrue(self.store.set_enabled(True, operation_id=None))
        self.assertTrue(self.store.bind_channel(CH, operation_id=None))
        self.assertTrue(self.store.set_roles([1, 2], operation_id=None))
        self.store.keep_roles([2])
        again = SqliteSteamStore(self.db)
        self.assertEqual((again.enabled(), again.channel_id(), again.role_ids()), (True, CH, [2]))

    def test_operation_receipts_replay_without_overwriting_newer_settings(self) -> None:
        self.assertTrue(self.store.bind_channel(CH, operation_id="channel"))
        self.store.bind_channel(11, operation_id=None)
        self.assertFalse(self.store.bind_channel(CH, operation_id="channel"))
        self.assertEqual(self.store.channel_id(), 11)
        with self.assertRaises(ValueError):
            self.store.bind_channel(11, operation_id="channel")
        self.assertEqual(self.records.get("channel").kind, "steam.bind_channel")
        self.store.set_roles([4], operation_id="roles")
        self.store.set_enabled(True, operation_id="toggle")
        self.assertEqual((self.records.get("roles").kind, self.records.get("roles").inputs), ("steam.set_roles", {"role_ids": [4]}))
        self.assertEqual((self.records.get("toggle").kind, self.records.get("toggle").inputs), ("steam.set_enabled", {"enabled": True}))

    def test_failed_receipt_keeps_the_previous_setting(self) -> None:
        self.reject("operations")
        with self.assertRaises(StoreError) as caught:
            self.store.set_enabled(True, operation_id="failed")
        self.assertIsInstance(caught.exception, sqlite3.Error)
        self.assertFalse(self.store.enabled())

    def test_queued_announcement_is_saved_as_given(self) -> None:
        payload = Announcement(offer(7), (3,)).payload()
        self.store.queue("steam:7:x", CH, payload, "nonce")
        effect = self.store.announcement("steam:7:x")
        self.assertEqual((effect.kind, effect.status, effect.channel_id, effect.nonce, effect.payload),
                         (ANNOUNCE, "pending", CH, "nonce", json.loads(json.dumps(payload))))
        self.assertEqual(self.store.announcements("pending"), [effect])

    def test_other_effect_kinds_are_not_announcements(self) -> None:
        with transaction(self.db):
            self.records.create_effect("other", "voice.create", CH, {})
        self.assertIsNone(self.store.announcement("other"))
        self.assertEqual(self.store.announcements("pending"), [])

    def test_delivery_moves_through_unknown_to_done_and_suppresses_the_offer(self) -> None:
        self.queue("e", 7)
        effect = self.store.announcement("e")
        self.store.begin_delivery(effect)
        self.assertEqual(self.statuses(), {"e": ("unknown", None)})
        self.store.finish_delivery(effect, 555)
        self.assertEqual(self.store.announcement("e").message_id, 555)
        self.assertEqual((self.statuses(), self.store.posted()), ({"e": ("done", None)}, {7}))

    def test_retried_delivery_restarts_from_pending(self) -> None:
        for status in ("unknown", "failed"):
            with self.subTest(status=status):
                self.queue(status, 7, status=status)
                self.store.begin_delivery(self.store.announcement(status))
                self.assertEqual(self.store.announcement(status).status, "unknown")

    def test_failed_delivery_records_the_error(self) -> None:
        self.queue("e", 7)
        self.store.begin_delivery(self.store.announcement("e"))
        self.store.fail_delivery("e", "failed", "Forbidden")
        self.assertEqual(self.statuses(), {"e": ("failed", "Forbidden")})

    def test_delivery_and_suppression_commit_together(self) -> None:
        self.queue("e", 7)
        effect = self.store.announcement("e")
        self.store.begin_delivery(effect)
        self.reject("steam_posted")
        with self.assertRaises(StoreError):
            self.store.finish_delivery(effect, 555)
        saved = self.store.announcement("e")
        self.assertEqual((saved.status, saved.message_id, self.store.posted()), ("unknown", None, set()))

    def test_settle_suppresses_the_offer_once(self) -> None:
        self.queue("e", 7)
        effect = self.store.announcement("e")
        with transaction(self.db):
            self.store.settle(effect)
            self.store.settle(effect)
        self.assertEqual(self.store.posted(), {7})

    def test_retire_forgets_ended_offers_and_ends_their_waiting_announcements(self) -> None:
        for app_id in (1, 2):
            self.queue(f"done{app_id}", app_id, status="done")
        with transaction(self.db):
            for app_id in (1, 2):
                self.store.settle(self.store.announcement(f"done{app_id}"))
        self.queue("pending", 3)
        self.queue("failed", 4, status="failed")
        self.queue("skipped", 5, status="skipped", error="explicitly_skipped")
        self.queue("unknown", 6, status="unknown")
        self.queue("active", 1)
        self.store.retire(frozenset({1}))
        self.assertEqual(self.store.posted(), {1})
        self.assertEqual(self.statuses(), {
            "done1": ("done", None), "done2": ("done", None), "pending": ("skipped", OFFER_ENDED),
            "failed": ("skipped", OFFER_ENDED), "skipped": ("skipped", OFFER_ENDED), "unknown": ("unknown", None),
            "active": ("pending", None),
        })

    def test_retire_leaves_already_ended_announcements_untouched(self) -> None:
        self.queue("e", 1, status="failed")
        self.store.retire(frozenset())
        self.db.execute("UPDATE discord_effects SET updated_at = 0")
        self.store.retire(frozenset())
        self.assertEqual(self.db.execute("SELECT status, error, updated_at FROM discord_effects").fetchall(),
                         [("skipped", OFFER_ENDED, 0)])


if __name__ == "__main__":
    unittest.main()
