from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, card_title, reported, send_notice
from src.steam.models import SteamOffer, offer_from_payload

if TYPE_CHECKING:
    from src.bot import HoroBot


def truncate_discord_text(text: str, prefix_limit: int, suffix: str) -> str:
    prefix = text[: max(0, prefix_limit)].rstrip("\\")
    return f"{prefix}…{suffix}"


def _price(offer: SteamOffer) -> str:
    if offer.old_price:
        return f"~~{discord.utils.escape_markdown(offer.old_price)}~~ → **免費**"
    return "**免費**"


def _deadline(offer: SteamOffer) -> str:
    if offer.ends_at is None:
        return ""
    return f"\n{ICONS['deadline']} <t:{offer.ends_at}:f> 截止（<t:{offer.ends_at}:R>）"


def offer_summary(offer: SteamOffer) -> discord.ui.Item:
    text = f"**{discord.utils.escape_markdown(offer.name)}**\n{_price(offer)}{_deadline(offer)}"
    if offer.header_image:
        return discord.ui.Section(
            text,
            accessory=discord.ui.Thumbnail(
                offer.header_image,
                description=f"{offer.name} Steam 商店圖片",
            ),
        )
    return discord.ui.TextDisplay(text)


def build_offer_view(offer: SteamOffer, roles: Iterable[discord.Role] = ()) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    if role_mentions := " ".join(role.mention for role in roles):
        view.add_item(discord.ui.TextDisplay(role_mentions))
    description = discord.ui.TextDisplay(
        discord.utils.escape_markdown(offer.description or "Steam 正在進行限時 100% 折扣，可免費加入收藏庫。")
    )
    small = ["限時免費"]
    if offer.old_price:
        small.append(f"~~{discord.utils.escape_markdown(offer.old_price)}~~")
    if offer.ends_at is not None:
        small.append(f"<t:{offer.ends_at}:f> 截止（<t:{offer.ends_at}:R>）")
    children: list[discord.ui.Item] = []
    if offer.header_image:
        children.append(discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=offer.header_image, description=f"{offer.name} Steam 商店圖片")
        ))
    children += [
        card_title(f"{ICONS['steam']} {discord.utils.escape_markdown(offer.name)}", " · ".join(small)),
        description,
    ]
    view.add_item(discord.ui.Container(*children, accent_colour=BRAND_COLOUR))
    view.add_item(discord.ui.ActionRow(discord.ui.Button(label="前往 Steam 領取", emoji=ICONS["gift"], url=offer.store_url)))
    if view.content_length() > 4000:
        notice = "\n\n說明已截短，完整內容請前往 Steam 查看。"
        description_budget = 4000 - (view.content_length() - len(description.content)) - len(notice) - 1
        description.content = truncate_discord_text(description.content, description_budget, notice)
    return view


class SteamClaim(discord.ui.DynamicItem[discord.ui.Button], template=r"horo:steam:claim:(?P<app>[0-9]+)"):
    def __init__(self, app_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="我領了", emoji=ICONS["ok"], style=discord.ButtonStyle.secondary,
            custom_id=f"horo:steam:claim:{app_id}",
        ))
        self.app_id = app_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /) -> SteamClaim:
        return cls(int(match["app"]))

    @reported
    async def callback(self, interaction: discord.Interaction) -> None:
        bot: HoroBot = interaction.client
        message, guild = interaction.message, bot.get_guild(interaction.guild_id)
        effect = next((effect for effect in bot.steam_free_games.operations.effects(status="done", kind="steam.announce")
                       if message is not None and effect.message_id == message.id), None)
        if effect is not None:
            roles = [role for role_id in effect.payload["role_ids"] if guild and (role := guild.get_role(role_id))]
            await interaction.response.edit_message(view=build_offer_view(offer_from_payload(effect.payload["offer"]), roles),
                                                    allowed_mentions=discord.AllowedMentions.none())
        await send_notice(interaction, "這個按鈕已停用，請直接按「前往 Steam 領取」。", "info")
