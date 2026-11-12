import base64
from datetime import datetime, timedelta, timezone

import discord


TAIPEI = timezone(timedelta(hours=8))
IMAGE_SUFFIXES: dict[str, str] = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
IMAGE_PIXELS = 50_000_000


def stamp(moment: datetime) -> str:
    return moment.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")


def image_mime(content_type: str | None, filename: str) -> str | None:
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in IMAGE_SUFFIXES.values():
        return mime
    return IMAGE_SUFFIXES.get(filename.rsplit(".", 1)[-1].lower())


def exceeds_pixels(width: int | None, height: int | None) -> bool:
    return (width or 0) * (height or 0) > IMAGE_PIXELS


def image_input(mime: str, data: bytes) -> dict:
    return {"type": "image", "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}


def component_texts(components) -> list[str]:
    texts = []
    for component in components:
        if isinstance(component, discord.TextDisplay):
            texts.append(component.content)
        texts.extend(component_texts(getattr(component, "children", ())))
    return texts


def message_text(message: discord.Message) -> str:
    return "\n".join(text for text in [message.clean_content, *component_texts(message.components)] if text.strip())
