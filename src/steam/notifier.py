from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict

import aiohttp
import discord

from src.core.adapters.discord.channels import post_problem, text_channel_problem
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.app.effects import EffectKind, MessageProof, delivered
from src.core.app.supervisor import TaskGroup
from src.core.domain.records import DiscordEffect
from src.core.domain.settings import Setting
from src.steam.models import SteamConfigurationError, SteamFetchResult, SteamOffer, SteamStatus, offer_from_payload
from src.steam.provider import SteamOfferProvider
from src.steam.views import build_offer_view

POLL_INTERVAL_SECONDS = 15 * 60
STEAM_ENABLED = Setting("feature.steam_free_games", False)
STEAM_CHANNEL_ID: Setting[int | None] = Setting("steam.channel_id", None)
STEAM_ROLE_IDS: Setting[list[int]] = Setting("steam.role_ids", [])


def announces_offer(effect: DiscordEffect, message: discord.Message) -> str | None:
    body = message.content + json.dumps([component.to_dict() for component in message.components], ensure_ascii=False)
    app_id = effect.payload.get("offer", {}).get("app_id")
    if type(app_id) is not int or app_id <= 0 or f"https://store.steampowered.com/app/{app_id}/" not in body:
        return "訊息內容不是這一項 Steam 活動。"
    return None


