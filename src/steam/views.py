from __future__ import annotations

from collections.abc import Iterable

import discord

from src.steam.provider import SteamOffer


def build_offer_view(
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
    description_item = discord.ui.TextDisplay(safe_description)
    children: list[discord.ui.Item] = [
        discord.ui.TextDisplay(heading),
        description_item,
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
    children.extend([
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
    ])
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            *children,
            accent_colour=discord.Colour.from_rgb(27, 40, 56),
        )
    )
    if view.content_length() > 4000:
        notice = "\n\n-# 說明已截短，完整內容請前往 Steam 查看。"
        description_budget = (
            4000 - (view.content_length() - len(description_item.content))
            - len(notice) - 1
        )
        prefix = safe_description[:max(0, description_budget)].rstrip("\\")
        description_item.content = f"{prefix}…{notice}"
    return view
