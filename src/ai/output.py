from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import discord

from src.ai.formatting import (
    DISCORD_MESSAGE_LIMIT,
    DISCORD_TEXT_DISPLAY_LIMIT,
    MAX_DISCORD_RESPONSE_CHUNKS,
    MAX_TEXT_DISPLAY_RESPONSE_CHUNKS,
    build_ai_native_image_links,
    discord_tables,
    split_markdown,
    split_reasoning_summary,
)
from src.brand import BRAND_COLOUR, ICONS, notice_view
from src.tasks import track

PROGRESS_FALLBACK = ("thinking", "{name} 正在思考")
PROGRESS_OVERRIDES = {
    "searching": ("query", "{name} 正在搜尋網路"),
    "fallback": ("fallback", "{name} 正在切換備援模型"),
    "reconnecting": ("reconnect", "{name} 連線逾時，正在重新連線"),
}
ANSWER_MENTIONS = discord.AllowedMentions(replied_user=True)


def build_ai_text_display_view(
    content: str,
    *,
    image_urls: tuple[str, ...] = (),
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(content))
    if image_urls:
        gallery = discord.ui.MediaGallery()
        for url in image_urls:
            gallery.add_item(media=url)
        view.add_item(gallery)
    return view


def build_ai_progress_view(
    stage: str,
    summary_title: str = "",
    started_at: int | None = None,
    *,
    name: str,
) -> discord.ui.LayoutView:
    icon, title = PROGRESS_OVERRIDES.get(stage, PROGRESS_FALLBACK)
    content = f"**{ICONS[icon]} {discord.utils.escape_markdown(title.format(name=name))}** {ICONS['dots']}"
    if started_at is not None:
        content += f" · 開始於 <t:{started_at}:R>"
    if summary_title:
        content += f"\n-# {discord.utils.escape_markdown(summary_title)}"
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(content), accent_colour=BRAND_COLOUR))
    return view


class AiProgress:
    def __init__(self, *, can_send: Any, deadline: float, name: str) -> None:
        self.message: discord.Message | None = None
        self.name = name
        self.can_send = can_send
        self.deadline = deadline
        self.started_at = int(time.time())
        self._latest = ("generating", "")
        self._published = self._latest
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._late: asyncio.Future[discord.Message] | None = None
        self._failed = False

    async def start(self, source: discord.Message) -> None:
        if not await self.can_send():
            return
        self._late = asyncio.ensure_future(
            source.reply(view=build_ai_progress_view(*self._latest, self.started_at, name=self.name))
        )
        try:
            async with asyncio.timeout_at(
                min(self.deadline, asyncio.get_running_loop().time() + 3)
            ):
                self.message = await asyncio.shield(self._late)
        except discord.HTTPException, aiohttp.ClientError, TimeoutError:
            logging.error("Discord AI progress card undeliverable; falling back to plain reply.")
            return
        self._late = None
        self._task = asyncio.create_task(self._run())

    def update(self, stage: str, summary: str = "") -> None:
        stage = stage if stage in PROGRESS_OVERRIDES else "generating"
        latest = (stage, split_reasoning_summary(summary)[0] or "")
        if latest != self._latest:
            self._latest = latest
            self._changed.set()

    async def _run(self) -> None:
        last_edit = asyncio.get_running_loop().time()
        try:
            while True:
                await self._changed.wait()
                await asyncio.sleep(max(0, last_edit + 1 - asyncio.get_running_loop().time()))
                self._changed.clear()
                if self._latest == self._published:
                    continue
                async with asyncio.timeout_at(
                    min(self.deadline, asyncio.get_running_loop().time() + 3)
                ):
                    if not await self.can_send():
                        return
                    snapshot = self._latest
                    await self.message.edit(
                        view=build_ai_progress_view(*snapshot, self.started_at, name=self.name),
                    )
                self._published = snapshot
                last_edit = asyncio.get_running_loop().time()
        except discord.HTTPException, aiohttp.ClientError, TimeoutError:
            logging.error("Discord AI progress updates stopped.")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def fail(self, text: str) -> bool:
        if self.message is None:
            return False
        await self.message.edit(view=notice_view(text, "warn"))
        self._failed = True
        return True

    async def finish(self) -> None:
        await self.stop()
        if self._failed:
            return
        if self.message is not None:
            message, self.message = self.message, None
            await _delete_card(message)
        elif self._late is not None:
            track(CARD_CLEANUPS, asyncio.create_task(_delete_when_landed(self._late)))
            self._late = None


