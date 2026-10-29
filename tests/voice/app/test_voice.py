import asyncio
import unittest
from dataclasses import replace

from src.core.app.supervisor import WORK, TaskGroup
from src.voice import Voice, VoiceStatus
from src.voice.app.ports import GatewayError
from src.voice.domain.rooms import build_temp_voice_name
from tests.support.voice import GUILD, FakeGateway, FakeVoiceStore, shape

ENTRY, ALICE, BOB = 10, 1, 2
CLOSING_CONFIRM = "Bot 正在關閉，未登記待查語音房。"
CLOSING_RETRY = "Bot 正在關閉，未處理語音清理。"


class VoiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store, self.gateway = FakeVoiceStore(), FakeGateway()
        self.gateway.channel(ENTRY)
        self.tasks = TaskGroup("voice", WORK)
        self.voice = Voice(self.store, self.gateway, self.tasks)
        await self.voice.set_enabled(True)
        self.assertIsNone(await self.voice.bind_entry(ENTRY))

    async def join(self, member_id: int, before: int | None = None) -> None:
        member = self.gateway.members.get(member_id) or self.gateway.member(member_id, name=f"user{member_id}")
        member.channel = ENTRY
        await self.voice.handle_move(member_id, bot=member.bot, before=before, after=ENTRY)

    async def leave(self, member_id: int) -> None:
        member = self.gateway.members[member_id]
        before, member.channel = member.channel, None
        await self.voice.handle_move(member_id, bot=member.bot, before=before, after=None)

    def rooms(self) -> dict[int, int]:
        return dict(self.store.owners)

    def creations(self):
        return [effect for effect in self.store.effects.values() if effect.kind == "voice.create"]

    async def uncertain(self, member_id: int = ALICE):
        self.gateway.failures["create"] = GatewayError("TimeoutError")
        with self.assertLogs(level="ERROR"):
            await self.join(member_id)
        del self.gateway.failures["create"]
        (effect,) = self.creations()
        self.assertEqual(effect.status, "unknown")
        return effect, max(self.gateway.channels)

    async def holding_lock(self) -> tuple[asyncio.Task, asyncio.Event]:
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked() -> None:
            started.set()
            await release.wait()

        self.gateway.during["create"] = blocked
        holder = asyncio.create_task(self.join(BOB))
        await started.wait()
        del self.gateway.during["create"]
        return holder, release


