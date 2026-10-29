from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Sequence

import aiohttp
import discord

BRAND_COLOUR = discord.Colour(0x7E6BD6)
GOLD_COLOUR = discord.Colour(0xE8C45C)
WARN_COLOUR = discord.Colour(0xE8833A)
ERROR_COLOUR = discord.Colour(0xE05A5A)
LOSS_COLOUR = discord.Colour(0x9C6B9E)
NEUTRAL_COLOUR = discord.Colour(0x80848E)

ICONS = {
    "overview": "🏠",
    "ai": "🤖",
    "voice": "🎙️",
    "steam": "🎮",
    "games": "🎲",
    "blackjack": "🃏",
    "slots": "🎰",
    "models": "🎛️",
    "refresh": "🔄",
    "save": "💾",
    "prev": "◀️",
    "next": "▶️",
    "settings": "⚙️",
    "sync": "🔁",
    "fallback": "🔀",
    "query": "🔍",
    "closed": "🔒",
    "leave": "🚪",
    "cancel": "🚫",
    "gift": "🎁",
    "info": "ℹ️",
    "ok": "✅",
    "warn": "⚠️",
    "error": "❌",
    "status": "📊",
    "access": "🔐",
    "thinking": "🧠",
    "tech": "🛠️",
    "image": "🖼️",
    "reconnect": "🔌",
    "deadline": "⏰",
    "dotok": "🟢",
    "dotwarn": "🟠",
    "dotoff": "⚪",
    "hit": "➕",
    "stand": "✋",
    "manage": "➡️",
    "back": "⬅️",
    "dots": "…",
    "rules": "📖",
}
APP_EMOJIS: dict[str, str] = {}
WHITE: dict[str, str] = {}
_TONE_EMOJIS = {"warn": "lowarn", "error": "lrerror"}
_TONE_COLOURS = {"warn": WARN_COLOUR, "error": ERROR_COLOUR, "deadline": NEUTRAL_COLOUR}
_brand_urls: dict[str, str | None] = {"avatar": None}


def sync_discord_brand(client: discord.Client) -> None:
    if client.user is not None:
        _brand_urls["avatar"] = str(client.user.display_avatar)


async def load_app_emojis(client: discord.Client) -> None:
    try:
        async with asyncio.timeout(3):
            emojis = await client.fetch_application_emojis()
    except (discord.HTTPException, aiohttp.ClientError, TimeoutError):
        logging.exception("Emoji load failed; icons, cards and slot symbols fall back to text.")
        return
    APP_EMOJIS.update({emoji.name: str(emoji) for emoji in emojis})
    for key in ICONS:
        if found := APP_EMOJIS.get(_TONE_EMOJIS.get(key, f"lg{key}")):
            ICONS[key] = found
            if white := APP_EMOJIS.get(f"lw{key}"):
                WHITE[found] = white
    logging.info("Loaded %d application emojis.", len(APP_EMOJIS))


def tint(emoji: str, style: discord.ButtonStyle) -> str:
    if style in (discord.ButtonStyle.primary, discord.ButtonStyle.danger):
        return WHITE.get(emoji, emoji)
    return emoji


def bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    names = [
        f"bar{'gold' if n < filled else 'empty'}{'l' if n == 0 else 'r' if n == width - 1 else 'm'}"
        for n in range(width)
    ]
    if all(name in APP_EMOJIS for name in names):
        return "".join(APP_EMOJIS[name] for name in names)
    return "▰" * filled + "▱" * (width - filled)


def rana_says(line: str, name: str) -> str:
    name = f"**{discord.utils.escape_markdown(name)}**"
    head = f"{APP_EMOJIS['rana']} {name}" if "rana" in APP_EMOJIS else name
    return "\n".join(f"> {part}" for part in f"{head}　{line}".split("\n"))


def bot_avatar() -> str | None:
    return _brand_urls["avatar"]


def tone_colour(tone: str, colour: discord.Colour = BRAND_COLOUR) -> discord.Colour:
    return _TONE_COLOURS.get(tone, colour)


def note_text(note: tuple[str, str]) -> str:
    tone, text = note
    return f"{ICONS[tone]} {text}"


def notice_view(
    text: str,
    tone: str = "warn",
    *,
    colour: discord.Colour = BRAND_COLOUR,
    items: Sequence[discord.ui.Item] = (),
    title: str | None = None,
    icon: str | None = None,
) -> discord.ui.LayoutView:
    icon = icon or ICONS[tone]
    body = f"**{icon} {title}**\n{text}" if title else f"{icon} {text}"
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(body), accent_colour=tone_colour(tone, colour)))
    if items:
        view.add_item(discord.ui.ActionRow(*items))
    return view


async def send_notice(
    interaction: discord.Interaction,
    text: str,
    tone: str = "warn",
    *,
    colour: discord.Colour = BRAND_COLOUR,
    items: Sequence[discord.ui.Item] = (),
    title: str | None = None,
    icon: str | None = None,
) -> None:
    view = notice_view(text, tone, colour=colour, items=items, title=title, icon=icon)
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


async def report_interaction_error(interaction: discord.Interaction, error: Exception) -> None:
    logging.error("Unexpected error while handling interaction.", exc_info=error)
    try:
        await send_notice(
            interaction, "請稍後再試；若持續發生，請聯絡管理員。", "error", title="發生未預期的錯誤"
        )
    except discord.HTTPException:
        logging.exception("Failed to send error notice.")


def reported(callback):
    @functools.wraps(callback)
    async def wrapper(self, interaction: discord.Interaction) -> None:
        try:
            await callback(self, interaction)
        except Exception as error:
            await report_interaction_error(interaction, error)

    return wrapper


class ErrorNotice:
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item | None = None,
        /,
    ) -> None:
        await report_interaction_error(interaction, error)


def card_title(
    heading: str,
    subtitle: str = "",
    *,
    thumbnail: str | None = None,
    accessory: discord.ui.Button | None = None,
    note: str = "",
) -> discord.ui.Item:
    text = f"## {heading}" + (f"\n-# {subtitle}" if subtitle else "") + (f"\n{note}" if note else "")
    if thumbnail:
        accessory = discord.ui.Thumbnail(thumbnail)
    if accessory is None:
        return discord.ui.TextDisplay(text)
    return discord.ui.Section(text, accessory=accessory)


def info_card(*items: discord.ui.Item, colour: discord.Colour = BRAND_COLOUR) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_colour=colour))
    return view


def full_rules(title: str, sections: Sequence[tuple[str, str]]) -> discord.ui.LayoutView:
    items: list[discord.ui.Item] = [card_title(title)]
    for heading, text in sections:
        items += [discord.ui.Separator(), discord.ui.TextDisplay(f"### {heading}\n{text}")]
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_colour=BRAND_COLOUR))
    return view
