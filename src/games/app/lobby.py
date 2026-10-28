from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from uuid import uuid4

from src.core.app.ports import Presence
from src.core.app.supervisor import TaskGroup
from src.games.app.ports import GameStore, LobbyBoard, PresentError
from src.games.domain.rules import play_problem

NO_CHANNEL = "請先選擇遊戲頻道。"
PUBLISH_STALE = "操作權限或期限已改變，未發送遊戲大廳。"
PUBLISH_RECORDED = "此遊戲大廳發送已有紀錄，請在待處理項目核對。"
PUBLISH_FAILED = "無法在遊戲頻道發送訊息，請確認 Bot 的檢視頻道與傳送訊息權限。"
PUBLISH_REVOKED = "操作權限或設定已改變，未發送遊戲大廳。"
BIND_STALE = "操作權限或期限已改變，未更新遊戲頻道。"


class Lobby:
    def __init__(self, store: GameStore, board: LobbyBoard, presence: Presence, tasks: TaskGroup) -> None:
        self._store = store
        self._board = board
        self._presence = presence
        self._tasks = tasks
        self._hub_lock = asyncio.Lock()

    @property
    def channel_id(self) -> int | None:
        return self._store.channel_id()

    @property
    def role_ids(self) -> list[int]:
        return self._store.role_ids()

    @property
    def hub_message(self) -> list[int] | None:
        return self._store.hub_message()

    def set_roles(self, role_ids: Iterable[int], *, operation_id: str | None = None) -> None:
        self._store.set_roles(sorted(role_ids), operation_id=operation_id)

    def problem(self, guild_id: int | None, channel_id: int | None, role_ids: frozenset[int] | None) -> str | None:
        return self._presence.unavailable_reason(guild_id) or play_problem(
            channel_id, self.channel_id, role_ids, self.role_ids,
        )

    def channel_problem(self) -> str | None:
        return self._board.channel_problem(self.channel_id)

    async def refresh(self) -> None:
        if hub := self.hub_message:
            await self._board.refresh(hub)

    async def bind_channel(
        self, channel_id: int, *, operation_id: str | None = None, still_current: Callable[[], bool] | None = None,
    ) -> str | None:
        if still_current is not None and not still_current():
            return BIND_STALE
        self._store.set_channel(channel_id, operation_id=f"{operation_id}:channel" if operation_id else None)
        if problem := await self.publish_hub(operation_id=operation_id, still_current=still_current):
            return f"已設定遊戲頻道，但{problem}"
        return None

    async def publish_hub(
        self, *, operation_id: str | None = None, still_current: Callable[[], bool] | None = None,
    ) -> str | None:
        with self._tasks.adopt():
            expected = self.channel_id
            async with self._hub_lock:
                return await self._publish(expected, operation_id, still_current)

    async def _publish(
        self, expected: int | None, operation_id: str | None, still_current: Callable[[], bool] | None,
    ) -> str | None:
        channel_id = self.channel_id
        if channel_id is None or not self._board.has_channel(channel_id):
            return NO_CHANNEL

        def current() -> bool:
            return self.channel_id == expected and (still_current is None or still_current())

        if not current():
            return PUBLISH_STALE
        effect_id = f"{operation_id or uuid4()}:game-hub"
        if not self._store.begin_hub(effect_id, channel_id, operation_id=operation_id):
            return PUBLISH_RECORDED
        try:
            message_id = await self._board.post(channel_id, current)
        except PresentError:
            logging.exception("Game hub could not be published.")
            return PUBLISH_FAILED
        if message_id is None:
            self._store.settle_hub(effect_id, "failed", error="revoked_before_send")
            return PUBLISH_REVOKED
        if not current():
            if await self._board.withdraw(channel_id, message_id):
                self._store.settle_hub(effect_id, "skipped", message_id=message_id, error="superseded")
            else:
                logging.warning("Superseded game hub message could not be deleted.")
                self._store.settle_hub(effect_id, "unknown", message_id=message_id, error="superseded_cleanup_failed")
            return None
        previous = self.hub_message
        self._store.record_hub(effect_id, channel_id, message_id)
        await self._board.clear(previous)
        return None
