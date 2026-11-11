import base64
import io
import warnings
from datetime import datetime, timedelta, timezone

import discord
from PIL import Image, UnidentifiedImageError


TAIPEI = timezone(timedelta(hours=8))
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
IMAGE_SUFFIXES = ("png", "jpg", "jpeg", "webp", "gif")


def stamp(moment: datetime) -> str:
    return moment.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")


def is_image(attachment: discord.Attachment) -> bool:
    mime = (attachment.content_type or "").split(";")[0].lower()
    return mime in IMAGE_TYPES.values() or attachment.filename.rsplit(".", 1)[-1].lower() in IMAGE_SUFFIXES


def image_input(data: bytes) -> dict | None:
    try:
        with warnings.catch_warnings(record=True):
            with Image.open(io.BytesIO(data)) as image:
                mime = IMAGE_TYPES.get(image.format)
                if mime is None or (image.format == "GIF" and getattr(image, "is_animated", False)):
                    return None
        return {"type": "image", "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def component_texts(components) -> list[str]:
    texts = []
    for component in components:
        if isinstance(component, discord.TextDisplay):
            texts.append(component.content)
        texts.extend(component_texts(getattr(component, "children", ())))
    return texts


def message_text(message: discord.Message) -> str:
    return "\n".join(text for text in [message.clean_content, *component_texts(message.components)] if text.strip())
