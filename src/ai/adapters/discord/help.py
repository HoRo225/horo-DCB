from __future__ import annotations

from collections.abc import Mapping

import discord

from src.ai.app.ports import GamesChannel
from src.core.adapters.discord.brand import ICONS, card_title, info_card
from src.core.app.actions import ActionGroup, ActionSpec, Query
from src.core.app.ports import Actor

HINT = "help 是使用者問你能做什麼或要功能說明時。"
HELP_WORDS = frozenset({"說明", "你會什麼", "你會什麼？", "你會什麼?"})


def help_view(games_channel_id: int | None) -> discord.ui.LayoutView:
    games = f"<#{games_channel_id}>" if games_channel_id else "遊戲頻道"
    return info_card(card_title(f"{ICONS['ai']} 樂奈能幫你做的事", "直接 @樂奈 用說的就行"),
                     discord.ui.TextDisplay(f"{ICONS['games']} 21 點、拉霸要到 {games} 玩"))


def help_actions(games: GamesChannel) -> ActionGroup:
    async def show_help(actor: Actor) -> Mapping[str, object]:
        return {"view": help_view(games.channel_id), "attachments": []}

    return ActionGroup("help", (ActionSpec("help", {}, admin=False, effect=Query("功能說明", show_help)),), hint=HINT)
