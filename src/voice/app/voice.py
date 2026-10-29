from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from src.core.app.supervisor import TaskGroup, cancel_all, wait_until
from src.voice.app.ports import GatewayError, StoreError, VoiceGateway, VoiceStore
from src.voice.domain.rooms import RoomSpec, VoiceStatus, bot_problem, creation_notice

CLOSING_CONFIRM = "Bot 正在關閉，未登記待查語音房。"
CLOSING_RETRY = "Bot 正在關閉，未處理語音清理。"
SYNC_FAILED = "入口同步失敗，請重新同步。"

type StillCurrent = Callable[[], bool] | None


class Voice:
    def __init__(self, store: VoiceStore, gateway: VoiceGateway, tasks: TaskGroup) -> None:
        self._store = store
        self._gateway = gateway
        self._tasks = tasks
        self._lock = asyncio.Lock()
        self._closing = False
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._store.enabled()

    def status(self) -> VoiceStatus:
        return VoiceStatus(self._store.entry_id(), self._store.room_count())

    def entry_problem(self) -> str | None:
        entry_id = self._store.entry_id()
        if entry_id is None or not self._gateway.is_voice(entry_id):
            return "找不到已綁定的入口頻道，請在控制台重新選擇入口。"
        return bot_problem(self._gateway.bot_missing(entry_id))

    async def set_enabled(self, value: bool, *, operation_id: str | None = None) -> str | None:
        if self._closing:
            raise RuntimeError("Bot 正在關閉，未修改臨時語音設定。")
        if type(value) is not bool:
            raise ValueError("invalid feature switch")
        turning_on = value and not self.enabled
        applied = self._store.set_enabled(value, operation_id=operation_id)
        if not (applied and turning_on):
            return None
        try:
            await self._own(self._reconcile(self._pinned(None)))
        except Exception:
            logging.warning("Temp voice entry sync after enabling failed.")
            return SYNC_FAILED
        return None

    def stop_new_work(self) -> None:
        self._closing = True

    async def close(self, *, deadline: float) -> None:
        self.stop_new_work()
        try:
            await wait_until(self._tasks.pending(), deadline)
        finally:
            self._closed = True
            cancel_all(self._tasks.pending())
            await asyncio.sleep(0)

    async def _own[T](self, coroutine: Coroutine[Any, Any, T]) -> T | None:
        if self._closing:
            coroutine.close()
            return None
        return await asyncio.shield(self._tasks.spawn(coroutine))

    def _pinned(self, still_current: StillCurrent) -> Callable[[], bool]:
        session = self._gateway.session()
        return lambda: session() and (still_current is None or still_current())

    async def handle_move(self, member_id: int, *, bot: bool, before: int | None, after: int | None) -> None:
        await self._own(self._handle_move(member_id, bot, before, after))

    async def _handle_move(self, member_id: int, bot: bool, before: int | None, after: int | None) -> None:
        if before == after:
            return
        async with self._lock:
            if self._closed:
                return
            entry_id = self._store.entry_id()
            moved_back = None
            if not bot and entry_id is not None and after == entry_id:
                moved_back = await self._enter(member_id, after)
            if self._closed:
                return
            if before is not None and before != moved_back and self._store.is_room(before):
                await self._delete_empty(before, "無法刪除已清空的臨時語音頻道。")

    def _admits(self, member_id: int, entry_id: int) -> bool:
        return (
            not self._closing and self.enabled and self._store.entry_id() == entry_id
            and self._gateway.in_entry(member_id, entry_id)
        )

    async def _owned_room(self, member_id: int) -> int | None:
        channel_id = self._store.room_of(member_id)
        if channel_id is None:
            return None
        if self._gateway.is_voice(channel_id):
            return channel_id
        try:
            if not await self._gateway.exists(channel_id):
                self._drop_room(channel_id)
        except GatewayError:
            logging.warning("Temp voice room %s could not be checked; keeping its record.", channel_id)
        return None

    async def _enter(self, member_id: int, entry_id: int) -> int | None:
        gateway = self._gateway
        if not self._admits(member_id, entry_id):
            return None
        existing = await self._owned_room(member_id)
        if existing is not None:
            if not gateway.can_join(member_id, existing):
                return None
            try:
                await gateway.move(member_id, existing)
            except GatewayError:
                logging.exception("Failed to move temp voice creator back to the existing channel.")
                return None
            return existing
        if self._store.creating(member_id):
            logging.warning("Temp voice creator %s has an unconfirmed room; resolve it before creating another.", member_id)
            return None
        if self._store.room_of(member_id) is not None:
            logging.warning("Temp voice creator %s has a recorded room missing from current cache; not creating another.", member_id)
            return None
        if (problem := bot_problem(gateway.bot_missing(entry_id))) is not None:
            logging.error("Temp voice creation failed: %s", problem)
            await self._notice(entry_id, member_id, problem)
            return None
        spec = gateway.plan(member_id, entry_id)
        created = await self._create(spec)
        if isinstance(created, str):
            await self._notice(entry_id, member_id, created)
            return None
        if created is None:
            return None
        category_id = spec.shape.category_id
        if (self._admits(member_id, entry_id) and gateway.is_voice(created)
                and gateway.category_id(entry_id) == category_id and gateway.category_id(created) == category_id
                and gateway.can_join(member_id, created)):
            try:
                await gateway.move(member_id, created)
                return None
            except GatewayError:
                logging.exception("Temp voice channel created, but the creator could not be moved.")
        await self._delete_empty(created, "無法刪除已清空的臨時語音頻道。")
        return None

    async def _create(self, spec: RoomSpec) -> int | str | None:
        store = self._store
        operation_id = f"voice.create:{uuid.uuid4()}"
        store.begin_create(operation_id, spec)
        try:
            channel_id = await self._gateway.create(spec)
        except GatewayError as error:
            if self._closed:
                return None
            if error.rejected:
                store.reject_create(operation_id, spec)
            logging.exception("Failed to create temp voice channel.")
            return "Discord 拒絕建立語音頻道。" if error.rejected else "Discord 沒有回應，語音房可能沒建好。"
        if self._closed:
            logging.warning("Late temp voice creation needs reconciliation: operation=%s channel=%s", operation_id, channel_id)
            return None
        try:
            store.record_create(operation_id, spec, channel_id)
        except StoreError:
            logging.exception("Temp voice room could not be recorded; deleting the new channel.")
            deleted = await self._delete_empty(channel_id, "無法清理由失敗流程建立的空臨時語音頻道。")
            if self._closed:
                return None
            try:
                store.compensate_create(operation_id, spec, channel_id, deleted=deleted)
            except StoreError:
                logging.exception("Voice operation %s needs reconciliation: channel=%s", operation_id, channel_id)
            return None
        return channel_id

    async def _notice(self, entry_id: int, member_id: int, problem: str) -> None:
        try:
            await self._gateway.notify(entry_id, creation_notice(member_id, problem))
        except GatewayError:
            logging.exception("Temp voice creation notice could not be sent.")

    def _drop_room(self, channel_id: int) -> None:
        if not self._closed:
            self._store.drop_room(channel_id)

    async def _delete_empty(self, channel_id: int, error_message: str, *, allow_unknown: bool = False) -> bool:
        gateway, store = self._gateway, self._store
        if not gateway.is_voice(channel_id) or self._closed or gateway.occupied(channel_id):
            return False
        tracked = False
        try:
            claim = store.claim_delete(channel_id, gateway.guild_id(), allow_unknown=allow_unknown)
            if claim == "done":
                return True
            if claim in {"skipped", "held"}:
                return False
            tracked = True
        except StoreError:
            logging.exception("Temp voice deletion could not be recorded; deleting only known channel %s.", channel_id)
        try:
            await gateway.delete(channel_id)
        except GatewayError as error:
            if tracked and not self._closed:
                store.fail_delete(channel_id, error.reason)
            logging.exception(error_message)
            return False
        if not self._closed:
            try:
                store.finish_delete(channel_id, tracked=tracked)
            except StoreError:
                logging.exception("Temp voice deletion confirmation needs reconciliation: channel=%s", channel_id)
                return False
        return True

    async def handle_channel_delete(self, channel_id: int) -> None:
        await self._own(self._handle_channel_delete(channel_id))

    async def _handle_channel_delete(self, channel_id: int) -> None:
        async with self._lock:
            if self._closed:
                return
            self._drop_room(channel_id)
            if self._store.entry_id() == channel_id:
                self._store.clear_entry()
                logging.warning("Temp voice entry channel deleted; reselect it in the admin panel.")

    async def confirm(self, effect_id: str, channel_id: int, still_current: StillCurrent = None) -> str | None:
        if self._closing:
            return CLOSING_CONFIRM
        return await self._own(self._confirm(effect_id, channel_id, self._pinned(still_current)))

    async def _confirm(self, effect_id: str, channel_id: int, still_current: Callable[[], bool]) -> str | None:
        store = self._store
        async with self._lock:
            if self._closing or not still_current():
                return CLOSING_CONFIRM
            effect = store.effect(effect_id)
            if effect is None or effect.kind != "voice.create":
                return "找不到待查的語音建立紀錄。"
            receipt = store.operation(effect.operation_id)
            if effect.status == "done" and receipt and receipt.resource_id == str(channel_id):
                return None
            if effect.status != "unknown" or receipt is None or receipt.status != "running":
                return "這筆語音建立紀錄不是尚待確認的操作。"
            recorded = effect.payload
            if self._gateway.guild_id() != recorded["guild_id"]:
                return "這筆語音建立紀錄屬於其他伺服器。"
            if type(channel_id) is not int or channel_id <= 0:
                return "請提供實際語音頻道 ID。"
            if receipt.resource_id is not None and receipt.resource_id != str(channel_id):
                return "頻道 ID 與操作已記錄的房間不同。"
            shape = self._gateway.shape(channel_id)
            if shape is None:
                return "找不到目前伺服器中的指定語音頻道。"
            if not shape.matches(recorded):
                return "語音房名稱、分類、權限或語音設定與建立紀錄不符，未登記。"
            owned = store.room_of(recorded["owner_id"])
            if owned is not None and owned != channel_id:
                return "房主已有另一個已登記的語音房，未覆蓋原紀錄。"
            owner = store.owner_of(channel_id)
            if owner is not None and owner != recorded["owner_id"]:
                return "指定語音房已登記給其他房主。"
            try:
                store.confirm_create(effect, channel_id, register=owner is None)
            except StoreError:
                logging.exception("Confirmed temp voice room could not be saved.")
                return "確認結果無法儲存，語音房仍保留待查。"
            return None

    async def retry(self, effect_id: str, still_current: StillCurrent = None) -> str | None:
        if self._closing:
            return CLOSING_RETRY
        return await self._own(self._retry(effect_id, self._pinned(still_current)))

    async def _retry(self, effect_id: str, still_current: Callable[[], bool]) -> str | None:
        store = self._store
        async with self._lock:
            if self._closing or not still_current():
                return CLOSING_RETRY
            effect = store.effect(effect_id)
            if effect is None or effect.kind not in {"voice.create", "voice.delete"}:
                return "找不到語音操作紀錄。"
            if effect.kind == "voice.create":
                return "建立結果不明，請核對實際語音房 ID；不能直接重新建立。"
            if effect.status == "skipped":
                return "這筆清理已略過，未重新刪除語音房。"
            if effect.payload["guild_id"] != self._gateway.guild_id():
                return "這筆清理紀錄屬於其他伺服器。"
            channel_id = effect.payload["channel_id"]
            if not self._gateway.is_voice(channel_id):
                store.settle_delete(effect)
                return None
            if await self._delete_empty(channel_id, "無法重新清理指定臨時語音頻道。", allow_unknown=True):
                return None
            return "頻道仍有成員或清理結果未確認，未刪除紀錄。"

    async def bind_entry(
        self, channel_id: int | None, *, still_current: StillCurrent = None, operation_id: str | None = None,
    ) -> str | None:
        if self._closing:
            return "Bot 正在關閉，未修改入口。"
        if channel_id is None:
            return "只能選擇目前伺服器的語音頻道。"
        return await self._own(self._bind_entry(channel_id, still_current, operation_id))

    async def _bind_entry(self, channel_id: int, still_current: StillCurrent, operation_id: str | None) -> str | None:
        async with self._lock:
            if self._closing or (still_current is not None and not still_current()):
                return "操作已過期或 Bot 正在關閉，未修改入口。"
            if self._gateway.guild_id() is None:
                return "目前無法確認最新的伺服器狀態，未修改入口。"
            if not self._gateway.is_voice(channel_id):
                return "目前找不到指定的語音頻道，未修改入口。"
            if self._store.is_room(channel_id):
                return "不能把臨時語音頻道設為入口。"
            try:
                self._store.bind_entry(channel_id, operation_id=operation_id)
            except StoreError:
                logging.exception("Temp voice entry channel could not be saved.")
                return "臨時語音設定無法儲存。"
        logging.info("Temp voice entry channel bound.")
        return None

    def _usable_entry(self) -> int | None:
        if self._closing:
            return None
        entry_id = self._store.entry_id()
        if entry_id is None:
            return None
        if self._gateway.is_voice(entry_id):
            return entry_id
        self._store.clear_entry()
        logging.warning("Bound temp voice entry channel is gone; reselect it in the admin panel.")
        return None

    async def reconcile(self, *, still_current: StillCurrent = None) -> None:
        await self._own(self._reconcile(still_current))

    async def _reconcile(self, still_current: StillCurrent) -> None:
        current = lambda: still_current is None or still_current()
        async with self._lock:
            if self._closed or self._gateway.guild_id() is None or not current():
                return
            entry_id = self._usable_entry()
            if entry_id is not None and (problem := bot_problem(self._gateway.bot_missing(entry_id))) is not None:
                logging.error("Temp voice entry is bound but unusable: %s", problem)
                entry_id = None
            if entry_id is not None:
                for member_id in self._gateway.humans(entry_id):
                    if not current():
                        return
                    await self._enter(member_id, entry_id)
            if self._closed:
                return
            for channel_id in self._store.rooms():
                if self._closed or not current() or self._gateway.guild_id() is None:
                    return
                if not self._gateway.is_voice(channel_id) or await self._delete_empty(
                    channel_id, "啟動清理時無法刪除空臨時語音頻道。",
                ):
                    self._drop_room(channel_id)
            if entry_id is not None:
                logging.info("Temp voice ready: entry channel bound.")
