import asyncio
import contextlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import ui
from discord.utils import escape_markdown

from . import log_event
from .store import Post, Promotion, Settings

if TYPE_CHECKING:
    from .bot import HoroBot


SEARCH_URL = "https://store.steampowered.com/search/results/"
ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
SEARCH_PARAMS = {"maxprice": "free", "specials": "1", "category1": "998", "json": "1", "cc": "tw", "l": "tchinese"}
ASSET_ROOT = "https://shared.akamai.steamstatic.com/store_item_assets/"
APP_IN_LOGO = re.compile(r"/apps/(\d+)/")
STEAM_EMOJI = "<:steamlogo:1570464394452668569>"
LIVE_COLOUR = 0x7F77DD
ENDED_COLOUR = 0x80848E
INTERVAL = 900
REQUEST_TIMEOUT = 30


@dataclass(frozen=True)
class Offer:
    package: int
    app: int
    name: str
    description: str
    header: str | None
    price: str
    ends: int | None


class Health(Enum):
    OK = auto()
    UNREACHABLE = auto()
    UNSENDABLE = auto()


class Edit(Enum):
    DONE = auto()
    GONE = auto()
    RETRY = auto()


def _integer(value) -> int:
    if type(value) is not int:
        raise ValueError("steam_invalid")
    return value


def _text(value) -> str:
    if not isinstance(value, str):
        raise ValueError("steam_invalid")
    return value


def search_apps(data: dict) -> set[int]:
    return {int(match.group(1)) for item in data["items"] if (match := APP_IN_LOGO.search(_text(item["logo"])))}


def header_url(assets: dict | None) -> str | None:
    if not assets or not assets.get("header") or not assets.get("asset_url_format"):
        return None
    return ASSET_ROOT + _text(assets["asset_url_format"]).replace("${FILENAME}", _text(assets["header"]))


def parse_items(data: dict) -> dict[int, Offer]:
    offers = {}
    for item in data["response"]["store_items"]:
        if item["success"] != 1:
            continue
        option = item.get("best_purchase_option") or {}
        if item.get("type") != 0 or option.get("is_free_to_keep") is not True or option.get("discount_pct") != 100 or int(option.get("original_price_in_cents") or 0) <= 0:
            continue
        ends = option.get("free_to_keep_ends")
        offer = Offer(
            package=_integer(option["packageid"]),
            app=_integer(item["appid"]),
            name=_text(item["name"]),
            description=_text((item.get("basic_info") or {}).get("short_description") or ""),
            header=header_url(item.get("assets")),
            price=_text(option["formatted_original_price"]),
            ends=None if ends is None else _integer(ends),
        )
        offers[offer.package] = offer
    return offers


def items_query(apps: Iterable[int]) -> dict:
    return {
        "ids": [{"appid": app} for app in sorted(apps)],
        "context": {"language": "tchinese", "country_code": "TW", "steam_realm": 1},
        "data_request": {"include_basic_info": True, "include_assets": True, "include_all_purchase_options": True},
    }


def store_button(label: str, app: int) -> ui.ActionRow:
    return ui.ActionRow(ui.Button(label=label, url=f"https://store.steampowered.com/app/{app}/"))


def live_card(offer: Offer, roles: Iterable[discord.abc.Snowflake]) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    mentions = " ".join(f"<@&{role.id}>" for role in roles)
    if mentions:
        view.add_item(ui.TextDisplay(mentions))
    deadline = "" if offer.ends is None else f" · <t:{offer.ends}:f> 截止（<t:{offer.ends}:R>）"
    parts: list[ui.Item] = [] if offer.header is None else [ui.MediaGallery(discord.MediaGalleryItem(offer.header))]
    parts.append(ui.TextDisplay(f"## {STEAM_EMOJI} {escape_markdown(offer.name)}\n-# 限時免費 · ~~{offer.price}~~{deadline}"))
    if offer.description:
        parts.append(ui.TextDisplay(escape_markdown(offer.description)))
    view.add_item(ui.Container(*parts, accent_colour=LIVE_COLOUR))
    view.add_item(store_button("前往 Steam 領取", offer.app))
    return view


