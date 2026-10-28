from __future__ import annotations

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, card_title


def info_card(*items: discord.ui.Item) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_colour=BRAND_COLOUR))
    return view


def help_view(games_channel_id: int | None) -> discord.ui.LayoutView:
    games = f"<#{games_channel_id}>" if games_channel_id else "遊戲頻道"
    return info_card(card_title(f"{ICONS['ai']} 樂奈能幫你做的事", "直接 @樂奈 用說的就行"),
                     discord.ui.TextDisplay(f"{ICONS['games']} 21 點、拉霸要到 {games} 玩"))
