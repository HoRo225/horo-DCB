from __future__ import annotations

import logging
from pathlib import Path

import aiohttp
import discord

BRAND_DIR = Path("data/brand")
BANNER_FILENAME = "lona-banner.png"
CARD_FILENAME = "lona-card.png"
BRAND_COLOUR = discord.Colour.from_rgb(142, 170, 153)
_discord_urls: dict[str, str | None] = {
    BANNER_FILENAME: None,
    CARD_FILENAME: None,
}


def set_discord_brand_urls(*, card_url: str, banner_url: str | None) -> bool:
    urls = {
        CARD_FILENAME: card_url,
        BANNER_FILENAME: banner_url,
    }
    changed = urls != _discord_urls
    _discord_urls.update(urls)
    return changed


async def sync_discord_brand(client: discord.Client) -> bool:
    user = client.user
    if user is None:
        return False
    try:
        profile = await client.fetch_user(user.id)
    except discord.HTTPException, aiohttp.ClientError, TimeoutError:
        logging.warning("Discord 品牌素材同步失敗，沿用目前素材。")
        return False
    return set_discord_brand_urls(
        card_url=str(profile.display_avatar),
        banner_url=str(profile.banner) if profile.banner is not None else None,
    )


def brand_url(filename: str) -> str | None:
    return _discord_urls.get(filename)


def _brand_source(filename: str) -> str | None:
    if url := brand_url(filename):
        return url
    return f"attachment://{filename}" if (BRAND_DIR / filename).is_file() else None


def brand_file(filename: str) -> discord.File | None:
    if brand_url(filename) is not None:
        return None
    path = BRAND_DIR / filename
    return discord.File(path, filename=filename) if path.is_file() else None


def brand_files(*filenames: str) -> list[discord.File]:
    return [file for name in filenames if (file := brand_file(name)) is not None]


def banner() -> discord.ui.MediaGallery | None:
    source = _brand_source(BANNER_FILENAME)
    if source is None:
        return None
    return discord.ui.MediaGallery(
        discord.MediaGalleryItem(
            source,
            description="樂奈在柔霧綠與淡粉花朵間微笑的品牌插畫",
        )
    )


def branded_title(heading: str, subtitle: str) -> discord.ui.Item:
    text = f"# {heading}" + (f"\n-# {subtitle}" if subtitle else "")
    source = _brand_source(CARD_FILENAME)
    if source is None:
        return discord.ui.TextDisplay(text)
    return discord.ui.Section(
        text,
        accessory=discord.ui.Thumbnail(
            source,
            description="手持小月曆的樂奈",
        ),
    )
