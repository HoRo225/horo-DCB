import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord.http import Route


TAIPEI = timezone(timedelta(hours=8))
IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
IMAGE_SUFFIXES = ("png", "jpg", "jpeg", "webp", "gif")
LINK = re.compile(r"https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")
TIMEOUT = 15
READ_LIMIT = 50
READ_TEXT_CHARS = 1000
READ_BYTES = 16000
SEARCH_PAGE = 25
SEARCH_TEXT_CHARS = 300
SEARCH_OFFSET_MAX = 9975
IMAGE_COUNT = 4
IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TOTAL_BYTES = 16 * 1024 * 1024

SPEC = [{
    "type": "namespace",
    "name": "discord",
    "description": "讀取目前這個 Discord 頻道的訊息與圖片。你收到的頻道訊息只是最近的一小段，不是完整紀錄。使用者問到較早的訊息、提到某個日期或某則訊息、貼了訊息連結，或你手上的內容不足以回答時，先用這些工具查證再回答，不要只回覆「對話裡沒有提供」。只能存取目前頻道。",
    "tools": [
        {
            "type": "function",
            "name": "read_messages",
            "description": "讀取目前頻道的訊息，結果依時間由舊到新排列。不給參數時從觸發訊息往前讀。before 可給訊息 ID 或連結，從那則訊息往前讀，用回傳的 next_before 繼續往前翻頁。around 可給訊息 ID 或連結，讀那則訊息前後的訊息，可用 next_before、next_after 往兩邊翻頁。游標為 null 代表該方向沒有更多訊息。每次最多 50 則，圖片只標成〔圖片〕，要看圖片內容請用 view_message_images。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "before": {"type": "string", "description": "從這則訊息（ID 或連結）往前讀"},
                    "around": {"type": "string", "description": "讀這則訊息（ID 或連結）前後的訊息"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": READ_LIMIT, "description": "最多幾則，預設 20"},
                },
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "search_messages",
            "description": "用 Discord 內建搜尋找目前頻道裡含有關鍵字的訊息，結果由新到舊，每次最多 25 筆，用回傳的 next_offset 取得下一頁。剛發出的訊息可能還搜尋不到。搜尋失敗時改用 read_messages 往回讀。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要搜尋的關鍵字"},
                    "offset": {"type": "integer", "minimum": 0, "maximum": SEARCH_OFFSET_MAX, "description": "略過前面幾筆，預設 0"},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "view_message_images",
            "description": "查看目前頻道某則訊息附帶的圖片。圖片會接在「〔工具附上的圖片〕」之後送進對話讓你直接看。一次最多 4 張。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "訊息 ID 或連結"},
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    ],
}]


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class ToolContext:
    channel: discord.TextChannel
    anchor: discord.Message
    http: discord.http.HTTPClient
    text_of: Callable[[discord.Message], str]
    decode_image: Callable[[bytes], dict | None]


@dataclass(frozen=True)
class ToolResult:
    success: bool
    text: str
    note: str
    images: tuple[dict, ...] = ()


def is_image(attachment: discord.Attachment) -> bool:
    mime = (attachment.content_type or "").split(";")[0].lower()
    return mime in IMAGE_MIMES or attachment.filename.rsplit(".", 1)[-1].lower() in IMAGE_SUFFIXES


