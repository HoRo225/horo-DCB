from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import discord

from src.ai.protocol import MAX_PROMPT_CHARACTERS


def clean_bot_mention(content: str, bot_user_id: int) -> str:
    return content.replace(f"<@{bot_user_id}>", "").replace(f"<@!{bot_user_id}>", "").strip()


def message_mentions_bot(message: Any, bot_user_id: int) -> bool:
    return any(getattr(user, "id", None) == bot_user_id for user in message.mentions)


def visible_message_text(message: Any) -> str:
    parts: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    def visit(components: object) -> None:
        if not isinstance(components, (list, tuple)):
            return
        for component in components:
            if isinstance(component, discord.TextDisplay):
                add(component.content)
            elif isinstance(
                component,
                (discord.Container, discord.components.SectionComponent),
            ):
                visit(getattr(component, "children", ()))

    add(getattr(message, "content", ""))
    visit(getattr(message, "components", ()))
    return "\n".join(parts)


async def get_referenced_message(message: Any) -> Any | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None

    reference_channel_id = getattr(reference, "channel_id", None)
    current_channel_id = getattr(getattr(message, "channel", None), "id", None)
    if (
        reference_channel_id is not None
        and current_channel_id is not None
        and reference_channel_id != current_channel_id
    ):
        return None

    resolved = getattr(reference, "resolved", None)
    if resolved is not None and (
        getattr(resolved, "author", None) is not None or hasattr(resolved, "attachments")
    ):
        return resolved

    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        return None

    try:
        return await asyncio.wait_for(message.channel.fetch_message(message_id), 2)
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
        aiohttp.ClientError,
        TimeoutError,
    ):
        return None


def build_codex_prompt(question: str, referenced_message: Any | None) -> str:
    if referenced_message is None:
        return question
    referenced = visible_message_text(referenced_message)
    question = question or "請說明我回覆的內容。"
    prefix = "【被回覆的訊息】\n"
    middle = "\n\n【本次問題】\n"
    note = (
        "\n\n（圖片輸入若存在，順序為被回覆訊息在前、本次訊息在後；"
        "動畫圖片由左到右依時間順序取樣。）"
    )
    question = question[
        : max(
            0,
            MAX_PROMPT_CHARACTERS - len(prefix) - len(middle) - len(note),
        )
    ]
    available = max(
        0,
        MAX_PROMPT_CHARACTERS - len(prefix) - len(middle) - len(question) - len(note),
    )
    referenced = referenced[:available]
    return f"{prefix}{referenced}{middle}{question}{note}"