CARD_CLEANUPS: set[asyncio.Task[None]] = set()


async def _delete_card(message: discord.Message) -> None:
    try:
        async with asyncio.timeout(3):
            await message.delete()
    except discord.HTTPException, aiohttp.ClientError, TimeoutError:
        logging.error("Discord AI progress card cleanup failed.")


async def _delete_when_landed(send: asyncio.Future[discord.Message]) -> None:
    try:
        message = await asyncio.shield(send)
    except discord.HTTPException, aiohttp.ClientError, TimeoutError:
        return
    await _delete_card(message)


async def reply(message: discord.Message, content: str | None = None, **kwargs: Any) -> None:
    await message.channel.send(
        content, reference=message.to_reference(fail_if_not_exists=False), **kwargs
    )


async def reply_notice(message: discord.Message, text: str, text_display_enabled: bool, tone: str | None = "warn") -> None:
    try:
        await reply(
            message,
            **({"view": notice_view(text, tone)} if text_display_enabled and tone else {"content": text}),
        )
    except discord.HTTPException:
        logging.exception("Failed to send AI notice reply.")


async def _send_native_ai_chunks(
    message: discord.Message,
    chunks: list[str],
    *,
    reply_first: bool,
    image_urls: tuple[str, ...] = (),
    can_send: Any,
) -> str:
    link_message = build_ai_native_image_links(image_urls)
    if not chunks and not link_message:
        return "unavailable"

    try:
        start = 0
        sent_any = False
        if reply_first and chunks:
            if not await can_send():
                return "revoked"
            await reply(message, chunks[0], allowed_mentions=ANSWER_MENTIONS)
            start = 1
            sent_any = True

        for chunk in chunks[start:]:
            if not await can_send():
                return "revoked"
            await message.channel.send(chunk)
            sent_any = True

        if link_message:
            if not await can_send():
                return "revoked"
            if not sent_any and reply_first:
                await reply(message, link_message, allowed_mentions=ANSWER_MENTIONS)
            else:
                await message.channel.send(link_message)
            sent_any = True
    except discord.HTTPException, aiohttp.ClientError:
        logging.error("Failed to send Discord AI reply.")
        return "unavailable"
    return "success" if sent_any else "unavailable"


async def send_ai_answer(
    message: discord.Message,
    answer: str,
    *,
    image_urls: tuple[str, ...] = (),
    text_display_enabled: bool,
    can_send: Any,
) -> str:
    answer = discord_tables(answer)
    if not text_display_enabled:
        return await _send_native_ai_chunks(
            message,
            split_markdown(
                answer, DISCORD_MESSAGE_LIMIT, MAX_DISCORD_RESPONSE_CHUNKS - bool(image_urls)
            ),
            reply_first=True,
            image_urls=image_urls,
            can_send=can_send,
        )

    display_chunks = split_markdown(
        answer, DISCORD_TEXT_DISPLAY_LIMIT, MAX_TEXT_DISPLAY_RESPONSE_CHUNKS
    )
    sent_count = 0
    try:
        for index, chunk in enumerate(display_chunks):
            if not await can_send():
                return "revoked"
            view = build_ai_text_display_view(
                chunk,
                image_urls=image_urls if index == 0 else (),
            )
            if index == 0:
                await reply(message, view=view, allowed_mentions=ANSWER_MENTIONS)
            else:
                await message.channel.send(view=view)
            sent_count += 1
    except aiohttp.ClientError:
        logging.error("Failed to send Discord AI TextDisplay reply.")
        return "unavailable"
    except discord.HTTPException as exc:
        if exc.status != 400:
            logging.error("Discord AI TextDisplay reply undeliverable.")
            return "unavailable"
        logging.error("Failed to send Discord AI TextDisplay reply; falling back to plain text.")
        remaining = answer if not sent_count else "\n".join(display_chunks[sent_count:])
        return await _send_native_ai_chunks(
            message,
            split_markdown(
                remaining,
                DISCORD_MESSAGE_LIMIT,
                MAX_DISCORD_RESPONSE_CHUNKS - bool(image_urls if not sent_count else ()),
            ),
            reply_first=not sent_count,
            image_urls=image_urls if not sent_count else (),
            can_send=can_send,
        )
    return "success" if sent_count else "unavailable"
