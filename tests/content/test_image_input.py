import base64
import io

import pytest
from PIL import Image

from horo_dcb.content import image_input


def encode(image: Image.Image, image_format: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def animated_gif() -> bytes:
    frames = [Image.new("RGB", (8, 8), colour) for colour in ("red", "blue")]
    return encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)


@pytest.mark.parametrize(
    ("image_format", "mime"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")],
)
def test_supported_image_becomes_a_data_url(image_format, mime):
    data = encode(Image.new("RGB", (8, 8), "green"), image_format)
    item = image_input(data)
    assert item == {"type": "image", "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}


@pytest.mark.parametrize(
    "data",
    [animated_gif(), encode(Image.new("RGB", (8, 8), "green"), "BMP"), b"not an image", b""],
    ids=["animated-gif", "bmp", "garbage", "empty"],
)
def test_unsupported_image_is_rejected(data):
    assert image_input(data) is None