class SteamFreeGamesNotifier:
    def __init__(self, db: sqlite3.Connection, live: LiveGuild, tasks: TaskGroup) -> None:
        self._db = db
        self._live = live
        self._lock = asyncio.Lock()
        self.provider = SteamOfferProvider()
        self._tasks = tasks
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._client: discord.Client | None = None
        self.operations = OperationStore(db)
        self._settings = SettingsStore(db, self.operations)
        self.last_success: int | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.get(STEAM_ENABLED)

    def effect_kinds(self) -> tuple[EffectKind, ...]:
        return (
            EffectKind("steam.announce", "Steam 免費遊戲通知", proof=MessageProof(check=announces_offer),
                       retry=self._retry_announce, settle=self._mark_posted),
        )

    async def _retry_announce(self, effect_id: str, still_current: Callable[[], bool]) -> str | None:
        return delivered(await self.retry_effect(effect_id, allow_unknown=True, still_current=still_current))

    def _mark_posted(self, effect: DiscordEffect) -> None:
        self._db.execute("INSERT OR IGNORE INTO steam_posted (app_id) VALUES (?)", (effect.payload["offer"]["app_id"],))

    def set_enabled(self, value: bool, *, operation_id: str | None = None) -> None:
        if self._closing:
            raise SteamConfigurationError("Bot 正在關閉，無法修改 Steam 通知設定。")
        if type(value) is not bool:
            raise ValueError("enabled must be a boolean")
        if not self._settings.save(
            "steam.set_enabled", {"enabled": value}, {STEAM_ENABLED: value}, operation_id=operation_id,
        ):
            return
        if value and self._client is not None:
            self.start(self._client)
        elif not value and self._task is not None:
            self._task.cancel()

    def _posted_app_ids(self) -> set[int]:
        return {app_id for (app_id,) in self._db.execute("SELECT app_id FROM steam_posted")}

    def status(self) -> SteamStatus:
        return SteamStatus(
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            channel_id=self._settings.get(STEAM_CHANNEL_ID),
            active_app_count=len(self._posted_app_ids()),
            role_ids=tuple(sorted(self._settings.get(STEAM_ROLE_IDS))),
            running=self._task is not None and not self._task.done() and not self._task.cancelling(),
            last_success=self.last_success,
            last_error=self.last_error,
            pending_count=len(self.operations.effects(status=("pending", "unknown", "failed"), kind="steam.announce")),
        )

    def notification_problem(self, guild: discord.Guild) -> str | None:
        channel = guild.get_channel(self._settings.get(STEAM_CHANNEL_ID))
        if problem := self._channel_problem(guild, channel):
            return problem
        unavailable_roles = sum(
            1
            for role_id in self._settings.get(STEAM_ROLE_IDS)
            if (role := guild.get_role(role_id)) is None
            or role.is_default()
            or not self._role_can_notify(guild, channel, role)
        )
        if unavailable_roles:
            return (
                f"有 {unavailable_roles} 個通知身分組已不存在或無法提及；"
                "請重新選擇，或調整可提及設定與 Bot 權限。"
            )
        return None

    @staticmethod
    def _role_can_notify(
        guild: discord.Guild, channel: discord.TextChannel, role: discord.Role
    ) -> bool:
        bot_member = guild.me
        return bool(
            role.mentionable
            or (bot_member is not None and channel.permissions_for(bot_member).mention_everyone)
        )

    async def set_notification_roles(
        self,
        guild: discord.Guild | None,
        roles: Iterable[discord.Role],
        *,
        still_current: Callable[[], bool] | None = None,
        operation_id: str | None = None,
    ) -> None:
        selected = tuple(roles)
        if selected and guild is None:
            raise SteamConfigurationError("無法取得目前伺服器。")
        if len(selected) > 25:
            raise SteamConfigurationError("通知身分組最多 25 個。")

        role_ids: set[int] = set()
        for role in selected:
            if role.guild.id != guild.id or role.is_default():
                raise SteamConfigurationError("只能選擇目前伺服器中的一般身分組。")
            if role.id in role_ids:
                raise SteamConfigurationError("通知身分組不可重複。")
            role_ids.add(role.id)

        async with self._lock:
            if self._closing:
                raise SteamConfigurationError("Bot 正在關閉，無法修改 Steam 通知設定。")
            if still_current is not None and not still_current():
                return

            if selected:
                guild = self._live.guild()
                if guild is None:
                    raise SteamConfigurationError("目前無法確認最新的伺服器狀態。")
                resolved = [guild.get_role(role_id) for role_id in sorted(role_ids)]
                if any(role is None or role.guild is not guild or role.is_default() for role in resolved):
                    raise SteamConfigurationError("選取的通知身分組已不存在，未修改設定。")
                channel = guild.get_channel(self._settings.get(STEAM_CHANNEL_ID))
                if self._channel_problem(guild, channel):
                    raise SteamConfigurationError("請先選擇可用的 Steam 通知頻道，並確認 Bot 權限。")

                for role in resolved:
                    if not self._role_can_notify(guild, channel, role):
                        raise SteamConfigurationError(
                            "選取的身分組中有目前不可被通知的項目；請將身分組設為可提及，或授予 Bot「提及 @everyone、@here 和所有身分組」權限。"
                        )

            self._settings.save(
                "steam.set_roles", {"role_ids": sorted(role_ids)}, {STEAM_ROLE_IDS: sorted(role_ids)},
                operation_id=operation_id,
            )

    async def bind_channel(
        self,
        guild: discord.Guild,
        channel: object | None,
        *,
        still_current: Callable[[], bool] | None = None,
        operation_id: str | None = None,
    ) -> str | None:
        if problem := text_channel_problem(channel, guild):
            return problem
        async with self._lock:
            if self._closing:
                return "Bot 正在關閉，無法修改 Steam 通知設定。"
            if still_current is not None and not still_current():
                return None
            guild = self._live.guild()
            if guild is None:
                return "目前無法確認最新的伺服器狀態，未綁定頻道。"
            channel = guild.get_channel(channel.id)
            if channel is None:
                return "指定通知頻道已不存在，未綁定頻道。"
            if problem := text_channel_problem(channel, guild):
                return problem
            if problem := self._channel_problem(guild, channel):
                return problem
            self._settings.save(
                "steam.bind_channel", {"channel_id": channel.id}, {STEAM_CHANNEL_ID: channel.id}, operation_id=operation_id,
            )
        logging.info("Steam free games notification channel bound.")
        return None

    def start(self, client: discord.Client) -> None:
        self._client = client
        if self._closing or not self.enabled:
            return
        if self._task is not None and not self._task.done():
            if self._task.cancelling():
                self._task.add_done_callback(lambda _: self.start(client) if not self._closing and self.enabled else None)
            return
        self._task = self._tasks.spawn(self._run_loop(client), name="steam-free-games-notifier")

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
            await self.provider.close()

    async def _run_loop(self, client: discord.Client) -> None:
        await client.wait_until_ready()

        while not self._closing and self.enabled:
            if self._live.guild() is not None:
                try:
                    await self.check_once()
                except sqlite3.Error:
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
        return await self.provider.fetch_current_offers()

    @staticmethod
    def _channel_problem(guild: discord.Guild, channel: object | None) -> str | None:
        return post_problem(guild, channel, "通知頻道")

    def _resolve_notification_roles(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
    ) -> tuple[discord.Role, ...]:
        role_ids = self._settings.get(STEAM_ROLE_IDS)
        kept: list[int] = []
        resolved: list[discord.Role] = []
        for role_id in sorted(role_ids):
            role = guild.get_role(role_id)
            if role is None or role.is_default():
                continue
            kept.append(role_id)
            if not self._role_can_notify(guild, channel, role):
                logging.warning(
                    "A Steam free games notification role is not mentionable; skipping it for now."
                )
                continue
            resolved.append(role)
        if len(kept) != len(role_ids):
            with transaction(self._db):
                self._settings.put({STEAM_ROLE_IDS: kept})
        return tuple(resolved)

    async def _send_offer(
        self,
        channel: discord.TextChannel,
        offer: SteamOffer,
        roles: Iterable[discord.Role] = (), *, nonce: str | None = None,
    ) -> discord.Message | None:
        if self._closing:
            return None
        selected_roles = tuple(roles)
        message = await channel.send(
            view=build_offer_view(offer, selected_roles),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, roles=list(selected_roles) if selected_roles else False, replied_user=False,
            ),
            nonce=nonce,
        )
        logging.info("Sent Steam free games notification: %s (%s)", offer.name, offer.app_id)
        return message

    async def _send_effect(
        self, effect: DiscordEffect, *, allow_unknown: bool = False, still_current=None,
    ) -> bool:
        effect = self.operations.get_effect(effect.effect_id)
        if (effect is None or effect.status not in ({"pending", "unknown", "failed"} if allow_unknown else {"pending"})
            or self._closing or not self.enabled or (still_current is not None and not still_current())):
            return False
        guild = self._live.guild()
        if guild is None:
            self.last_error = "目前無法確認最新的伺服器狀態，通知仍保留待處理。"
            return False
        channel = guild.get_channel(effect.channel_id)
        if problem := self._channel_problem(guild, channel):
            self.last_error = problem
            return False
        offer = offer_from_payload(effect.payload["offer"])
        allowed = set(self._settings.get(STEAM_ROLE_IDS))
        roles = tuple(
            role for role_id in effect.payload["role_ids"]
            if role_id in allowed and (role := guild.get_role(role_id)) is not None
            and not role.is_default() and self._role_can_notify(guild, channel, role)
        )
        if still_current is not None and not still_current():
            return False
        with transaction(self._db):
            if effect.status != "pending":
                self.operations.update_effect(effect.effect_id, "pending", retry=True)
            self.operations.update_effect(effect.effect_id, "unknown")
        try:
            message = await self._send_offer(channel, offer, roles, nonce=effect.nonce)
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError) as exc:
            self.last_error = "Discord 通知送達結果不明，請至「待處理項目」核對。"
            status = "failed" if isinstance(exc, discord.HTTPException) and 400 <= exc.status < 500 and exc.status != 429 else "unknown"
            with transaction(self._db):
                self.operations.update_effect(effect.effect_id, status, error=type(exc).__name__)
            logging.exception("Steam free games Discord notification delivery unconfirmed.")
            return False
        message_id = getattr(message, "id", None)
        with transaction(self._db):
            self.operations.update_effect(effect.effect_id, "done", message_id=message_id if type(message_id) is int else None)
            self._mark_posted(effect)
        return True

    async def retry_effect(self, effect_id: str, *, allow_unknown: bool = False, still_current=None) -> bool:
        async with self._lock:
            if still_current is not None and not still_current():
                return False
            guild = self._live.guild()
            if guild is None:
                return False
            effect = self.operations.get_effect(effect_id)
            if effect is None or effect.kind != "steam.announce":
                return False
            return await self._send_effect(effect, allow_unknown=allow_unknown, still_current=still_current)

    def _blocking(self) -> set[int]:
        return {
            effect.payload["offer"]["app_id"]
            for effect in self.operations.effects(status=("pending", "unknown", "failed", "skipped"), kind="steam.announce")
            if effect.status != "skipped" or effect.error != "offer-ended"
        }

    async def check_once(self) -> None:
        if self._closing:
            return
        effects = self.operations.effects(status=("pending", "unknown", "failed"), kind="steam.announce")
        tracked = self._posted_app_ids() | {effect.payload["offer"]["app_id"] for effect in effects}
        result = await self.provider.fetch_current_offers(tracked_app_ids=frozenset(tracked))
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
            guild = self._live.guild()
            if guild is None:
                self.last_error = "目前無法確認最新的伺服器狀態，通知仍保留待處理。"
                return
            channel_id = self._settings.get(STEAM_CHANNEL_ID)
            if channel_id is None:
                return
            channel = guild.get_channel(channel_id)
            if problem := self._channel_problem(guild, channel):
                self.last_error = problem
                logging.error("Steam notification channel is bound but unusable: %s", problem)
                return

            roles = self._resolve_notification_roles(guild, channel)
            posted = self._posted_app_ids()
            effects = self.operations.effects(status=("pending", "failed", "skipped"), kind="steam.announce")
            with transaction(self._db):
                self._db.executemany(
                    "DELETE FROM steam_posted WHERE app_id = ?",
                    [(app_id,) for app_id in posted - result.active_app_ids],
                )
                for effect in effects:
                    if effect.status == "skipped" and effect.error == "offer-ended":
                        continue
                    if effect.payload["offer"]["app_id"] not in result.active_app_ids:
                        self.operations.update_effect(effect.effect_id, "skipped", error="offer-ended")
            blocking = self._blocking()
            for app_id in sorted(offers_by_id.keys() - posted):
                if self._closing or not self.enabled:
                    return
                if app_id in blocking:
                    continue
                payload = {"offer": asdict(offers_by_id[app_id]), "role_ids": [role.id for role in roles]}
                with transaction(self._db):
                    self.operations.create_effect(
                        f"steam:{app_id}:{uuid.uuid4()}", "steam.announce", channel_id, payload, nonce=uuid.uuid4().hex[:24],
                    )
            for effect in self.operations.effects(status="pending", kind="steam.announce"):
                await self._send_effect(effect)
