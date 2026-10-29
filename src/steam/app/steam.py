from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence

from src.core.app.supervisor import TaskGroup
from src.steam.app.ports import Announcer, DeliveryError, Guild, OfferSource, SteamStore, StoreError
from src.steam.domain.offers import (
    OFFER_ENDED,
    Announcement,
    SteamConfigurationError,
    SteamFetchResult,
    SteamStatus,
    announced_app,
)

POLL_INTERVAL_SECONDS = 15 * 60
CLOSING = "Bot 正在關閉，無法修改 Steam 通知設定。"
GUILD_PENDING = "目前無法確認最新的伺服器狀態，通知仍保留待處理。"
PICK_TEXT = "只能選擇目前伺服器的文字頻道。"
WAITING = ("pending", "unknown", "failed")

type StillCurrent = Callable[[], bool] | None


class Steam:
    def __init__(self, store: SteamStore, source: OfferSource, announcer: Announcer, tasks: TaskGroup) -> None:
        self._store = store
        self._source = source
        self._announcer = announcer
        self._tasks = tasks
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self.last_success: int | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._store.enabled()

    def set_enabled(self, value: bool, *, operation_id: str | None = None) -> None:
        if self._closing:
            raise SteamConfigurationError(CLOSING)
        if type(value) is not bool:
            raise ValueError("enabled must be a boolean")
        if not self._store.set_enabled(value, operation_id=operation_id):
            return
        if value and self._started:
            self.start()
        elif not value and self._task is not None:
            self._task.cancel()

    def status(self) -> SteamStatus:
        return SteamStatus(
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            channel_id=self._store.channel_id(),
            active_app_count=len(self._store.posted()),
            role_ids=tuple(sorted(self._store.role_ids())),
            running=self._task is not None and not self._task.done() and not self._task.cancelling(),
            last_success=self.last_success,
            last_error=self.last_error,
            pending_count=len(self._store.announcements(*WAITING)),
        )

    def notification_problem(self) -> str | None:
        guild = self._announcer.guild()
        if guild is None:
            return None
        channel_id = self._store.channel_id()
        if problem := guild.channel_problem(channel_id):
            return problem
        unavailable = sum(1 for role_id in self._store.role_ids() if guild.role(role_id, channel_id) != "usable")
        if unavailable:
            return (
                f"有 {unavailable} 個通知身分組已不存在或無法提及；"
                "請重新選擇，或調整可提及設定與 Bot 權限。"
            )
        return None

    async def set_roles(
        self, role_ids: Sequence[int], *, still_current: StillCurrent = None, operation_id: str | None = None,
    ) -> None:
        selected = tuple(role_ids)
        guild = self._announcer.guild()
        if selected and guild is None:
            raise SteamConfigurationError("無法取得目前伺服器。")
        if len(selected) > 25:
            raise SteamConfigurationError("通知身分組最多 25 個。")
        chosen: set[int] = set()
        for role_id in selected:
            if guild.role(role_id, None) == "default":
                raise SteamConfigurationError("只能選擇目前伺服器中的一般身分組。")
            if role_id in chosen:
                raise SteamConfigurationError("通知身分組不可重複。")
            chosen.add(role_id)

        async with self._lock:
            if self._closing:
                raise SteamConfigurationError(CLOSING)
            if still_current is not None and not still_current():
                return
            if chosen:
                guild = self._announcer.guild()
                if guild is None:
                    raise SteamConfigurationError("目前無法確認最新的伺服器狀態。")
                channel_id = self._store.channel_id()
                states = [guild.role(role_id, channel_id) for role_id in sorted(chosen)]
                if any(state in ("missing", "default") for state in states):
                    raise SteamConfigurationError("選取的通知身分組已不存在，未修改設定。")
                if guild.channel_problem(channel_id):
                    raise SteamConfigurationError("請先選擇可用的 Steam 通知頻道，並確認 Bot 權限。")
                if "muted" in states:
                    raise SteamConfigurationError(
                        "選取的身分組中有目前不可被通知的項目；請將身分組設為可提及，或授予 Bot「提及 @everyone、@here 和所有身分組」權限。"
                    )
            self._store.set_roles(sorted(chosen), operation_id=operation_id)

    async def bind_channel(
        self, channel_id: int | None, *, still_current: StillCurrent = None, operation_id: str | None = None,
    ) -> str | None:
        if channel_id is None:
            return PICK_TEXT
        async with self._lock:
            if self._closing:
                return CLOSING
            if still_current is not None and not still_current():
                return None
            guild = self._announcer.guild()
            if guild is None:
                return "目前無法確認最新的伺服器狀態，未綁定頻道。"
            if problem := guild.bind_problem(channel_id):
                return problem
            self._store.bind_channel(channel_id, operation_id=operation_id)
        logging.info("Steam free games notification channel bound.")
        return None

    def start(self) -> None:
        self._started = True
        if self._closing or not self.enabled:
            return
        if self._task is not None and not self._task.done():
            if self._task.cancelling():
                self._task.add_done_callback(lambda _: self.start() if not self._closing and self.enabled else None)
            return
        self._task = self._tasks.spawn(self._run_loop(), name="steam-free-games-notifier")

    def stop_new_work(self) -> None:
        self._closing = True

    async def close(self, *, deadline: float) -> None:
        self.stop_new_work()
        try:
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        finally:
            self._task = None
            await self._source.close()

    async def _run_loop(self) -> None:
        await self._announcer.ready()

        while not self._closing and self.enabled:
            if self._announcer.guild() is not None:
                try:
                    await self.check_once()
                except StoreError:
                    self.last_error = "資料庫寫入失敗；自動通知已停止。"
                    logging.exception(
                        "Steam state could not be saved; notifications stopped to avoid duplicate posts."
                    )
                    return
                except Exception:
                    self.last_error = "背景檢查失敗，稍後會重試。"
                    logging.exception("Unexpected error in Steam free games background check.")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def fetch_current_offers(self) -> SteamFetchResult | None:
        if self._closing:
            return None
        return await self._source.fetch_current_offers()

    def _notification_roles(self, guild: Guild, channel_id: int) -> tuple[int, ...]:
        role_ids = self._store.role_ids()
        kept: list[int] = []
        usable: list[int] = []
        for role_id in sorted(role_ids):
            state = guild.role(role_id, channel_id)
            if state in ("missing", "default"):
                continue
            kept.append(role_id)
            if state == "muted":
                logging.warning(
                    "A Steam free games notification role is not mentionable; skipping it for now."
                )
                continue
            usable.append(role_id)
        if len(kept) != len(role_ids):
            self._store.keep_roles(kept)
        return tuple(usable)

    async def _deliver(self, effect_id: str, *, allow_unknown: bool = False, still_current: StillCurrent = None) -> bool:
        effect = self._store.announcement(effect_id)
        if (effect is None or effect.status not in (WAITING if allow_unknown else ("pending",))
                or self._closing or not self.enabled or (still_current is not None and not still_current())):
            return False
        guild = self._announcer.guild()
        if guild is None:
            self.last_error = GUILD_PENDING
            return False
        if problem := guild.channel_problem(effect.channel_id):
            self.last_error = problem
            return False
        announcement = Announcement.parse(effect.payload)
        allowed = set(self._store.role_ids())
        role_ids = tuple(
            role_id for role_id in announcement.role_ids
            if role_id in allowed and guild.role(role_id, effect.channel_id) == "usable"
        )
        if still_current is not None and not still_current():
            return False
        self._store.begin_delivery(effect)
        try:
            message_id = await guild.announce(effect.channel_id, announcement.offer, role_ids, effect.nonce)
        except DeliveryError as error:
            self.last_error = "Discord 通知送達結果不明，請至「待處理項目」核對。"
            self._store.fail_delivery(effect.effect_id, "failed" if error.rejected else "unknown", error.reason)
            logging.exception("Steam free games Discord notification delivery unconfirmed.")
            return False
        self._store.finish_delivery(effect, message_id)
        return True

    async def retry(self, effect_id: str, *, still_current: StillCurrent = None) -> bool:
        async with self._lock:
            if still_current is not None and not still_current():
                return False
            if self._announcer.guild() is None:
                return False
            return await self._deliver(effect_id, allow_unknown=True, still_current=still_current)

    def _blocking(self) -> set[int]:
        return {
            announced_app(effect.payload)
            for effect in self._store.announcements(*WAITING, "skipped")
            if effect.status != "skipped" or effect.error != OFFER_ENDED
        }

    async def check_once(self) -> None:
        if self._closing:
            return
        tracked = self._store.posted() | {announced_app(effect.payload) for effect in self._store.announcements(*WAITING)}
        result = await self._source.fetch_current_offers(tracked_app_ids=frozenset(tracked))
        if result is None:
            self.last_error = "目前無法取得 Steam 資料。"
            return
        if self._closing or not self.enabled:
            return
        self.last_success = int(time.time())
        self.last_error = f"{result.failed_app_count} 款遊戲詳細資料無法取得。" if result.failed_app_count else None

        offers_by_id = {offer.app_id: offer for offer in result.offers}
        async with self._lock:
            if self._closing or not self.enabled:
                return
            guild = self._announcer.guild()
            if guild is None:
                self.last_error = GUILD_PENDING
                return
            channel_id = self._store.channel_id()
            if channel_id is None:
                return
            if problem := guild.channel_problem(channel_id):
                self.last_error = problem
                logging.error("Steam notification channel is bound but unusable: %s", problem)
                return

            role_ids = self._notification_roles(guild, channel_id)
            posted = self._store.posted()
            self._store.retire(result.active_app_ids)
            blocking = self._blocking()
            for app_id in sorted(offers_by_id.keys() - posted):
                if self._closing or not self.enabled:
                    return
                if app_id in blocking:
                    continue
                self._store.queue(
                    f"steam:{app_id}:{uuid.uuid4()}", channel_id, Announcement(offers_by_id[app_id], role_ids).payload(),
                    uuid.uuid4().hex[:24],
                )
            for effect in self._store.announcements("pending"):
                await self._deliver(effect.effect_id)
