from __future__ import annotations

import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.games import rana
from src.games.blackjack import Card
from src.games.store import GameStore
from src.state import open_db, transaction


class Fatal(BaseException):
    pass


def text(view: discord.ui.BaseView) -> str:
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )


def deck(*ranks: str) -> list[Card]:
    return [Card(rank, "S") for rank in reversed(ranks)]


def memory_db(test: unittest.TestCase) -> sqlite3.Connection:
    db = open_db(":memory:")
    test.addCleanup(db.close)
    return db


def seat(store: GameStore, user_id: int, game: str = "blackjack", message_id: int = 20) -> None:
    with transaction(store.db):
        store.replace_table(user_id, game, 10, message_id)
    store.advance_state(user_id)


def quiet_rana(test: unittest.IsolatedAsyncioTestCase, ask: str | None = None) -> AsyncMock:
    ask_once = AsyncMock(return_value=ask)
    for patcher in (patch("src.games.rana.ask_once", ask_once), patch("src.games.rana.ready", return_value=True)):
        patcher.start()
        test.addCleanup(patcher.stop)
    test.addAsyncCleanup(lambda: asyncio.gather(*rana.TASKS, return_exceptions=True))
    return ask_once


def interaction() -> MagicMock:
    fake = MagicMock()
    fake.response.is_done = MagicMock(return_value=False)

    def respond(*args: object, **kwargs: object) -> None:
        if fake.response.is_done():
            raise discord.InteractionResponded(fake)
        fake.response.is_done.return_value = True

    fake.response.defer = AsyncMock(side_effect=respond)
    fake.response.send_message = AsyncMock(side_effect=respond)
    fake.response.edit_message = AsyncMock(side_effect=respond)
    fake.followup.send = AsyncMock()
    fake.edit_original_response = AsyncMock()
    fake.delete_original_response = AsyncMock()
    fake.client.unavailable_reason.return_value = None
    return fake

