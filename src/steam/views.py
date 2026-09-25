from __future__ import annotations

from collections.abc import Iterable
from typing import cast

import discord

from src.brand import BRAND_COLOUR
from src.discord_utils import truncate_discord_text
from src.steam.provider import SteamOffer


def offer_items(
    offer: SteamOffer,
    *,
    compact: bool = False,
    roles: Iterable[discord.Role] = (),
) -> list[discord.ui.Item]:
    safe_name = discord.utils.escape_markdown(offer.name)
    safe_price = discord.utils.escape_markdown(offer.old_price) if offer.old_price else "—"
    if compact:
        text = f"### {safe_name}\n原價　{safe_price}　·　折扣 100%"
        if offer.header_image:
            return [discord.ui.Section(
                text,
                accessory=discord.ui.Thumbnail(
                    offer.header_image,
                    description=f"{offer.name} Steam 商店圖片",
                ),
            )]
        return [discord.ui.TextDisplay(text)]

    heading = f"## Steam 限時免費領取\n### {safe_name}"
    role_mentions = " ".join(role.mention for role in roles)
    if role_mentions:
        heading = f"{role_mentions}\n{heading}"
    description = discord.ui.TextDisplay(discord.utils.escape_markdown(
        offer.description or "Steam 正在進行限時 100% 折扣，可免費加入收藏庫。"
    ))
    safe_developers = (
        discord.utils.escape_markdown(", ".join(offer.developers))
        if offer.developers else "未提供"
    )
    children: list[discord.ui.Item] = [discord.ui.TextDisplay(heading), description]
    if offer.header_image:
        children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(
            media=offer.header_image,
            description=f"{offer.name} Steam 商店圖片",
        )))
    children.extend((
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"**原價**　{safe_price}\n"
            "**折扣**　100%\n"
            f"**開發商**　{safe_developers}\n"
            f"-# Steam App ID：{offer.app_id}"
        ),
        discord.ui.ActionRow(discord.ui.Button(
            label="前往 Steam 領取",
            style=discord.ButtonStyle.link,
            url=offer.store_url,
        )),
    ))
    return children


def build_offer_view(
    offer: SteamOffer,
    roles: Iterable[discord.Role] = (),
) -> discord.ui.LayoutView:
    children = offer_items(offer, roles=roles)
    description_item = cast(discord.ui.TextDisplay, children[1])
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            *children,
            accent_colour=BRAND_COLOUR,
        )
    )
    if view.content_length() > 4000:
        notice = "\n\n-# 說明已截短，完整內容請前往 Steam 查看。"
        description_budget = (
            4000 - (view.content_length() - len(description_item.content))
            - len(notice) - 1
        )
        description_item.content = truncate_discord_text(
            description_item.content, description_budget, notice,
        )
    return view
