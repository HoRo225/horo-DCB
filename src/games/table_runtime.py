from __future__ import annotations

import logging

import discord

from src.games.store import GameStore
from src.games.tables import LIVE_TABLES, TABLES, SeatView


async def open_table(interaction: discord.Interaction, game: str) -> None:
    operations = interaction.client.game_operations
    state = operations.snapshot(interaction.user.id)
    await operations.execute(interaction, "open_table", game=game, expected_token=state["state_token"], ui=True)


async def delete_message(client: discord.Client, channel_id: int, message_id: int) -> None:
    try:
        await client.get_partial_messageable(channel_id).get_partial_message(message_id).delete()
    except discord.NotFound:
        pass
    except discord.HTTPException:
        logging.warning("Game message %s could not be deleted.", message_id)


async def dispatch_table(interaction: discord.Interaction) -> None:
    if interaction.type is not discord.InteractionType.component or interaction.message is None:
        return
    if interaction.data["custom_id"].startswith("horo:game:rules:"):
        return
    if (table := LIVE_TABLES.get(interaction.message.id)) is not None:
        await table.click(interaction)


def restore_tables(client: discord.Client, store: GameStore) -> list[SeatView]:
    restored = []
    for user_id, channel_id, message_id, game in store.reopen_tables():
        if (table_class := TABLES.get(game)) is None:
            store.close_table(user_id)
            logging.warning("Game table %s has unknown game %r and was closed.", message_id, game)
            continue
        table = table_class(store, user_id)
        table.message = client.get_partial_messageable(channel_id).get_partial_message(message_id)
        LIVE_TABLES[message_id] = table
        restored.append(table)
    return restored