def ended_card(promotion: Promotion) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    deadline = "" if promotion.ends is None else f" · <t:{promotion.ends}:f> 截止"
    view.add_item(ui.Container(ui.TextDisplay(f"## {STEAM_EMOJI} {escape_markdown(promotion.name)}\n-# 限免已結束 · 原價 {promotion.price}{deadline}"), accent_colour=ENDED_COLOUR))
    view.add_item(store_button("Steam 商店頁", promotion.app))
    return view


def roles_in(channel: discord.abc.GuildChannel, settings: Settings) -> list[discord.Role]:
    return [role for role in map(channel.guild.get_role, sorted(settings.steam_role_ids)) if role is not None]


class Steam:
    def __init__(self, bot: HoroBot, search_url: str = SEARCH_URL, items_url: str = ITEMS_URL) -> None:
        self.bot = bot
        self.search_url = search_url
        self.items_url = items_url
        self.health = Health.OK
        self.checked_at: int | None = None
        self.lock = asyncio.Lock()
        self.loop: asyncio.Task | None = None

    def start(self) -> None:
        if self.loop is None:
            self.loop = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self.loop is not None:
            self.loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.loop

    async def _run(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(INTERVAL)

    async def check(self) -> None:
        async with self.lock:
            try:
                await self._converge()
            except Exception as error:
                log_event("steam_check_error", error)
        await self.bot.console.refresh_panels()

    async def _converge(self) -> None:
        settings = await self.bot.store.settings()
        records = self.bot.store.promotions()
        live = {package: (record, record.post) for package, record in records.items() if record.post is not None}
        channel_id = settings.steam_channel_id
        if channel_id is None and not live:
            return
        try:
            offers = await self._fetch(channel_id is not None, {record.app for record, _ in live.values()})
        except Exception as error:
            log_event("steam_fetch_error", error)
            self.health = Health.UNREACHABLE
            return
        for package, (record, post) in live.items():
            await self._follow(package, record, post, offers.get(package), settings)
        health = Health.OK
        if channel_id is not None:
            health = await self._announce(channel_id, settings, [offer for package, offer in offers.items() if package not in records])
        self.health = health
        self.checked_at = int(time.time())

    async def _fetch(self, search: bool, apps: set[int]) -> dict[int, Offer]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
            if search:
                async with session.get(self.search_url, params=SEARCH_PARAMS) as response:
                    response.raise_for_status()
                    apps = apps | search_apps(await response.json(content_type=None))
            if not apps:
                return {}
            async with session.get(self.items_url, params={"input_json": json.dumps(items_query(apps))}) as response:
                response.raise_for_status()
                return parse_items(await response.json(content_type=None))

    async def _follow(self, package: int, record: Promotion, post: Post, offer: Offer | None, settings: Settings) -> None:
        if offer is not None and offer.ends == record.ends:
            return
        if offer is None:
            view, updated = ended_card(record), replace(record, post=None)
        else:
            channel = self.bot.get_channel(post.channel)
            roles = roles_in(channel, settings) if isinstance(channel, discord.TextChannel) else []
            view, updated = live_card(offer, roles), Promotion(offer.app, offer.name, offer.price, offer.ends, post)
        match await self._edit(post, view):
            case Edit.DONE:
                await self.bot.store.save_promotion(package, updated)
            case Edit.GONE:
                await self.bot.store.save_promotion(package, replace(updated, post=None))

    async def _edit(self, post: Post, view: ui.LayoutView) -> Edit:
        try:
            await self.bot.get_partial_messageable(post.channel).get_partial_message(post.message).edit(view=view)
        except (discord.NotFound, discord.Forbidden) as error:
            log_event("steam_edit_error", error)
            return Edit.GONE
        except discord.HTTPException as error:
            log_event("steam_edit_error", error)
            return Edit.RETRY
        return Edit.DONE

    async def _announce(self, channel_id: int, settings: Settings, fresh: list[Offer]) -> Health:
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            log_event("steam_channel_missing")
            return Health.UNSENDABLE
        roles = roles_in(channel, settings)
        health = Health.OK
        for offer in fresh:
            try:
                message = await channel.send(view=live_card(offer, roles), allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=roles))
            except discord.HTTPException as error:
                log_event("steam_send_error", error)
                health = Health.UNSENDABLE
                continue
            await self.bot.store.save_promotion(offer.package, Promotion(offer.app, offer.name, offer.price, offer.ends, Post(channel.id, message.id)))
        return health
