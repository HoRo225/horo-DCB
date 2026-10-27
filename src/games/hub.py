from __future__ import annotations

import logging
from uuid import uuid4

import discord

from src.cards import render
from src.discord_utils import post_problem, text_channel_problem
from src.games.cards import banner_file, banner_png
from src.games.store import GameStore
from src.games.views import lobby_view
from src.operations import OperationStore
from src.state import transaction


async def _banner() -> discord.File | None:
    try:
        await render(banner_png)
    except Exception:
        logging.exception("Game hub banner could not be drawn.")
        return None
    return banner_file()


async def refresh_hub(client: discord.Client, store: GameStore) -> None:
    hub = store.hub_message
    if not hub:
        return
    message = client.get_partial_messageable(hub[0]).get_partial_message(hub[1])
    try:
        if file := await _banner():
            try:
                await message.edit(view=lobby_view(), attachments=[file])
                return
            except discord.Forbidden:
                logging.warning("Game lobby banner was refused; refreshing without it.")
        await message.edit(view=lobby_view(banner=False), attachments=[])
    except discord.HTTPException:
        logging.warning("Published game lobby could not be refreshed.")


async def bind_channel(guild: discord.Guild, operations, channel: object | None, *,
                       operation_id: str | None = None, still_current=None) -> str | None:
    if problem := text_channel_problem(channel, guild):
        return problem
    if still_current is not None and not still_current():
        return "操作權限或期限已改變，未更新遊戲頻道。"
    operations.store.set_channel(channel.id, operation_id=f"{operation_id}:channel" if operation_id else None)
    if problem := await operations.publish_hub(guild, operation_id=operation_id, still_current=still_current):
        return f"已設定遊戲頻道，但{problem}"
    return None


def channel_problem(guild: discord.Guild, store: GameStore) -> str | None:
    return post_problem(guild, guild.get_channel(store.channel_id), "遊戲頻道")


async def _send_hub(guild: discord.Guild, channel: discord.TextChannel, *,
                    still_current=None) -> discord.Message | None:
    if channel.permissions_for(guild.me).attach_files and (file := await _banner()):
        if still_current is not None and not still_current():
            return None
        try:
            return await channel.send(view=lobby_view(), file=file)
        except discord.Forbidden:
            logging.warning("Game lobby banner was refused; posting without it.")
    if still_current is not None and not still_current():
        return None
    return await channel.send(view=lobby_view(banner=False))


async def publish_hub(guild: discord.Guild, store: GameStore, *,
                      operation_id: str | None = None, still_current=None) -> str | None:
    expected_channel_id = store.channel_id
    async with store._hub_lock:
        return await _publish_hub_locked(guild, store, expected_channel_id=expected_channel_id,
                                         operation_id=operation_id, still_current=still_current)


async def _publish_hub_locked(guild, store, *, expected_channel_id, operation_id, still_current):
    channel_id = store.channel_id
    channel = guild.get_channel(channel_id) if channel_id is not None else None
    if channel is None:
        return "請先選擇遊戲頻道。"
    current = lambda: store.channel_id == expected_channel_id and (still_current is None or still_current())
    if not current():
        return "操作權限或期限已改變，未發送遊戲大廳。"
    records = OperationStore(store.db)
    base = operation_id or uuid4()
    effect_id = f"{base}:game-hub"
    with transaction(store.db):
        if records.get_effect(effect_id) is not None:
            return "此遊戲大廳發送已有紀錄，請在待處理項目核對。"
        records.create_effect(effect_id, "game_hub_publish", channel.id, {"previous_hub": store.hub_message}, operation_id=operation_id)
        records.update_effect(effect_id, "unknown")
    try:
        message = await _send_hub(guild, channel, still_current=current)
    except discord.HTTPException:
        logging.exception("Game hub could not be published.")
        return "無法在遊戲頻道發送訊息，請確認 Bot 的檢視頻道與傳送訊息權限。"
    if message is None:
        with transaction(store.db):
            records.update_effect(effect_id, "failed", error="revoked_before_send")
        return "操作權限或設定已改變，未發送遊戲大廳。"
    if not current():
        try:
            await message.delete()
        except discord.HTTPException:
            logging.warning("Superseded game hub message could not be deleted.")
            with transaction(store.db):
                records.update_effect(effect_id, "unknown", message_id=message.id, error="superseded_cleanup_failed")
        else:
            with transaction(store.db):
                records.update_effect(effect_id, "skipped", message_id=message.id, error="superseded")
        return None
    old = store.hub_message
    store.set_hub(channel.id, message.id)
    with transaction(store.db):
        records.update_effect(effect_id, "done", message_id=message.id)
    await _delete_previous(guild, old, "game hub")
    return None


async def _delete_previous(guild: discord.Guild, old: list[int] | None, what: str) -> None:
    old_channel = guild.get_channel(old[0]) if old else None
    if old_channel is not None:
        try:
            await old_channel.get_partial_message(old[1]).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logging.warning("Previous %s message could not be deleted.", what)
