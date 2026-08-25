from __future__ import annotations

import discord

DISCORD_MESSAGE_LIMIT = 2_000
MAX_DISCORD_RESPONSE_CHUNKS = 8
DISCORD_TEXT_DISPLAY_LIMIT = 4_000
MAX_TEXT_DISPLAY_RESPONSE_CHUNKS = 4
AI_RESPONSE_TRUNCATION_NOTICE = "\n\n（回覆過長，已截斷。）"


def _find_natural_split(text: str, max_chars: int) -> int:
    if len(text) <= max_chars:
        return len(text)
    if max_chars <= 0:
        return 0

    minimum = max_chars // 2
    window = text[:max_chars]
    for separator in ("\n\n", "\n", " "):
        index = window.rfind(separator, minimum)
        if index != -1:
            return index + len(separator)
    return max_chars


def _update_code_fence_language(text: str, language: str | None) -> str | None:
    current = language
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        if current is not None:
            if stripped == "```":
                current = None
            continue

        label = stripped[3:].strip()
        if (
            label
            and len(label) <= 32
            and all(char.isalnum() or char in "+-_.#" for char in label)
        ):
            current = label
        else:
            current = ""
    return current


def _code_fence_prefix(language: str | None) -> str:
    if language is None:
        return ""
    return f"```{language}\n"


def _split_discord_markdown(
    text: str,
    *,
    limit: int,
    max_chunks: int,
) -> list[str]:
    if not text:
        return []
    if limit <= 0 or max_chunks <= 0:
        raise ValueError("limit and max_chunks must be positive")
    if len(text) <= limit:
        return [text]
    if limit < len(AI_RESPONSE_TRUNCATION_NOTICE) + len("\n```"):
        max_length = limit * max_chunks
        if len(text) > max_length:
            notice = AI_RESPONSE_TRUNCATION_NOTICE[:max_length]
            text = text[: max_length - len(notice)] + notice
        return [text[index : index + limit] for index in range(0, len(text), limit)]

    chunks: list[str] = []
    remaining = text
    language: str | None = None

    while remaining and len(chunks) < max_chunks:
        prefix = _code_fence_prefix(language)
        final_language = _update_code_fence_language(remaining, language)
        final_suffix = "\n```" if final_language is not None else ""
        if len(prefix) + len(remaining) + len(final_suffix) <= limit:
            chunks.append(prefix + remaining + final_suffix)
            break

        is_last_chunk = len(chunks) == max_chunks - 1
        if is_last_chunk:
            notice = AI_RESPONSE_TRUNCATION_NOTICE
            raw_budget = max(0, limit - len(prefix) - len(notice))
            split_at = _find_natural_split(remaining, raw_budget)
            segment = remaining[:split_at]
            next_language = _update_code_fence_language(segment, language)
            suffix = "\n```" if next_language is not None else ""
            if len(prefix) + len(segment) + len(suffix) + len(notice) > limit:
                raw_budget = max(0, raw_budget - len(suffix))
                split_at = _find_natural_split(remaining, raw_budget)
                segment = remaining[:split_at]
                next_language = _update_code_fence_language(segment, language)
                suffix = "\n```" if next_language is not None else ""
            chunks.append(prefix + segment + suffix + notice)
            break

        raw_budget = limit - len(prefix)
        split_at = _find_natural_split(remaining, raw_budget)
        segment = remaining[:split_at]
        next_language = _update_code_fence_language(segment, language)
        suffix = "\n```" if next_language is not None else ""

        if len(prefix) + len(segment) + len(suffix) > limit:
            raw_budget = limit - len(prefix) - len(suffix)
            split_at = _find_natural_split(remaining, raw_budget)
            segment = remaining[:split_at]
            next_language = _update_code_fence_language(segment, language)
            suffix = "\n```" if next_language is not None else ""

        chunks.append(prefix + segment + suffix)
        remaining = remaining[split_at:]
        language = next_language

    return chunks


def split_discord_message(
    text: str,
    limit: int = DISCORD_MESSAGE_LIMIT,
) -> list[str]:
    if limit <= 0 or limit > DISCORD_MESSAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {DISCORD_MESSAGE_LIMIT}")
    return _split_discord_markdown(
        text,
        limit=limit,
        max_chunks=MAX_DISCORD_RESPONSE_CHUNKS,
    )


def split_discord_text_display(text: str) -> list[str]:
    return _split_discord_markdown(
        text,
        limit=DISCORD_TEXT_DISPLAY_LIMIT,
        max_chunks=MAX_TEXT_DISPLAY_RESPONSE_CHUNKS,
    )


def build_ai_text_display_view(
    content: str, images: tuple[object, ...] = ()
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(content))
    if images:
        view.add_item(
            discord.ui.MediaGallery(
                *[
                    discord.MediaGalleryItem(
                        media=image.url,
                        description=image.description,
                    )
                    for image in images
                ]
            )
        )
    return view
