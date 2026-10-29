from __future__ import annotations

import re

_MENTION = re.compile(r"@(everyone|here|[!&]?[0-9]{17,20})")


def escape_mentions(text: str) -> str:
    return _MENTION.sub("@\u200b\\1", text)


def clean_line(text: str | None, limit: int) -> str | None:
    text = escape_mentions((text or "").strip())
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text or None


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


def split_markdown(text: str, limit: int, max_chunks: int, notice: str) -> list[str]:
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

        tail = notice if len(chunks) == max_chunks - 1 else ""
        budget = max(0, limit - len(prefix) - len(tail))
        split_at, next_language, suffix = _cut(remaining, language, budget)
        if len(prefix) + split_at + len(suffix) + len(tail) > limit:
            split_at, next_language, suffix = _cut(
                remaining, language, max(0, budget - len(suffix))
            )
        chunks.append(prefix + remaining[:split_at] + suffix + tail)
        if tail:
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


def reasoning_title(summary: str) -> str:
    cleaned = "\n".join(
        line for line in summary.strip().splitlines() if line.strip() != "<!-- -->"
    ).strip()
    if not cleaned.startswith("**"):
        return ""
    closing = cleaned.find("**", 2)
    if closing == -1 and "\n" not in cleaned:
        return cleaned[2:].rstrip("*").strip()
    if closing <= 2:
        return ""
    remainder = cleaned[closing + 2 :]
    if remainder and not remainder.startswith(("\n", "\r")):
        return ""
    return cleaned[2:closing].strip()
