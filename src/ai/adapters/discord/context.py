from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import aiohttp
import discord

from src.ai.adapters.media.images import SUPPORTED_IMAGE_TYPES, ImageAttachmentError
from src.ai.app.settings import AiSettings
from src.ai.domain.conversation import MAX_PROMPT_CHARACTERS, conversation_key, is_discord_media_url
from src.core.adapters.discord.channels import member_role_ids

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"})
_CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_~]+:([1-9][0-9]*)>")


def require_short(content: str) -> None:
    if len(content) > MAX_PROMPT_CHARACTERS:
        raise ImageAttachmentError(f"問題最多 {MAX_PROMPT_CHARACTERS:,} 個字元，請縮短後再試。")


def clean_bot_mention(content: str, bot_user_id: int) -> str:
    return content.replace(f"<@{bot_user_id}>", "").replace(f"<@!{bot_user_id}>", "").strip()


def message_mentions_bot(message: Any, bot_user_id: int) -> bool:
    return any(getattr(user, "id", None) == bot_user_id for user in message.mentions)


def allowed_channel_id(channel: Any) -> int:
    return channel.parent_id if isinstance(channel, discord.Thread) else channel.id


def conversation_access(message: Any, settings: AiSettings, *, author: Any | None = None) -> tuple[str | None, str | None]:
    channel = message.channel
    author = message.author if author is None else author
    reason = settings.denial_reason(message.guild.id, allowed_channel_id(channel), member_role_ids(author))
    if reason is not None:
        return None, reason
    return conversation_key(message.guild.id, channel.id, author.id, is_thread=isinstance(channel, discord.Thread)), None


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
    if isinstance(resolved, discord.DeletedReferencedMessage):
        return None
    if resolved is not None and (
        getattr(resolved, "author", None) is not None or hasattr(resolved, "attachments")
    ):
        return resolved

    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        return None

    try:
        return await asyncio.wait_for(message.channel.fetch_message(message_id), 2)
    except discord.HTTPException, aiohttp.ClientError, TimeoutError:
        return None


def _attachment_kind(attachment: Any) -> str | None:
    content_type = getattr(attachment, "content_type", None)
    filename = getattr(attachment, "filename", "")
    extension = os.path.splitext(filename)[1].lower() if isinstance(filename, str) else ""
    if isinstance(content_type, str) and content_type.startswith("video/"):
        return "video"
    is_image_like = (
        isinstance(content_type, str) and content_type.startswith("image/")
    ) or extension in IMAGE_EXTENSIONS
    if not is_image_like:
        return None
    if (
        content_type not in SUPPORTED_IMAGE_TYPES
        or extension not in SUPPORTED_IMAGE_TYPES[content_type]
    ):
        return "broken"
    return "media"


def _discord_media_url(value: Any) -> str | None:
    for name in ("url", "proxy_url"):
        url = getattr(value, name, None)
        if isinstance(url, str) and is_discord_media_url(url):
            return url
    return None


def select_message_media(
    messages: list[Any],
    *,
    seen: set[str] | None = None,
) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    seen = set() if seen is None else seen

    def add(kind: str, url: str | None, key: str | None = None) -> None:
        if url is None or not is_discord_media_url(url):
            return
        identity = key or url
        if identity not in seen:
            seen.add(identity)
            selected.append((kind, url))

    def visit_components(components: object) -> None:
        if not isinstance(components, (list, tuple)):
            return
        for component in components:
            if isinstance(component, discord.components.MediaGalleryComponent):
                for item in component.items:
                    media = getattr(item, "media", None)
                    attachment_id = getattr(media, "attachment_id", None)
                    add(
                        "media",
                        _discord_media_url(media),
                        f"attachment:{attachment_id}" if type(attachment_id) is int else None,
                    )
            elif isinstance(component, discord.Container):
                visit_components(getattr(component, "children", ()))

    for message in messages:
        for attachment in getattr(message, "attachments", ()):
            kind = _attachment_kind(attachment)
            if kind is not None:
                add(kind, _discord_media_url(attachment), f"attachment:{attachment.id}")

        content = getattr(message, "content", "")
        if isinstance(content, str):
            for match in _CUSTOM_EMOJI.finditer(content):
                emoji = discord.PartialEmoji.from_str(match.group(0))
                add("media", str(emoji.url), f"emoji:{emoji.id}")

        for embed in getattr(message, "embeds", ()):
            video = _discord_media_url(getattr(embed, "video", None))
            if video is not None:
                add("video", video)
            else:
                add(
                    "media",
                    _discord_media_url(getattr(embed, "image", None))
                    or _discord_media_url(getattr(embed, "thumbnail", None)),
                )

        for sticker in getattr(message, "stickers", ()):
            format_name = getattr(getattr(sticker, "format", None), "name", "").casefold()
            raw_url = getattr(sticker, "url", None)
            sticker_id = getattr(sticker, "id", None)
            add(
                "broken" if format_name == "lottie" else "media",
                str(raw_url) if raw_url is not None else None,
                f"sticker:{sticker_id}" if sticker_id is not None else None,
            )

        visit_components(getattr(message, "components", ()))
    return selected


def select_media(messages: list[Any]) -> list[list[tuple[str, str]]]:
    seen: set[str] = set()
    return [select_message_media([message], seen=seen) for message in messages]
