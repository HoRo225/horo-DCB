from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import discord

from src.ai.domain.formatting import discord_tables, split_markdown, split_reasoning_summary
from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, notice_view
from src.core.app.supervisor import TaskGroup

DISCORD_MESSAGE_LIMIT = 2_000
MAX_DISCORD_RESPONSE_CHUNKS = 8
PROGRESS_FALLBACK = ("thinking", "{name} 正在思考")
PROGRESS_OVERRIDES = {
    "searching": ("query", "{name} 正在搜尋網路"),
    "fallback": ("fallback", "{name} 正在切換備援模型"),
    "reconnecting": ("reconnect", "{name} 連線逾時，正在重新連線"),
}
ANSWER_MENTIONS = discord.AllowedMentions(replied_user=True)


def truncation_notice() -> str:
    return f"\n\n-# {ICONS['info']} 回覆過長，已截斷。"


def build_ai_native_image_links(image_urls: tuple[str, ...]) -> str:
    urls = list(image_urls)
    if not urls:
        return ""

    for omitted in range(len(urls) + 1):
        suffix = f"\n-# 另有 {omitted} 個圖片連結因訊息長度限制省略。" if omitted else ""
        content = "\n".join((f"-# {ICONS['image']} 圖片連結", *urls)) + suffix
        if len(content) <= DISCORD_MESSAGE_LIMIT:
            return content
        urls.remove(max(reversed(urls), key=len))
    raise AssertionError("image link message cannot fit Discord limit")


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
    def __init__(self, *, can_send: Any, deadline: float, name: str, cleanups: TaskGroup) -> None:
        self.message: discord.Message | None = None
        self.name = name
        self.can_send = can_send
        self.deadline = deadline
        self._cleanups = cleanups
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
            self._cleanups.spawn(_delete_when_landed(self._late), log=False)
            self._late = None



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


async def reply_notice(message: discord.Message, text: str, tone: str | None = "warn") -> None:
    try:
        await reply(message, **({"view": notice_view(text, tone)} if tone else {"content": text}))
    except discord.HTTPException:
        logging.exception("Failed to send AI notice reply.")


async def send_ai_answer(
    message: discord.Message,
    answer: str,
    *,
    image_urls: tuple[str, ...] = (),
    can_send: Any,
) -> str:
    chunks = split_markdown(
        discord_tables(answer), DISCORD_MESSAGE_LIMIT, MAX_DISCORD_RESPONSE_CHUNKS - bool(image_urls),
        truncation_notice(),
    )
    if link_message := build_ai_native_image_links(image_urls):
        chunks.append(link_message)
    if not chunks:
        return "unavailable"
    try:
        for index, chunk in enumerate(chunks):
            if not await can_send():
                return "revoked"
            if index == 0:
                await reply(message, chunk, allowed_mentions=ANSWER_MENTIONS)
            else:
                await message.channel.send(chunk)
    except discord.HTTPException, aiohttp.ClientError:
        logging.error("Failed to send Discord AI reply.")
        return "unavailable"
    return "success"
