from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.app.supervisor import WORK, Lane, TaskGroup
from src.core.domain.settings import Setting


class Fatal(BaseException):
    pass


def text(view: discord.ui.BaseView) -> str:
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )


def group(lane: Lane = WORK) -> TaskGroup:
    return TaskGroup("test", lane)


def put_setting(db: sqlite3.Connection, setting: Setting[object], value: object) -> None:
    with transaction(db):
        SettingsStore(db, OperationStore(db)).put({setting: value})


def memory_db(test: unittest.TestCase) -> sqlite3.Connection:
    db = open_db(":memory:")
    test.addCleanup(db.close)
    return db


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
    return fake