class EntryTests(VoiceTestCase):
    async def test_joining_the_entry_creates_a_named_room_and_moves_the_member_there(self) -> None:
        await self.join(ALICE)
        (spec,) = self.gateway.created
        self.assertEqual(spec.shape.name, build_temp_voice_name("user1"))
        (room,) = self.rooms()
        self.assertEqual(self.rooms(), {room: ALICE})
        self.assertEqual(self.gateway.moves, [(ALICE, room)])
        (effect,) = self.creations()
        self.assertEqual((effect.status, self.store.operations[effect.operation_id].resource_id), ("done", str(room)))
        self.assertEqual(effect.operation_id, effect.effect_id)
        self.assertTrue(effect.operation_id.startswith("voice.create:"))

    async def test_disabling_during_creation_deletes_the_new_room(self) -> None:
        self.gateway.during["create"] = lambda: self.voice.set_enabled(False)
        await self.join(ALICE)
        (room,) = self.gateway.deleted
        self.assertEqual((self.gateway.moves, self.rooms()), ([], {}))
        self.assertEqual(self.store.effects[f"voice.delete:{room}"].status, "done")

    async def test_rejoining_moves_the_owner_back_without_a_second_room(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        await self.join(ALICE, before=room)
        self.assertEqual(len(self.gateway.created), 1)
        self.assertEqual(self.gateway.moves, [(ALICE, room), (ALICE, room)])
        self.assertEqual(self.gateway.deleted, [])

    async def test_a_recorded_room_that_no_longer_exists_is_replaced(self) -> None:
        await self.join(ALICE)
        (old,) = self.rooms()
        del self.gateway.channels[old]
        await self.join(ALICE)
        (new,) = self.rooms()
        self.assertNotEqual(new, old)
        self.assertEqual(self.gateway.moves[-1], (ALICE, new))

    async def test_an_unverifiable_missing_room_keeps_its_record_and_blocks_a_second_room(self) -> None:
        await self.join(ALICE)
        (old,) = self.rooms()
        del self.gateway.channels[old]
        self.gateway.failures["exists"] = GatewayError("HTTPException")
        with self.assertLogs(level="WARNING"):
            await self.join(ALICE)
        self.assertEqual((len(self.gateway.created), self.rooms()), (1, {old: ALICE}))

    async def test_owner_walking_into_the_entry_is_moved_back_and_only_the_left_empty_room_is_deleted(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        other = self.gateway.channel().id
        self.store.owners[other] = BOB
        for before in (room, other):
            with self.subTest(before=before):
                await self.join(ALICE, before=before)
                self.assertEqual(self.gateway.moves[-1], (ALICE, room))
        self.assertEqual(self.gateway.deleted, [other])
        self.assertEqual(self.rooms(), {room: ALICE})

    async def test_staying_in_the_entry_creates_nothing(self) -> None:
        self.gateway.member(BOB, channel=ENTRY)
        await self.voice.handle_move(BOB, bot=False, before=ENTRY, after=ENTRY)
        self.assertEqual(self.gateway.created, [])

    async def test_bots_and_members_who_cannot_connect_get_no_room(self) -> None:
        self.gateway.member(BOB, bot=True)
        await self.join(BOB)
        self.gateway.channels[ENTRY].denied.add(ALICE)
        await self.join(ALICE)
        self.assertEqual(self.gateway.created, [])

    async def test_leaving_an_owned_room_empty_deletes_it_even_when_disabled(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        await self.voice.set_enabled(False)
        await self.leave(ALICE)
        self.assertEqual((self.gateway.deleted, self.rooms()), ([room], {}))
        await self.join(BOB)
        self.assertEqual(len(self.gateway.created), 1)

    async def test_a_room_with_members_left_is_kept(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        self.gateway.member(BOB, channel=room)
        await self.leave(ALICE)
        self.assertEqual((self.gateway.deleted, self.rooms()), ([], {room: ALICE}))

    async def test_creation_failures_are_explained_in_the_entry_chat(self) -> None:
        self.gateway.failures["create"] = GatewayError("Forbidden", rejected=True)
        with self.assertLogs(level="ERROR"):
            await self.join(ALICE)
        self.assertEqual(self.gateway.notices, [(ENTRY, "<@1>，無法建立你的語音房：Discord 拒絕建立語音頻道。請通知管理員。")])
        (effect,) = self.creations()
        self.assertEqual((effect.status, effect.error), ("failed", "creation_rejected"))
        self.assertEqual(self.store.operations[effect.operation_id].status, "failed")

        self.gateway.notices.clear()
        self.gateway.missing = ("連線", "移動成員")
        self.gateway.failures["notify"] = GatewayError("HTTPException")
        with self.assertLogs(level="ERROR") as logs:
            await self.join(ALICE)
        self.assertEqual(len(self.gateway.created), 1)
        self.assertIn("notice could not be sent", "\n".join(logs.output))
        del self.gateway.failures["notify"]
        with self.assertLogs(level="ERROR"):
            await self.join(ALICE)
        self.assertEqual(self.gateway.notices, [(ENTRY, "<@1>，無法建立你的語音房：Bot 缺少權限：連線、移動成員。請通知管理員。")])

    async def test_unanswered_creation_tells_the_member_it_may_not_exist(self) -> None:
        await self.uncertain()
        self.assertEqual(self.gateway.notices, [(ENTRY, "<@1>，無法建立你的語音房：Discord 沒有回應，語音房可能沒建好。請通知管理員。")])


class RaceTests(VoiceTestCase):
    async def test_guild_replaced_during_creation_neither_moves_nor_deletes_an_occupied_room(self) -> None:
        async def replace_guild() -> None:
            self.gateway.reconnect()
            self.gateway.members[ALICE].channel = None
            self.gateway.member(BOB, channel=max(self.gateway.channels))

        self.gateway.during["create"] = replace_guild
        await self.join(ALICE)
        self.assertEqual((self.gateway.moves, self.gateway.deleted, len(self.rooms())), ([], [], 1))

    async def test_connect_revoked_during_creation_deletes_the_room_unmoved(self) -> None:
        self.gateway.during["create"] = lambda: self.gateway.channels[ENTRY].denied.add(ALICE)
        await self.join(ALICE)
        self.assertEqual((self.gateway.moves, len(self.gateway.deleted), self.rooms()), ([], 1, {}))

    async def test_rebinding_or_recategorizing_the_entry_during_creation_prevents_the_move(self) -> None:
        def rebind() -> None:
            self.store.entry = 999

        def recategorize() -> None:
            entry = self.gateway.channels[ENTRY]
            entry.shape = replace(entry.shape, category_id=999)

        for change in (rebind, recategorize):
            with self.subTest(change=change.__name__):
                self.store.entry = ENTRY
                self.gateway.channels[ENTRY].shape = shape()
                self.gateway.during["create"] = change
                await self.join(ALICE)
                self.assertEqual((self.gateway.moves, self.rooms()), ([], {}))

    async def test_a_missing_guild_after_creation_keeps_the_room_and_blocks_a_second_one(self) -> None:
        def vanish() -> None:
            self.gateway.guild = None

        self.gateway.during["create"] = vanish
        await self.join(ALICE)
        self.assertEqual((self.gateway.moves, self.gateway.deleted, len(self.rooms())), ([], [], 1))
        await self.join(ALICE)
        self.assertEqual(len(self.gateway.created), 1)

    async def test_member_leaving_the_entry_before_creation_returns_is_not_pulled_back(self) -> None:
        self.gateway.during["create"] = lambda: setattr(self.gateway.members[ALICE], "channel", None)
        await self.join(ALICE)
        self.assertEqual((self.gateway.moves, len(self.gateway.deleted), self.rooms()), ([], 1, {}))

    async def test_failed_move_deletes_the_new_room_and_its_record(self) -> None:
        self.gateway.failures["move"] = GatewayError("TimeoutError")
        with self.assertLogs(level="ERROR"):
            await self.join(ALICE)
        (room,) = self.gateway.deleted
        self.assertEqual((self.rooms(), self.store.effects[f"voice.delete:{room}"].status), ({}, "done"))
        del self.gateway.failures["move"]
        await self.join(ALICE)
        self.assertEqual((len(self.gateway.created), len(self.rooms())), (2, 1))


class RecordTests(VoiceTestCase):
    async def test_unknown_creation_is_never_rebuilt_and_can_be_confirmed(self) -> None:
        effect, room = await self.uncertain()
        with self.assertLogs(level="WARNING"):
            await self.join(ALICE)
            await self.voice.reconcile()
        self.assertEqual((len(self.gateway.created), self.gateway.deleted, self.rooms()), (1, [], {}))
        self.assertEqual(await self.voice.retry(effect.effect_id), "建立結果不明，請核對實際語音房 ID；不能直接重新建立。")
        self.assertIsNone(await self.voice.confirm(effect.effect_id, room))
        self.assertEqual(self.rooms(), {room: ALICE})
        receipt = self.store.operations[effect.operation_id]
        self.assertEqual((receipt.status, receipt.resource_id), ("done", str(room)))
        self.assertIsNone(await self.voice.confirm(effect.effect_id, room))

    async def test_confirmation_checks_the_room_against_the_record(self) -> None:
        effect, room = await self.uncertain()
        channel = self.gateway.channels[room]
        recorded = channel.shape
        cases = (
            ("名稱", lambda: setattr(channel, "shape", replace(recorded, name="別人的房間")),
             "語音房名稱、分類、權限或語音設定與建立紀錄不符，未登記。"),
            ("頻道", lambda: self.gateway.channels.pop(room), "找不到目前伺服器中的指定語音頻道。"),
            ("房主", lambda: self.store.owners.__setitem__(self.gateway.channel().id, ALICE),
             "房主已有另一個已登記的語音房，未覆蓋原紀錄。"),
        )
        for name, change, problem in cases:
            with self.subTest(name):
                change()
                self.assertEqual(await self.voice.confirm(effect.effect_id, room), problem)
                self.gateway.channels[room] = channel
                channel.shape = recorded
        self.assertEqual(self.store.effects[effect.effect_id].status, "unknown")
        self.assertNotIn(room, self.rooms())

    async def test_confirmation_refuses_records_it_cannot_trust(self) -> None:
        effect, room = await self.uncertain()
        self.assertEqual(await self.voice.confirm("missing", room), "找不到待查的語音建立紀錄。")
        self.assertEqual(await self.voice.confirm(effect.effect_id, 0), "請提供實際語音頻道 ID。")
        self.store.owners[room] = BOB
        self.assertEqual(await self.voice.confirm(effect.effect_id, room), "指定語音房已登記給其他房主。")
        del self.store.owners[room]
        self.store.effects[effect.effect_id] = replace(effect, payload={**effect.payload, "guild_id": GUILD + 1})
        self.assertEqual(await self.voice.confirm(effect.effect_id, room), "這筆語音建立紀錄屬於其他伺服器。")
        self.store.effects[effect.effect_id] = replace(effect, status="failed")
        self.assertEqual(await self.voice.confirm(effect.effect_id, room), "這筆語音建立紀錄不是尚待確認的操作。")

    async def test_failed_registration_deletes_the_room_and_allows_a_fresh_one(self) -> None:
        self.store.failing.add("record_create")
        with self.assertLogs(level="ERROR"):
            await self.join(ALICE)
        (room,) = self.gateway.deleted
        (effect,) = self.creations()
        self.assertEqual((effect.status, effect.error), ("failed", "compensated"))
        self.assertEqual(self.store.operations[effect.operation_id].status, "failed")
        self.store.failing.clear()
        await self.join(ALICE)
        self.assertEqual((len(self.gateway.created), len(self.rooms())), (2, 1))

    async def test_failed_registration_and_cleanup_stay_confirmable_without_a_second_room(self) -> None:
        self.store.failing.add("record_create")
        self.gateway.failures["delete"] = GatewayError("TimeoutError")
        with self.assertLogs(level="ERROR"):
            await self.join(ALICE)
        (effect,) = self.creations()
        room = max(self.gateway.channels)
        receipt = self.store.operations[effect.operation_id]
        self.assertEqual((effect.status, receipt.status, receipt.resource_id), ("unknown", "running", str(room)))
        self.assertEqual(self.store.effects[f"voice.delete:{room}"].error, "TimeoutError")
        with self.assertLogs(level="WARNING"):
            await self.join(ALICE)
        self.assertEqual(len(self.gateway.created), 1)
        self.store.failing.clear()
        self.assertIsNone(await self.voice.confirm(effect.effect_id, room))
        self.assertEqual(self.rooms(), {room: ALICE})

    async def test_unsaved_confirmation_leaves_the_record_pending(self) -> None:
        effect, room = await self.uncertain()
        self.store.failing.add("confirm_create")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await self.voice.confirm(effect.effect_id, room), "確認結果無法儲存，語音房仍保留待查。")
        self.assertEqual((self.store.effects[effect.effect_id].status, self.rooms()), ("unknown", {}))
        self.store.failing.clear()
        self.assertIsNone(await self.voice.confirm(effect.effect_id, room))

    async def test_unrecorded_deletion_still_deletes_the_known_room(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        self.store.failing.add("claim_delete")
        with self.assertLogs(level="ERROR"):
            await self.leave(ALICE)
        self.assertEqual((self.gateway.deleted, self.rooms()), ([room], {}))
        self.assertNotIn(f"voice.delete:{room}", self.store.effects)


class RetryTests(VoiceTestCase):
    async def failed_delete(self) -> int:
        await self.join(ALICE)
        (room,) = self.rooms()
        self.gateway.failures["delete"] = GatewayError("TimeoutError")
        with self.assertLogs(level="ERROR"):
            await self.leave(ALICE)
        del self.gateway.failures["delete"]
        self.assertEqual(self.store.effects[f"voice.delete:{room}"].status, "unknown")
        self.assertEqual(self.rooms(), {room: ALICE})
        return room

    async def test_unknown_deletion_is_held_until_an_explicit_retry(self) -> None:
        room = await self.failed_delete()
        await self.voice.reconcile()
        self.assertEqual((self.gateway.deleted, self.rooms()), ([], {room: ALICE}))
        self.assertIsNone(await self.voice.retry(f"voice.delete:{room}"))
        self.assertEqual((self.gateway.deleted, self.rooms()), ([room], {}))
        self.assertEqual(self.store.effects[f"voice.delete:{room}"].status, "done")
        self.assertEqual(len(self.gateway.created), 1)

    async def test_retrying_a_room_that_is_already_gone_settles_the_record(self) -> None:
        room = await self.failed_delete()
        del self.gateway.channels[room]
        self.assertIsNone(await self.voice.retry(f"voice.delete:{room}"))
        self.assertEqual((self.gateway.deleted, self.rooms()), ([], {}))
        self.assertEqual(self.store.effects[f"voice.delete:{room}"].status, "done")

    async def test_retry_refuses_occupied_skipped_foreign_and_unknown_records(self) -> None:
        room = await self.failed_delete()
        effect_id = f"voice.delete:{room}"
        self.gateway.member(BOB, channel=room)
        self.assertEqual(await self.voice.retry(effect_id), "頻道仍有成員或清理結果未確認，未刪除紀錄。")
        effect = self.store.effects[effect_id]
        self.store.effects[effect_id] = replace(effect, payload={**effect.payload, "guild_id": GUILD + 1})
        self.assertEqual(await self.voice.retry(effect_id), "這筆清理紀錄屬於其他伺服器。")
        self.store.effects[effect_id] = replace(effect, status="skipped")
        self.assertEqual(await self.voice.retry(effect_id), "這筆清理已略過，未重新刪除語音房。")
        self.assertEqual(await self.voice.retry("missing"), "找不到語音操作紀錄。")
        self.assertEqual(self.gateway.deleted, [])

    async def test_a_reconnect_before_the_lock_turns_confirm_and_retry_away(self) -> None:
        effect, room = await self.uncertain()
        holder, release = await self.holding_lock()
        confirm = asyncio.create_task(self.voice.confirm(effect.effect_id, room))
        retry = asyncio.create_task(self.voice.retry(effect.effect_id))
        await asyncio.sleep(0)
        self.gateway.reconnect()
        release.set()
        await holder
        self.assertEqual((await confirm, await retry), (CLOSING_CONFIRM, CLOSING_RETRY))
        self.assertEqual(self.store.effects[effect.effect_id].status, "unknown")

    async def test_revoked_caller_stops_retry_before_deleting(self) -> None:
        room = await self.failed_delete()
        current = [True]
        holder, release = await self.holding_lock()
        retry = asyncio.create_task(self.voice.retry(f"voice.delete:{room}", lambda: current[0]))
        await asyncio.sleep(0)
        current[0] = False
        release.set()
        await holder
        self.assertEqual(await retry, CLOSING_RETRY)
        self.assertNotIn(room, self.gateway.deleted)


class SettingsTests(VoiceTestCase):
    async def test_status_reports_the_entry_and_tracked_rooms(self) -> None:
        await self.join(ALICE)
        self.assertEqual(self.voice.status(), VoiceStatus(ENTRY, 1))
        self.assertTrue(self.voice.enabled)

    async def test_entry_problem_names_what_blocks_the_entry(self) -> None:
        self.assertIsNone(self.voice.entry_problem())
        self.gateway.missing = None
        self.assertEqual(self.voice.entry_problem(), "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。")
        del self.gateway.channels[ENTRY]
        self.assertEqual(self.voice.entry_problem(), "找不到已綁定的入口頻道，請在控制台重新選擇入口。")

    async def test_binding_rejects_non_voice_temp_missing_and_unsaved_choices(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        self.assertEqual(await self.voice.bind_entry(None), "只能選擇目前伺服器的語音頻道。")
        self.assertEqual(await self.voice.bind_entry(room), "不能把臨時語音頻道設為入口。")
        self.assertEqual(await self.voice.bind_entry(77), "目前找不到指定的語音頻道，未修改入口。")
        self.store.failing.add("bind_entry")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await self.voice.bind_entry(self.gateway.channel().id), "臨時語音設定無法儲存。")
        self.gateway.guild = None
        self.assertEqual(await self.voice.bind_entry(77), "目前無法確認最新的伺服器狀態，未修改入口。")
        self.assertEqual(self.voice.status().entry_channel_id, ENTRY)

    async def test_binding_that_went_stale_while_waiting_does_not_write(self) -> None:
        current = [True]
        other = self.gateway.channel().id
        holder, release = await self.holding_lock()
        binding = asyncio.create_task(self.voice.bind_entry(other, still_current=lambda: current[0]))
        await asyncio.sleep(0)
        current[0] = False
        release.set()
        await holder
        self.assertEqual(await binding, "操作已過期或 Bot 正在關閉，未修改入口。")
        self.assertEqual(self.voice.status().entry_channel_id, ENTRY)

    async def test_deleting_the_entry_unbinds_it(self) -> None:
        del self.gateway.channels[ENTRY]
        with self.assertLogs(level="WARNING"):
            await self.voice.handle_channel_delete(ENTRY)
        self.assertIsNone(self.voice.status().entry_channel_id)
        await self.join(ALICE)
        self.assertEqual(self.gateway.created, [])

    async def test_deleting_a_room_forgets_it(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        await self.voice.handle_channel_delete(room)
        self.assertEqual(self.rooms(), {})

    async def test_turning_on_syncs_members_already_in_the_entry_once(self) -> None:
        await self.voice.set_enabled(False)
        self.gateway.member(ALICE, channel=ENTRY)
        self.assertIsNone(await self.voice.set_enabled(True))
        (room,) = self.rooms()
        self.assertEqual(self.gateway.moves, [(ALICE, room)])
        self.gateway.member(BOB, channel=ENTRY)
        self.assertIsNone(await self.voice.set_enabled(True))
        self.assertEqual(self.rooms(), {room: ALICE})

    async def test_turning_off_does_not_sync(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        self.gateway.members[ALICE].channel = None
        self.assertIsNone(await self.voice.set_enabled(False))
        self.assertEqual((self.gateway.deleted, self.rooms()), ([], {room: ALICE}))

    async def test_failed_sync_after_turning_on_is_reported_and_the_switch_stays_on(self) -> None:
        await self.voice.set_enabled(False)
        self.gateway.member(ALICE, channel=ENTRY)
        self.store.failing.add("begin_create")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.voice.set_enabled(True), "入口同步失敗，請重新同步。")
        self.assertEqual((self.voice.enabled, self.gateway.created), (True, []))

    async def test_switch_rejects_non_booleans(self) -> None:
        with self.assertRaises(ValueError):
            await self.voice.set_enabled(1)


class ReconcileTests(VoiceTestCase):
    async def test_reconcile_deletes_empty_rooms_and_forgets_missing_ones(self) -> None:
        await self.join(ALICE)
        await self.join(BOB)
        gone, empty = self.rooms()
        del self.gateway.channels[gone]
        self.gateway.members[ALICE].channel = self.gateway.members[BOB].channel = None
        await self.voice.reconcile()
        self.assertEqual((self.gateway.deleted, self.rooms()), ([empty], {}))
        self.assertEqual(self.voice.status().entry_channel_id, ENTRY)

    async def test_reconcile_gives_waiting_members_rooms(self) -> None:
        self.gateway.member(ALICE, channel=ENTRY, name="user1")
        self.gateway.member(BOB, channel=ENTRY, bot=True)
        await self.voice.reconcile()
        self.assertEqual([spec.owner_id for spec in self.gateway.created], [ALICE])

    async def test_disabled_reconcile_still_cleans_empty_rooms(self) -> None:
        await self.join(ALICE)
        self.gateway.members[ALICE].channel = None
        await self.voice.set_enabled(False)
        await self.voice.reconcile()
        self.assertEqual((len(self.gateway.deleted), self.rooms()), (1, {}))

    async def test_reconcile_unbinds_a_vanished_entry_and_skips_an_unusable_one(self) -> None:
        self.gateway.member(ALICE, channel=ENTRY)
        self.gateway.missing = ("連線",)
        with self.assertLogs(level="ERROR"):
            await self.voice.reconcile()
        self.assertEqual(self.gateway.created, [])
        del self.gateway.channels[ENTRY]
        with self.assertLogs(level="WARNING"):
            await self.voice.reconcile()
        self.assertIsNone(self.voice.status().entry_channel_id)

    async def test_reconcile_rechecks_rooms_between_deletions(self) -> None:
        await self.join(ALICE)
        await self.join(BOB)
        first, second = self.rooms()
        self.gateway.members[ALICE].channel = self.gateway.members[BOB].channel = None
        self.gateway.during["delete"] = lambda: self.gateway.member(3, channel=second)
        await self.voice.reconcile()
        self.assertEqual((self.gateway.deleted, list(self.rooms())), ([first], [second]))

    async def test_reconcile_stops_once_its_caller_is_revoked(self) -> None:
        await self.join(ALICE)
        await self.join(BOB)
        first, second = self.rooms()
        self.gateway.members[ALICE].channel = self.gateway.members[BOB].channel = None
        authorized = [True]
        self.gateway.during["delete"] = lambda: authorized.__setitem__(0, False)
        await self.voice.reconcile(still_current=lambda: authorized[0])
        self.assertEqual((self.gateway.deleted, list(self.rooms())), ([first], [second]))

    async def test_reconcile_without_a_guild_does_nothing(self) -> None:
        await self.join(ALICE)
        self.gateway.members[ALICE].channel = None
        self.gateway.guild = None
        await self.voice.reconcile()
        self.assertEqual((self.gateway.deleted, len(self.rooms())), ([], 1))


class ShutdownTests(VoiceTestCase):
    async def test_cancelled_caller_does_not_lose_owned_work(self) -> None:
        await self.join(ALICE)
        (room,) = self.rooms()
        holder, release = await self.holding_lock()
        caller = asyncio.create_task(self.voice.handle_channel_delete(room))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        release.set()
        await holder
        await self.voice.close(deadline=asyncio.get_running_loop().time() + 0.5)
        self.assertNotIn(room, self.rooms())
        self.assertFalse(self.tasks.pending())

    async def test_closed_voice_refuses_new_work(self) -> None:
        await self.voice.close(deadline=asyncio.get_running_loop().time())
        self.assertEqual(await self.voice.bind_entry(ENTRY), "Bot 正在關閉，未修改入口。")
        self.assertEqual(await self.voice.confirm("voice.create:x", 5), CLOSING_CONFIRM)
        self.assertEqual(await self.voice.retry("voice.delete:5"), CLOSING_RETRY)
        with self.assertRaises(RuntimeError):
            await self.voice.set_enabled(False)
        await self.join(ALICE)
        await self.voice.reconcile()
        self.assertEqual(self.gateway.created, [])

    async def test_late_creation_after_close_never_writes(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def stubborn() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        self.gateway.during["create"] = stubborn
        caller = asyncio.create_task(self.join(ALICE))
        await started.wait()
        await self.voice.close(deadline=asyncio.get_running_loop().time() + 0.01)
        self.store.sealed = True
        release.set()
        with self.assertLogs(level="WARNING"):
            await caller
        await asyncio.sleep(0)
        self.assertFalse(self.tasks.pending())
        self.assertEqual(self.creations()[0].status, "unknown")

    async def test_cancelled_creation_caller_finishes_before_shutdown_drains(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def slow() -> None:
            started.set()
            await release.wait()
            self.gateway.member(BOB, channel=max(self.gateway.channels))

        self.gateway.during["create"] = slow
        caller = asyncio.create_task(self.join(ALICE))
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(self.tasks.pending())
        release.set()
        await self.voice.close(deadline=asyncio.get_running_loop().time() + 0.5)
        self.assertFalse(self.tasks.pending())
        self.assertEqual((len(self.rooms()), self.creations()[0].status, self.gateway.deleted), (1, "done", []))

    async def test_cancelling_close_still_cancels_owned_remote_work(self) -> None:
        started = asyncio.Event()

        async def hang() -> None:
            started.set()
            await asyncio.Event().wait()

        self.gateway.during["create"] = hang
        caller = asyncio.create_task(self.join(ALICE))
        await started.wait()
        closing = asyncio.create_task(self.voice.close(deadline=asyncio.get_running_loop().time() + 5))
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        with self.assertRaises(asyncio.CancelledError):
            await caller
        await asyncio.sleep(0)
        self.assertFalse(self.tasks.pending())
        self.assertEqual(self.creations()[0].status, "unknown")


if __name__ == "__main__":
    unittest.main()
