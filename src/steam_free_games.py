from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
import html
import json
import logging
import os
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import urlparse

import aiohttp
import discord

NOTIFICATION_CHANNEL_NAME = "▍ꜱᴛᴇᴀᴍ免費遊戲領取"
STATE_VERSION = 1
DEFAULT_STATE_PATH = Path("/app/data/steam_free_games.json")
POLL_INTERVAL_SECONDS = 15 * 60
REQUEST_TIMEOUT_SECONDS = 30
FETCH_BATCH_TIMEOUT_SECONDS = 90
AUDIT_REASON = "horo-DCB Steam free game notifications"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) "
    "Gecko/20100101 Firefox/134.0"
)
STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_APP_ID_PATTERN = re.compile(r"/apps/(\d+)/")


@dataclass(frozen=True, slots=True)
class SteamOffer:
    app_id: int
    name: str
    old_price: str
    description: str
    developers: tuple[str, ...]
    header_image: str | None

    @property
    def store_url(self) -> str:
        return f"https://store.steampowered.com/app/{self.app_id}/"


@dataclass(frozen=True, slots=True)
class SteamFetchResult:
    active_app_ids: frozenset[int]
    offers: tuple[SteamOffer, ...]


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
    def __init__(
        self,
        state_path: Path | str = DEFAULT_STATE_PATH,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._state_path = Path(state_path)
        self._poll_interval_seconds = poll_interval_seconds
        self._state_available = True
        self._guilds: dict[int, _GuildState] = {}
        self._guild_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None

        try:
            self._guilds = self._load_state()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._state_available = False
            logging.exception(
                "Steam 免費遊戲狀態檔無法讀取；為避免重複洗版，通知功能已停用。"
            )

    def get_guild_status(self, guild_id: int) -> SteamGuildStatus:
        state = self._guilds.get(guild_id)
        return SteamGuildStatus(
            state_available=self._state_available,
            poll_interval_seconds=self._poll_interval_seconds,
            channel_id=state.channel_id if state is not None else None,
            active_app_count=len(state.active_app_ids) if state is not None else 0,
            role_ids=tuple(sorted(getattr(state, "role_ids", ()))) if state is not None else (),
        )

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
            legacy_role_id = item.get("role_id")
            if type(guild_id) is not int or guild_id <= 0:
                raise ValueError("invalid Steam notifier guild id")
            if type(channel_id) is not int or channel_id <= 0:
                raise ValueError("invalid Steam notifier channel id")
            if not isinstance(active_app_ids, list):
                raise ValueError("invalid Steam notifier active app list")
            if guild_id in result:
                raise ValueError("duplicate Steam notifier guild id")

            role_ids: set[int] = set()
            if role_ids_value is not None:
                if not isinstance(role_ids_value, list) or len(role_ids_value) > 25:
                    raise ValueError("invalid Steam notifier role list")
                if legacy_role_id is not None:
                    raise ValueError("ambiguous Steam notifier role state")
                for role_id in role_ids_value:
                    if type(role_id) is not int or role_id <= 0 or role_id == guild_id:
                        raise ValueError("invalid Steam notifier role id")
                    role_ids.add(role_id)
                if len(role_ids) != len(role_ids_value):
                    raise ValueError("duplicate Steam notifier role id")
            elif legacy_role_id is not None:
                if (
                    type(legacy_role_id) is not int
                    or legacy_role_id <= 0
                    or legacy_role_id == guild_id
                ):
                    raise ValueError("invalid Steam notifier role id")
                role_ids.add(legacy_role_id)

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

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._state_path.with_name(f"{self._state_path.name}.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self._state_path)

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

    def _ensure_session(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )

    def start(self, client: discord.Client) -> None:
        if not self._state_available:
            return
        if self._task is not None and not self._task.done():
            return

        self._ensure_session()
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

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _run_loop(self, client: discord.Client) -> None:
        await client.wait_until_ready()

        while self._state_available:
            try:
                await self.check_once(client.guilds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Steam 免費遊戲背景檢查發生未預期錯誤。")

            if not self._state_available:
                break

            await asyncio.sleep(self._poll_interval_seconds)

    @staticmethod
    def _extract_app_id(logo_url: object) -> int | None:
        if not isinstance(logo_url, str):
            return None
        match = _APP_ID_PATTERN.search(logo_url)
        if match is None:
            return None
        try:
            app_id = int(match.group(1))
        except ValueError:
            return None
        return app_id if app_id > 0 else None

    @staticmethod
    def _clean_text(value: object, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        cleaned = " ".join(html.unescape(value).split())
        return cleaned[:limit]

    @staticmethod
    def _safe_https_url(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            return None
        return value

    async def _request_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> object | None:
        if self._session is None or self._session.closed:
            return None

        try:
            async with self._session.get(url, params=params) as response:
                if response.status != 200:
                    logging.warning(
                        "Steam 免費遊戲請求失敗：HTTP %s",
                        response.status,
                    )
                    return None
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            logging.exception("Steam 免費遊戲 HTTP 請求失敗。")
            return None

    async def _fetch_offer(self, app_id: int, fallback_name: str) -> SteamOffer | None:
        payload = await self._request_json(
            STEAM_APPDETAILS_URL,
            params={
                "appids": str(app_id),
                "l": "tchinese",
                "filters": "basic,short_description,developers,price_overview",
            },
        )
        if not isinstance(payload, dict):
            return None

        entry = payload.get(str(app_id))
        if not isinstance(entry, dict) or entry.get("success") is not True:
            return None
        data = entry.get("data")
        if not isinstance(data, dict) or data.get("type") != "game":
            return None

        price = data.get("price_overview")
        if not isinstance(price, dict):
            return None
        initial = price.get("initial")
        discount_percent = price.get("discount_percent")
        if (
            data.get("is_free") is not True
            or type(initial) is not int
            or initial <= 0
            or discount_percent != 100
        ):
            return None

        name = self._clean_text(data.get("name"), 200) or self._clean_text(
            fallback_name,
            200,
        )
        if not name:
            return None

        old_price = self._clean_text(price.get("initial_formatted"), 80)
        description = self._clean_text(data.get("short_description"), 1000)

        developers_value = data.get("developers")
        developers: tuple[str, ...] = ()
        if isinstance(developers_value, list):
            developers = tuple(
                cleaned
                for item in developers_value[:5]
                if (cleaned := self._clean_text(item, 100))
            )

        return SteamOffer(
            app_id=app_id,
            name=name,
            old_price=old_price,
            description=description,
            developers=developers,
            header_image=self._safe_https_url(data.get("header_image")),
        )

    async def fetch_current_offers(self) -> SteamFetchResult | None:
        self._ensure_session()
        try:
            async with asyncio.timeout(FETCH_BATCH_TIMEOUT_SECONDS):
                return await self._fetch_current_offers()
        except TimeoutError:
            logging.warning(
                "Steam 免費遊戲整批查詢超過 %s 秒，已取消。",
                FETCH_BATCH_TIMEOUT_SECONDS,
            )
            return None

    async def _fetch_current_offers(self) -> SteamFetchResult | None:
        payload = await self._request_json(
            STEAM_SEARCH_URL,
            params={
                "maxprice": "free",
                "specials": "1",
                "category1": "994,998,21",
                "json": "1",
                "l": "tchinese",
            },
        )
        if not isinstance(payload, dict):
            return None

        items = payload.get("items")
        if not isinstance(items, list) or len(items) > 200:
            logging.error("Steam 免費遊戲搜尋回傳格式不正確。")
            return None

        search_items: dict[int, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            app_id = self._extract_app_id(item.get("logo"))
            name = self._clean_text(item.get("name"), 200)
            if app_id is not None and name:
                search_items.setdefault(app_id, name)

        offers: list[SteamOffer] = []
        for app_id, name in search_items.items():
            offer = await self._fetch_offer(app_id, name)
            if offer is not None:
                offers.append(offer)

        return SteamFetchResult(
            active_app_ids=frozenset(search_items),
            offers=tuple(offers),
        )

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

    @staticmethod
    def _build_view(
        offer: SteamOffer,
        roles: Iterable[discord.Role] = (),
    ) -> discord.ui.LayoutView:
        safe_name = discord.utils.escape_markdown(offer.name)
        safe_description = discord.utils.escape_markdown(
            offer.description
            or "Steam 正在進行限時 100% 折扣，可免費加入收藏庫。"
        )
        safe_price = discord.utils.escape_markdown(offer.old_price) if offer.old_price else "—"
        safe_developers = (
            discord.utils.escape_markdown(", ".join(offer.developers))
            if offer.developers
            else "未提供"
        )

        heading = f"## Steam 限時免費領取\n### {safe_name}"
        role_mentions = " ".join(role.mention for role in roles)
        if role_mentions:
            heading = f"{role_mentions}\n{heading}"

        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(heading),
            discord.ui.TextDisplay(safe_description),
        ]
        if offer.header_image:
            children.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        media=offer.header_image,
                        description=f"{offer.name} Steam 商店圖片",
                    )
                )
            )

        children.extend(
            [
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**原價**　{safe_price}\n"
                    "**折扣**　100%\n"
                    f"**開發商**　{safe_developers}\n"
                    f"-# Steam App ID：{offer.app_id}"
                ),
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label="前往 Steam 領取",
                        style=discord.ButtonStyle.link,
                        url=offer.store_url,
                    )
                ),
            ]
        )

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            discord.ui.Container(
                *children,
                accent_colour=discord.Colour.from_rgb(27, 40, 56),
            )
        )
        return view

    async def _send_offer(
        self,
        channel: discord.TextChannel,
        offer: SteamOffer,
        roles: Iterable[discord.Role] = (),
    ) -> bool:
        selected_roles = tuple(roles)
        try:
            await channel.send(
                view=self._build_view(offer, selected_roles),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=list(selected_roles) if selected_roles else False,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Steam 免費遊戲 Discord 通知送出失敗。")
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
