import unittest

from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.operations import OperationStore
from src.games.adapters.sqlite.store import GAME_ACTION, SqliteGameStore
from src.games.app.tables import Posted
from src.games.domain.table import Seat

DONE = {"ok": True}


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.db = db
        self.store = SqliteGameStore(db)
        self.records = OperationStore(db)

    def opened(self, seat: Seat, operation_id: str = "open", *, retire: Posted | None = None) -> None:
        effect_id = f"{operation_id}:table-send"
        self.store.begin_open(effect_id, seat.channel_id, {"user_id": seat.user_id}, operation_id, {}, {"ok": False})
        self.store.finish_open(seat, effect_id, operation_id, {}, DONE, retire=retire)

    def test_settings_default_and_save_once_per_operation(self) -> None:
        self.assertEqual((self.store.channel_id(), self.store.role_ids(), self.store.hub_message()), (None, [], None))
        self.store.set_roles([3, 9], operation_id="roles")
        self.store.set_roles([3, 9], operation_id="roles")
        self.store.set_channel(10, operation_id="channel")
        self.assertEqual((self.store.channel_id(), self.store.role_ids()), (10, [3, 9]))
        self.assertEqual(self.records.get("roles").kind, "games.roles")
        with self.assertRaises(ValueError):
            self.store.set_roles([4], operation_id="roles")

    def test_one_seat_per_player(self) -> None:
        self.opened(Seat(1, "slots", 10, 20))
        self.opened(Seat(1, "blackjack", 10, 30), "again")
        self.opened(Seat(2, "slots", 10, 40), "other")
        self.assertEqual(self.store.seat(1), Seat(1, "blackjack", 10, 30))
        self.assertEqual(self.store.seats(), [Seat(1, "blackjack", 10, 30), Seat(2, "slots", 10, 40)])
        self.assertTrue(self.store.close_seat(1))
        self.assertFalse(self.store.close_seat(1))
        self.assertIsNone(self.store.seat(1))

    def test_finishing_an_open_records_the_receipt_and_retires_the_old_table(self) -> None:
        self.opened(Seat(1, "slots", 10, 20))
        self.opened(Seat(1, "blackjack", 11, 30), "switch", retire=Posted(10, 20))
        receipt = self.records.get("switch")
        self.assertEqual((receipt.kind, receipt.status, receipt.resource_id), (GAME_ACTION, "done", "30"))
        self.assertEqual(self.records.get_effect("switch:table-send").status, "done")
        retired = self.records.get_effect("switch:table-delete")
        self.assertEqual((retired.kind, retired.channel_id, retired.payload), ("game_table_delete", 11, {"user_id": 1, "message_id": 20}))

    def test_moves_commit_only_while_the_seat_exists(self) -> None:
        self.opened(Seat(1, "slots", 10, 20))
        posted = Posted(10, 20)
        self.assertTrue(self.store.commit_move(1, "spin", {}, {"ok": True, "resource_id": "20"}, posted=posted, leave=False))
        self.assertEqual(self.records.get_effect("spin:table-edit").kind, "game_table_edit")
        self.assertTrue(self.store.commit_move(1, "leave", {}, DONE, posted=posted, leave=True))
        self.assertIsNone(self.store.seat(1))
        self.assertEqual(self.records.get_effect("leave:table-delete").channel_id, 10)
        self.assertFalse(self.store.commit_move(1, "late", {}, DONE, posted=posted, leave=False))
        self.assertIsNone(self.records.get("late"))

    def test_prior_returns_even_an_unconfirmed_result(self) -> None:
        self.store.begin_open("op:table-send", 10, {"user_id": 1}, "op", {"a": 1}, {"ok": False, "code": "unknown"})
        self.assertEqual(self.store.prior("op", {"a": 1}), {"ok": False, "code": "unknown"})
        self.assertIsNone(self.store.prior("missing", {}))
        with self.assertRaises(ValueError):
            self.store.prior("op", {"a": 2})

    def test_orphans_are_the_players_unsettled_opens(self) -> None:
        for operation_id, user_id in (("mine", 1), ("theirs", 2)):
            self.store.begin_open(f"{operation_id}:table-send", 10, {"user_id": user_id}, operation_id, {}, DONE)
        self.opened(Seat(1, "slots", 10, 20), "settled")
        [orphan] = self.store.open_orphans(1)
        self.store.supersede_open(orphan, {"ok": False, "code": "superseded"})
        self.assertEqual((self.records.get("mine").status, self.records.get("theirs").status), ("failed", "running"))
        self.assertEqual(self.store.open_orphans(1), [])

    def test_hub_pointer_and_its_effect_change_together(self) -> None:
        self.assertTrue(self.store.begin_hub("a:game-hub", 10, operation_id="a"))
        self.assertFalse(self.store.begin_hub("a:game-hub", 10, operation_id="a"))
        self.store.record_hub("a:game-hub", 10, 50)
        self.assertEqual(self.store.hub_message(), [10, 50])
        self.assertEqual(self.records.get_effect("a:game-hub").status, "done")
        with self.assertRaises(ValueError):
            self.store.record_hub("missing:game-hub", 10, 60)
        self.assertEqual(self.store.hub_message(), [10, 50])
        self.assertTrue(self.store.begin_hub("b:game-hub", 10, operation_id="b"))
        self.assertEqual(self.records.get_effect("b:game-hub").payload, {"previous_hub": [10, 50]})

    def test_idle_close_removes_the_seat_with_an_optional_cleanup(self) -> None:
        self.opened(Seat(1, "slots", 10, 20))
        self.store.close_idle(1, "games.idle:20", retire=Posted(10, 20))
        self.assertIsNone(self.store.seat(1))
        self.assertEqual(self.records.get_effect("games.idle:20:table-delete").channel_id, 10)
        self.opened(Seat(2, "blackjack", 10, 30), "other")
        self.store.close_idle(2, "games.idle:30", retire=None)
        self.assertIsNone(self.records.get_effect("games.idle:30:table-delete"))

    def test_marking_ignores_effects_that_were_never_recorded(self) -> None:
        self.store.mark("missing:table-edit", "unknown")
        self.assertIsNone(self.records.get_effect("missing:table-edit"))


if __name__ == "__main__":
    unittest.main()
