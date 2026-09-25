from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Iterable

import aiohttp
import discord

from src.state import write_json_atomic
from src.steam.provider import SteamFetchResult, SteamOffer, SteamOfferProvider
from src.steam.views import build_offer_view

NOTIFICATION_CHANNEL_NAME = "▍ꜱᴛᴇᴀᴍ免費遊戲領取"
STATE_VERSION = 1
DEFAULT_STATE_PATH = Path("/app/data/steam_free_games.json")
POLL_INTERVAL_SECONDS = 15 * 60
AUDIT_REASON = "horo-DCB Steam free game notifications"


class SteamConfigurationError(Exception):
    pass


@dataclass(slots=True)
class _GuildState:
    channel_id: int
    active_app_ids: set[int] = field(default_factory=set)
    role_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class SteamGuildStatus:
    state_available: bool
    poll_interval_seconds: float
    channel_id: int | None
    active_app_count: int
    role_ids: tuple[int, ...] = ()


class SteamFreeGamesNotifier:
    def __init__(self) -> None:
        self._state_path = DEFAULT_STATE_PATH
        self._state_available = True
        self._guilds: dict[int, _GuildState] = {}
        self._guild_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.provider = SteamOfferProvider()
        self._task: asyncio.Task[None] | None = None

        try:
            self._guilds = self._load_state()
        except (OSError, ValueError, TypeError):
            self._state_available = False
            logging.exception(
                "Steam 免費遊戲狀態檔無法讀取；為避免重複洗版，通知功能已停用。"
            )

    def get_guild_status(self, guild_id: int) -> SteamGuildStatus:
        state = self._guilds.get(guild_id)
        return SteamGuildStatus(
            state_available=self._state_available,
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            channel_id=state.channel_id if state is not None else None,
            active_app_count=len(state.active_app_ids) if state is not None else 0,
            role_ids=tuple(sorted(state.role_ids)) if state is not None else (),
        )

    def notification_problem(self, guild: discord.Guild) -> str | None:
        if not self._state_available:
            return "狀態檔不可用，請修復後重新啟動 Bot。"
        state = self._guilds.get(guild.id)
        if state is None:
            return "尚未綁定通知頻道，背景檢查會尋找或建立通知頻道。"
        channel = guild.get_channel(state.channel_id)
        if not self._is_text_channel(channel):
            return "找不到已綁定的通知頻道；請確認 Bot 有管理頻道權限，背景檢查會重新尋找或建立。"
        if guild.me is None:
            return "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。"
        permissions = channel.permissions_for(guild.me)
        missing = [
            label for attribute, label in (
                ("view_channel", "檢視頻道"),
                ("send_messages", "傳送訊息"),
            ) if not getattr(permissions, attribute)
        ]
        if missing:
            return f"Bot 在通知頻道缺少權限：{'、'.join(missing)}。"
        unavailable_roles = sum(
            1 for role_id in state.role_ids
            if (role := guild.get_role(role_id)) is None
            or role.is_default()
            or not self._role_can_notify(guild, role)
        )
        if unavailable_roles:
            return (
                f"有 {unavailable_roles} 個通知身分組已不存在或無法提及；"
                "請重新選擇，或調整可提及設定與 Bot 權限。"
            )
        return None

    def _load_state(self) -> dict[int, _GuildState]:
        if not self._state_path.exists():
            return {}

        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            raise ValueError("invalid Steam notifier state version")

        guild_records = payload.get("guilds")
        if not isinstance(guild_records, list):
            raise ValueError("invalid Steam notifier guild list")

        result: dict[int, _GuildState] = {}
        for item in guild_records:
            if not isinstance(item, dict):
                raise ValueError("invalid Steam notifier guild record")

            guild_id = item.get("guild_id")
            channel_id = item.get("channel_id")
            active_app_ids = item.get("active_app_ids")
            role_ids_value = item.get("role_ids")
            if type(guild_id) is not int or guild_id <= 0:
                raise ValueError("invalid Steam notifier guild id")
            if type(channel_id) is not int or channel_id <= 0:
                raise ValueError("invalid Steam notifier channel id")
            if not isinstance(active_app_ids, list):
                raise ValueError("invalid Steam notifier active app list")
            if guild_id in result:
                raise ValueError("duplicate Steam notifier guild id")

            if "role_id" in item or not isinstance(role_ids_value, list) or len(role_ids_value) > 25:
                raise ValueError("invalid Steam notifier role list")
            role_ids: set[int] = set()
            for role_id in role_ids_value:
                if type(role_id) is not int or role_id <= 0 or role_id == guild_id:
                    raise ValueError("invalid Steam notifier role id")
                role_ids.add(role_id)
            if len(role_ids) != len(role_ids_value):
                raise ValueError("duplicate Steam notifier role id")

            active: set[int] = set()
            for app_id in active_app_ids:
                if type(app_id) is not int or app_id <= 0:
                    raise ValueError("invalid Steam notifier app id")
                active.add(app_id)
            if len(active) != len(active_app_ids):
                raise ValueError("duplicate Steam notifier app id")

            result[guild_id] = _GuildState(
                channel_id=channel_id,
                active_app_ids=active,
                role_ids=role_ids,
            )

        return result

    def _persist_state(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "guilds": [
                {
                    "guild_id": guild_id,
                    "channel_id": state.channel_id,
                    "active_app_ids": sorted(state.active_app_ids),
                    "role_ids": sorted(state.role_ids),
                }
                for guild_id, state in sorted(self._guilds.items())
            ],
        }

        write_json_atomic(self._state_path, payload)

    def _persist_or_disable(self) -> bool:
        if not self._state_available:
            return False
        try:
            self._persist_state()
            return True
        except OSError:
            self._state_available = False
            logging.exception(
                "Steam 免費遊戲狀態無法保存；為避免重複洗版，通知功能已停用。"
            )
            return False

    @staticmethod
    def _role_can_notify(guild: discord.Guild, role: discord.Role) -> bool:
        bot_member = guild.me
        return bool(
            role.mentionable
            or (
                bot_member is not None
                and bot_member.guild_permissions.mention_everyone
            )
        )

    async def set_notification_roles(
        self,
        guild: discord.Guild,
        roles: Iterable[discord.Role],
    ) -> None:
        if not self._state_available:
            raise SteamConfigurationError("Steam 通知狀態目前不可用。")

        selected = tuple(roles)
        if not selected or len(selected) > 25:
            raise SteamConfigurationError("通知身分組必須選擇 1 到 25 個。")

        role_ids: set[int] = set()
        for role in selected:
            if role.guild.id != guild.id or role.is_default():
                raise SteamConfigurationError("只能選擇目前伺服器中的一般身分組。")
            if role.id in role_ids:
                raise SteamConfigurationError("通知身分組不可重複。")
            if not self._role_can_notify(guild, role):
                raise SteamConfigurationError(
                    "選取的身分組中有目前不可被通知的項目；請將身分組設為可提及，或授予 Bot Mention Everyone 權限。"
                )
            role_ids.add(role.id)

        async with self._guild_locks[guild.id]:
            if not self._state_available:
                raise SteamConfigurationError("Steam 通知狀態目前不可用。")

            channel, changed = await self._resolve_notification_channel(guild)
            if channel is None:
                raise SteamConfigurationError("目前無法使用 Steam 通知頻道，請檢查 Bot 權限。")

            state = self._guilds[guild.id]
            if state.role_ids == role_ids and not changed:
                return
            state.role_ids = role_ids
            if not self._persist_or_disable():
                raise SteamConfigurationError("Steam 通知設定目前無法保存。")

    async def clear_notification_roles(self, guild_id: int) -> bool:
        async with self._guild_locks[guild_id]:
            if not self._state_available:
                raise SteamConfigurationError("Steam 通知狀態目前不可用。")
            state = self._guilds.get(guild_id)
            if state is None or not state.role_ids:
                return False
            state.role_ids.clear()
            if not self._persist_or_disable():
                raise SteamConfigurationError("Steam 通知設定目前無法保存。")
            return True

    def start(self, client: discord.Client) -> None:
        if not self._state_available:
            return
        if self._task is not None and not self._task.done():
            return

        self._task = asyncio.create_task(
            self._run_loop(client),
            name="steam-free-games-notifier",
        )

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

        await self.provider.close()

    async def _run_loop(self, client: discord.Client) -> None:
        await client.wait_until_ready()

        while self._state_available:
            try:
                await self.check_once(client.guilds)
            except Exception:
                logging.exception("Steam 免費遊戲背景檢查發生未預期錯誤。")

            if not self._state_available:
                break

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def fetch_current_offers(self) -> SteamFetchResult | None:
        return await self.provider.fetch_current_offers()

    @staticmethod
    def _is_text_channel(channel: object | None) -> bool:
        return getattr(channel, "type", None) == discord.ChannelType.text

    @staticmethod
    def _channel_permissions_ok(
        channel: discord.TextChannel,
        bot_member: discord.Member,
    ) -> bool:
        permissions = channel.permissions_for(bot_member)
        missing = [
            label
            for attribute, label in (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
            )
            if not getattr(permissions, attribute)
        ]
        if missing:
            logging.error(
                "Steam 免費遊戲通知頻道缺少 Bot 權限：%s",
                ", ".join(missing),
            )
            return False
        return True

    async def _resolve_notification_channel(
        self,
        guild: discord.Guild,
    ) -> tuple[discord.TextChannel | None, bool]:
        state = self._guilds.get(guild.id)
        if state is not None:
            stored_channel = guild.get_channel(state.channel_id)
            if self._is_text_channel(stored_channel):
                bot_member = guild.me
                if bot_member is None:
                    return None, False
                if not self._channel_permissions_ok(stored_channel, bot_member):
                    return None, False
                return stored_channel, False  # type: ignore[return-value]

        candidates = [
            channel
            for channel in guild.channels
            if self._is_text_channel(channel)
            and channel.name == NOTIFICATION_CHANNEL_NAME
        ]
        if len(candidates) > 1:
            logging.error(
                "找到多個同名 Steam 免費遊戲通知頻道，無法安全綁定 Channel ID：%s",
                NOTIFICATION_CHANNEL_NAME,
            )
            return None, False

        channel: discord.TextChannel | None
        if len(candidates) == 1:
            channel = candidates[0]  # type: ignore[assignment]
        else:
            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_channels:
                logging.error(
                    "找不到 Steam 免費遊戲通知頻道，而且 Bot 缺少 Manage Channels。"
                )
                return None, False
            try:
                channel = await guild.create_text_channel(
                    NOTIFICATION_CHANNEL_NAME,
                    reason=AUDIT_REASON,
                )
            except (discord.Forbidden, discord.HTTPException):
                logging.exception("自動建立 Steam 免費遊戲通知頻道失敗。")
                return None, False

        bot_member = guild.me
        if bot_member is None or not self._channel_permissions_ok(channel, bot_member):
            return None, False

        previous_active = state.active_app_ids if state is not None else set()
        previous_role_ids = state.role_ids if state is not None else set()
        self._guilds[guild.id] = _GuildState(
            channel_id=channel.id,
            active_app_ids=set(previous_active),
            role_ids=set(previous_role_ids),
        )
        logging.info("已綁定 Steam 免費遊戲通知 Channel ID。")
        return channel, True

    def _resolve_notification_roles(
        self,
        guild: discord.Guild,
        state: _GuildState,
    ) -> tuple[tuple[discord.Role, ...], bool]:
        if not state.role_ids:
            return (), False

        changed = False
        resolved: list[discord.Role] = []
        for role_id in sorted(state.role_ids):
            role = guild.get_role(role_id)
            if role is None or role.is_default():
                state.role_ids.discard(role_id)
                changed = True
                continue
            if not self._role_can_notify(guild, role):
                logging.warning(
                    "Steam 免費遊戲通知身分組目前不可被提及，將暫時略過其中一個身分組。"
                )
                continue
            resolved.append(role)
        return tuple(resolved), changed

    async def _send_offer(
        self,
        channel: discord.TextChannel,
        offer: SteamOffer,
        roles: Iterable[discord.Role] = (),
    ) -> bool:
        selected_roles = tuple(roles)
        try:
            await channel.send(
                view=build_offer_view(offer, selected_roles),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=list(selected_roles) if selected_roles else False,
                    replied_user=False,
                ),
            )
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError):
            logging.exception("Steam 免費遊戲 Discord 通知未確認送出成功。")
            return False
        logging.info("已送出 Steam 免費遊戲通知：%s (%s)", offer.name, offer.app_id)
        return True

    async def check_once(self, guilds: Iterable[discord.Guild]) -> None:
        if not self._state_available:
            return
        result = await self.fetch_current_offers()
        if result is None:
            return

        guild_map = {guild.id: guild for guild in guilds}
        for guild_id in list(self._guilds):
            if guild_id not in guild_map:
                lock = self._guild_locks[guild_id]
                async with lock:
                    if guild_id not in guild_map and guild_id in self._guilds:
                        self._guilds.pop(guild_id)
                        if not self._persist_or_disable():
                            return

        offers_by_id = {offer.app_id: offer for offer in result.offers}
        for guild in guild_map.values():
            async with self._guild_locks[guild.id]:
                if not self._state_available:
                    return

                changed = False
                channel, channel_changed = await self._resolve_notification_channel(guild)
                changed = changed or channel_changed
                if channel is None:
                    continue

                state = self._guilds[guild.id]
                roles, roles_changed = self._resolve_notification_roles(guild, state)
                changed = changed or roles_changed
                next_active = state.active_app_ids & result.active_app_ids
                new_ids = sorted(
                    app_id
                    for app_id in offers_by_id
                    if app_id not in state.active_app_ids
                )

                for app_id in new_ids:
                    if await self._send_offer(channel, offers_by_id[app_id], roles):
                        next_active.add(app_id)

                if next_active != state.active_app_ids:
                    state.active_app_ids = next_active
                    changed = True

                if changed and not self._persist_or_disable():
                    return
