from __future__ import annotations

import re

import discord

from src.brand import ICONS

DISCORD_MESSAGE_LIMIT = 2_000
MAX_DISCORD_RESPONSE_CHUNKS = 8
DISCORD_TEXT_DISPLAY_LIMIT = 4_000
MAX_TEXT_DISPLAY_RESPONSE_CHUNKS = 4


def truncation_notice() -> str:
    return f"\n\n-# {ICONS['info']} 回覆過長，已截斷。"


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
        if label and len(label) <= 32 and all(char.isalnum() or char in "+-_.#" for char in label):
            current = label
        else:
            current = ""
    return current


def _cut(text: str, language: str | None, budget: int) -> tuple[int, str | None, str]:
    split_at = _find_natural_split(text, budget)
    next_language = _update_code_fence_language(text[:split_at], language)
    return split_at, next_language, "\n```" if next_language is not None else ""


def split_markdown(text: str, limit: int, max_chunks: int) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    language: str | None = None

    while remaining and len(chunks) < max_chunks:
        prefix = "" if language is None else f"```{language}\n"
        final_language = _update_code_fence_language(remaining, language)
        final_suffix = "\n```" if final_language is not None else ""
        if len(prefix) + len(remaining) + len(final_suffix) <= limit:
            chunks.append(prefix + remaining + final_suffix)
            break

        notice = truncation_notice() if len(chunks) == max_chunks - 1 else ""
        budget = max(0, limit - len(prefix) - len(notice))
        split_at, next_language, suffix = _cut(remaining, language, budget)
        if len(prefix) + split_at + len(suffix) + len(notice) > limit:
            split_at, next_language, suffix = _cut(
                remaining, language, max(0, budget - len(suffix))
            )
        chunks.append(prefix + remaining[:split_at] + suffix + notice)
        if notice:
            break
        remaining = remaining[split_at:]
        language = next_language

    return chunks


_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")
_TABLE_INLINE_CHARS = 80


_SPOILER = re.compile(r"\|\|(?=\S)[^|\n]*?(?<=\S)\|\|")
_CELL_PIPE = re.compile(r"(?<!\\)\|")


def _mask_spoilers(line: str) -> tuple[str, list[str]]:
    spoilers = _SPOILER.findall(line)
    return _SPOILER.sub("\0", line), spoilers


def _is_table_row(line: str) -> bool:
    masked = _mask_spoilers(line)[0].lstrip()
    return not masked.startswith("||") and _CELL_PIPE.search(masked) is not None


def _table_cells(line: str) -> list[str]:
    masked, spoilers = _mask_spoilers(line.strip())
    spoiler_iter = iter(spoilers)
    cells = _CELL_PIPE.split(masked.removeprefix("|").removesuffix("|"))
    return [
        re.sub("\0", lambda _match: next(spoiler_iter), cell).strip().replace("\\|", "|")
        for cell in cells
    ]


def _table_as_list(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = []
    for row in rows:
        label = row[0].replace("**", "")
        pairs = [(header, cell) for header, cell in zip(headers[1:], row[1:]) if cell]
        lead = f"- **{label}**" if label else "-"
        if sum(len(header) + len(cell) for header, cell in pairs) <= _TABLE_INLINE_CHARS:
            body = " · ".join(f"{header} {cell}" for header, cell in pairs)
            lines.append(f"{lead}：{body}" if label and body else f"{lead} {body}".rstrip())
        else:
            lines.append(lead)
            lines.extend(f"  - {header}：{cell}" for header, cell in pairs)
    return lines


def discord_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    fenced = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            fenced = not fenced
        elif (
            not fenced
            and _is_table_row(line)
            and index + 1 < len(lines)
            and _TABLE_SEPARATOR.match(lines[index + 1])
            and len(_table_cells(line)) == len(_table_cells(lines[index + 1]))
        ):
            start = index
            headers = _table_cells(line)
            piped = line.lstrip().startswith("|")
            index += 2
            rows = []
            while (
                index < len(lines)
                and _is_table_row(lines[index])
                and lines[index].lstrip().startswith("|") == piped
                and len(cells := _table_cells(lines[index])) == len(headers)
            ):
                rows.append(cells)
                index += 1
            malformed = (
                index < len(lines)
                and _is_table_row(lines[index])
                and lines[index].lstrip().startswith("|") == piped
            )
            out.extend(lines[start:index] if not rows or malformed else _table_as_list(headers, rows))
            continue
        out.append(line)
        index += 1
    return "\n".join(out)


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


def split_reasoning_summary(summary: str) -> tuple[str | None, str]:
    cleaned = "\n".join(
        line for line in summary.strip().splitlines() if line.strip() != "<!-- -->"
    ).strip()
    if not cleaned.startswith("**"):
        return None, cleaned
    closing = cleaned.find("**", 2)
    if closing == -1 and "\n" not in cleaned:
        return cleaned[2:].rstrip("*").strip() or None, ""
    if closing <= 2:
        return None, cleaned
    remainder = cleaned[closing + 2 :]
    if remainder and not remainder.startswith(("\n", "\r")):
        return None, cleaned
    return cleaned[2:closing].strip() or None, remainder.strip()


_ERROR_TEXT = {
    "busy": "{name} 目前忙碌，請稍後再試。",
    "not_allowed": "{name} 目前沒有在這個頻道或你的身分組開放。",
    "not_allowed_channel": "這個頻道不能找{name}聊天，請到 {channels} 再 @{name}。",
    "not_allowed_role": "你目前沒有使用{name} AI 的身分組，需要的話請找管理員。",
    "auth_required": "{name} 尚未完成登入，請聯絡管理員。",
    "timeout": "{name} 回覆逾時，請稍後再試。",
    "model_capacity": "目前模型滿載，請稍後再試。",
    "usage_limit_or_unavailable": "{name} 額度已用盡或服務暫時無法使用，請稍後再試。",
    "model_configuration_invalid": "AI 模型設定目前無法使用，請聯絡管理員檢查模型與推理強度。",
}


def codex_error_text(code: str, name: str, *, channels: str = "") -> str:
    text = _ERROR_TEXT.get(code, "{name} 暫時無法回覆，請稍後再試。")
    return text.format(name=discord.utils.escape_markdown(name), channels=channels)