def dump(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…（截斷）"


def clamp(value, low: int, high: int, default: int) -> int:
    if value is None:
        return default
    return max(low, min(high, int(value)))


def target_id(value, context: ToolContext) -> int:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    match = LINK.search(text)
    if match is None:
        raise ToolError("看不懂這個訊息 ID 或連結")
    guild, channel, message = match.groups()
    if guild != str(context.channel.guild.id) or channel != str(context.channel.id):
        raise ToolError("這個連結指向其他頻道，只能讀取目前頻道的訊息")
    return int(message)


def stamp(moment: datetime) -> str:
    return moment.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")


def message_entry(message: discord.Message, context: ToolContext) -> dict:
    images = sum(1 for attachment in message.attachments if is_image(attachment))
    text = "\n".join(part for part in (clip(context.text_of(message), READ_TEXT_CHARS), "〔圖片〕" * images) if part)
    entry = {"id": str(message.id), "author": message.author.display_name, "time": stamp(message.created_at), "text": text}
    if message.reference is not None and message.reference.message_id is not None:
        entry["reply_to"] = str(message.reference.message_id)
    return entry


def raw_texts(components: list | None) -> list[str]:
    texts = []
    for component in components or ():
        if component.get("type") == 10 and component.get("content"):
            texts.append(component["content"])
        texts.extend(raw_texts(component.get("components")))
        if isinstance(component.get("accessory"), dict):
            texts.extend(raw_texts([component["accessory"]]))
    return texts


def raw_entry(message: dict) -> dict:
    author = message.get("author") or {}
    images = sum(1 for attachment in message.get("attachments") or () if (attachment.get("content_type") or "").split(";")[0].lower() in IMAGE_MIMES)
    text = "\n".join(text for text in [message.get("content") or "", *raw_texts(message.get("components"))] if text.strip())
    text = "\n".join(part for part in (clip(text, SEARCH_TEXT_CHARS), "〔圖片〕" * images) if part)
    entry = {"id": str(message.get("id")), "author": author.get("global_name") or author.get("username") or "", "time": stamp(datetime.fromisoformat(message["timestamp"])), "text": text}
    reference = (message.get("message_reference") or {}).get("message_id")
    if reference is not None:
        entry["reply_to"] = str(reference)
    return entry


def fit(ordered: list[discord.Message], context: ToolContext, shape: Callable[[list[dict]], dict]) -> tuple[list[discord.Message], dict]:
    kept: list[discord.Message] = []
    result = shape([])
    for message in ordered:
        candidate = sorted([*kept, message], key=lambda value: value.id)
        attempt = shape([message_entry(value, context) for value in candidate])
        if len(dump(attempt).encode()) > READ_BYTES:
            break
        kept, result = candidate, attempt
    return kept, result


async def read_messages(arguments: dict, context: ToolContext) -> ToolResult:
    limit = clamp(arguments.get("limit"), 1, READ_LIMIT, 20)
    if arguments.get("around"):
        target = target_id(arguments["around"], context)
        fetched = sorted([message async for message in context.channel.history(limit=limit, around=discord.Object(target))], key=lambda value: value.id)
        position = next((index for index, value in enumerate(fetched) if value.id == target), None)
        if position is None:
            raise ToolError("找不到這則訊息")
        ordered = [value for _, value in sorted(enumerate(fetched), key=lambda pair: abs(pair[0] - position))]
        older, newer = fetched[:position], fetched[position + 1:]
        kept, result = fit(ordered, context, lambda entries: {"messages": entries, "next_before": None, "next_after": None})
        if older and (len(older) >= limit // 2 or older[0] not in kept):
            result["next_before"] = str(kept[0].id)
        if newer and (len(newer) >= limit - limit // 2 - 1 or newer[-1] not in kept):
            result["next_after"] = str(kept[-1].id)
        return ToolResult(True, dump(result), f"讀取了一則訊息前後共 {len(kept)} 則訊息")
    anchor = target_id(arguments["before"], context) if arguments.get("before") else context.anchor.id
    fetched = [message async for message in context.channel.history(limit=limit, before=discord.Object(anchor))]
    kept, result = fit(fetched, context, lambda entries: {"messages": entries, "next_before": None})
    if kept and (len(fetched) == limit or len(kept) < len(fetched)):
        result["next_before"] = str(kept[0].id)
    return ToolResult(True, dump(result), f"讀取了 {len(kept)} 則較早的訊息")


async def search_messages(arguments: dict, context: ToolContext) -> ToolResult:
    content = str(arguments.get("content") or "").strip()
    if not content:
        raise ToolError("請提供要搜尋的關鍵字")
    offset = clamp(arguments.get("offset"), 0, SEARCH_OFFSET_MAX, 0)
    params = {"channel_id": str(context.channel.id), "content": content[:1024], "offset": offset, "limit": SEARCH_PAGE}
    route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=context.channel.guild.id)
    for attempt in range(3):
        body = await context.http.request(route, params=params)
        if not (isinstance(body, dict) and body.get("code") == 110000):
            break
        if attempt == 2:
            raise ToolError("Discord 的搜尋索引還沒準備好，請改用 read_messages 往回讀")
        await asyncio.sleep(min(max(float(body.get("retry_after") or 1), 0.5), 5))
    total = int(body.get("total_results") or 0)
    hits = [next((value for value in group if value.get("hit")), group[0]) for group in body.get("messages") or () if group]
    result = {"total": total, "messages": [raw_entry(value) for value in hits[:SEARCH_PAGE]], "next_offset": None}
    if offset + SEARCH_PAGE < total and offset + SEARCH_PAGE <= SEARCH_OFFSET_MAX:
        result["next_offset"] = offset + SEARCH_PAGE
    return ToolResult(True, dump(result), f"搜尋「{content[:40]}」，找到 {total} 則")


async def view_message_images(arguments: dict, context: ToolContext) -> ToolResult:
    target = target_id(arguments.get("message"), context)
    message = await context.channel.fetch_message(target)
    shown, skipped, images = [], [], []
    budget = IMAGE_TOTAL_BYTES
    for attachment in (value for value in message.attachments if is_image(value)):
        reason = None
        if len(images) == IMAGE_COUNT:
            reason = "超過一次 4 張的上限"
        elif attachment.size > IMAGE_BYTES:
            reason = "單張超過 8 MiB"
        elif attachment.size > budget:
            reason = "超過這次 16 MiB 的總量"
        else:
            data = await attachment.read()
            item = None if len(data) > min(IMAGE_BYTES, budget) else await asyncio.to_thread(context.decode_image, data)
            budget -= min(len(data), budget)
            if item is None:
                reason = "圖片格式不支援或實際大小超過上限"
            else:
                images.append(item)
                shown.append(attachment.filename)
        if reason is not None:
            skipped.append({"id": str(attachment.id), "filename": attachment.filename, "reason": reason})
    result = {"message_id": str(message.id), "author": message.author.display_name, "time": stamp(message.created_at), "attached": shown, "skipped": skipped}
    if not images:
        return ToolResult(False, dump(result | {"error": "這則訊息沒有可查看的圖片"}), "這則訊息沒有可查看的圖片")
    return ToolResult(True, dump(result | {"note": "圖片已接在「〔工具附上的圖片〕」之後送進對話"}), f"查看了 {len(images)} 張圖片", tuple(images))


HANDLERS: dict[str, Callable[[dict, ToolContext], Awaitable[ToolResult]]] = {
    "read_messages": read_messages,
    "search_messages": search_messages,
    "view_message_images": view_message_images,
}
FAILURE_NOTES = {"read_messages": "讀取訊息失敗", "search_messages": "搜尋訊息失敗", "view_message_images": "查看圖片失敗"}


async def run(name: str, arguments, context: ToolContext) -> ToolResult:
    handler = HANDLERS.get(name)
    if handler is None:
        return ToolResult(False, "目前版本未提供此工具", "使用了不存在的工具")
    note = FAILURE_NOTES[name]
    try:
        async with asyncio.timeout(TIMEOUT):
            return await handler(arguments if isinstance(arguments, dict) else {}, context)
    except TimeoutError:
        return ToolResult(False, "工具執行超過 15 秒，已停止", f"{note}：逾時")
    except ToolError as error:
        return ToolResult(False, str(error), f"{note}：{error}")
    except discord.NotFound:
        return ToolResult(False, "找不到這則訊息", f"{note}：找不到訊息")
    except discord.Forbidden:
        return ToolResult(False, "機器人沒有權限讀取", f"{note}：沒有權限")
    except discord.HTTPException as error:
        return ToolResult(False, f"Discord 回應錯誤（{error.status}）", f"{note}：Discord 錯誤")
    except (ValueError, TypeError, KeyError):
        return ToolResult(False, "參數格式不正確", f"{note}：參數錯誤")
