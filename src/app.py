from __future__ import annotations

import logging
from typing import Any

import discord

from src.ai.access import CodexAccess
from src.ai.client import CodexBridgeClient
from src.ai.media_executor import MediaExecutor
from src.bot import HoroBot
from src.calendar.discord import CalendarController
from src.calendar.manager import CalendarManager
from src.config import AppConfig
from src.steam.notifier import SteamFreeGamesNotifier
from src.voice.manager import TempVoiceManager


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = AppConfig.from_env()
    codex = CodexBridgeClient(
        "http://codex:8765",
        config.codex_bridge_token,
    )
    codex_access = CodexAccess(config.codex_enabled, config.codex_allowed_guild_id)
    temp_voice = TempVoiceManager()
    steam_free_games = SteamFreeGamesNotifier()
    controller: CalendarController

    def make_board(
        guild_name: str, events: Any
    ) -> tuple[discord.ui.LayoutView, list[discord.File]]:
        return controller.build_board(guild_name, events)

    calendar = CalendarManager(board_view_factory=make_board)
    controller = CalendarController(calendar)
    media_executor = MediaExecutor()
    HoroBot(
        codex,
        codex_access,
        temp_voice,
        steam_free_games,
        calendar=calendar,
        calendar_controller=controller,
        media_executor=media_executor,
        ai_text_display_enabled=config.ai_text_display_enabled,
        temp_voice_enabled=config.temp_voice_enabled,
        steam_free_games_enabled=config.steam_free_games_enabled,
    ).run(
        config.discord_token,
        log_handler=None,
    )
