from __future__ import annotations

import discord

from src.core.adapters.discord.brand import ICONS, card_title, info_card
from src.core.adapters.discord.live import name_text

HELP_WORDS = frozenset({"說明", "你會什麼", "你會什麼？", "你會什麼?"})


def help_view(games_channel_id: int | None, bot_name: str) -> discord.ui.LayoutView:
    games = f"<#{games_channel_id}>" if games_channel_id else "遊戲頻道"
    name = discord.utils.escape_markdown(bot_name)
    return info_card(card_title(f"{ICONS['ai']} {name_text('{name}能幫你做的事', name)}", f"直接 @{name} 聊天"),
                     discord.ui.TextDisplay(f"{ICONS['games']} 21 點、拉霸要到 {games} 玩"))
