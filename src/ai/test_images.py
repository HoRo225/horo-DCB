from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from PIL import Image

from src.ai.frames import extract_frames
from src.ai.images import (
    ImageAttachmentError,
    MediaBudget,
    _download_discord_media,
    _image_type,
    _needs_frames,
    describe_media,
    read_message_media,
    select_message_media,
)
from src.codex.protocol import MAX_IMAGE_BYTES

CDN = "https://cdn.discordapp.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 8
WEBP = b"RIFF\0\0\0\0WEBPVP8 "
EMOJI_ID = 1100000000000
ANIMATED_EMOJI_ID = 1200000000000


def _encode(frames: list[Image.Image], fmt: str) -> bytes:
    out = io.BytesIO()
    frames[0].save(out, fmt, save_all=len(frames) > 1, append_images=frames[1:], duration=100)
    return out.getvalue()


def _colors(*names: str) -> list[Image.Image]:
    return [Image.new("RGB", (64, 48), name) for name in names]


def _pixel(frame: bytes, xy: tuple[int, int] = (5, 5)) -> tuple[int, ...]:
    return Image.open(io.BytesIO(frame)).getpixel(xy)


def _write_video(path: str, codec: str, names: tuple[str, ...]) -> None:
    import av

    with av.open(path, "w") as container:
        stream = container.add_stream(codec, rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for image in _colors(*names):
            for _ in range(5):
                container.mux(stream.encode(av.VideoFrame.from_image(image)))
        container.mux(stream.encode())


def _attachment(id: int, filename: str, content_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        filename=filename,
        content_type=content_type,
        proxy_url=f"https://media.discordapp.net/attachments/1/{id}/{filename}",
        url=f"{CDN}/attachments/1/{id}/{filename}",
    )


def _sticker(id: int, format_name: str, ext: str = "png") -> SimpleNamespace:
    return SimpleNamespace(
        id=id, format=SimpleNamespace(name=format_name), url=f"{CDN}/stickers/{id}.{ext}"
    )


def _message(**fields: object) -> SimpleNamespace:
    defaults = {"attachments": (), "content": "", "embeds": (), "stickers": (), "components": ()}
    return SimpleNamespace(**(defaults | fields))


class SelectMessageMediaTest(unittest.TestCase):
    def test_kinds_and_dedupe(self) -> None:
        seen: set[str] = set()
        message = _message(
            attachments=(
                _attachment(1, "a.png", "image/png"),
                _attachment(2, "notes.txt", "text/plain"),
                _attachment(3, "a.gif", "image/gif"),
                _attachment(4, "clip.mp4", "video/mp4"),
            ),
            content=f"<:ok:{EMOJI_ID}> <:ok:{EMOJI_ID}> <a:dance:{ANIMATED_EMOJI_ID}>",
            stickers=(_sticker(21, "png"), _sticker(22, "apng"), _sticker(23, "lottie", "json")),
            embeds=(
                SimpleNamespace(
                    video=SimpleNamespace(proxy_url=f"{CDN}/v.mp4", url=None),
                    image=None,
                    thumbnail=SimpleNamespace(proxy_url=f"{CDN}/t.png", url=None),
                ),
            ),
        )
        self.assertEqual(
            select_message_media([message], seen=seen),
            [
                ("media", f"{CDN}/attachments/1/1/a.png"),
                ("media", f"{CDN}/attachments/1/3/a.gif"),
                ("video", f"{CDN}/attachments/1/4/clip.mp4"),
                ("media", f"{CDN}/emojis/{EMOJI_ID}.png"),
                ("media", f"{CDN}/emojis/{ANIMATED_EMOJI_ID}.webp?animated=true"),
                ("video", f"{CDN}/v.mp4"),
                ("media", f"{CDN}/stickers/21.png"),
                ("media", f"{CDN}/stickers/22.png"),
                ("lottie", f"{CDN}/stickers/23.json"),
            ],
        )
        self.assertEqual(select_message_media([message], seen=seen), [])

    def test_unsupported_image_attachments_are_broken(self) -> None:
        message = _message(
            attachments=(
                _attachment(1, "a.svg", "image/svg+xml"),
                _attachment(2, "a.heic", "image/heic"),
                _attachment(3, "a.jpg", "image/png"),
                _attachment(4, "b.png", "image/png"),
            )
        )
        self.assertEqual(
            [kind for kind, _url in select_message_media([message])],
            ["broken", "broken", "broken", "media"],
        )


class ImageBytesTest(unittest.TestCase):
    def test_detects_type_from_signature(self) -> None:
        self.assertEqual(_image_type(PNG), "image/png")
        self.assertEqual(_image_type(JPEG), "image/jpeg")
        self.assertEqual(_image_type(WEBP), "image/webp")
        self.assertEqual(_image_type(b"GIF89a" + b"\0" * 8), "image/gif")
        self.assertIsNone(_image_type(b"<svg></svg>"))

    def test_needs_frames(self) -> None:
        still, animated = _colors("red"), _colors("red", "blue")
        self.assertTrue(_needs_frames("image/gif", _encode(still, "GIF")))
        self.assertTrue(_needs_frames("image/png", _encode(animated, "PNG")))
        self.assertFalse(_needs_frames("image/png", _encode(still, "PNG")))
        self.assertTrue(_needs_frames("image/webp", _encode(animated, "WEBP")))
        self.assertFalse(_needs_frames("image/webp", _encode(still, "WEBP")))
        self.assertFalse(_needs_frames("image/jpeg", _encode(still, "JPEG")))

    def test_budget_quota_and_describe(self) -> None:
        budget = MediaBudget(images_left=2)
        budget.data_url("image/png", PNG)
        self.assertEqual(budget.images_left, 1)
        note = describe_media([("靜態圖", ("a",)), ("動畫", ("b", "c", "d", "e"))], 3, 1, 2)
        self.assertEqual(
            note,
            "\n（圖 2–5：動畫依時間順序取樣的 4 格；另有 3 個媒體因數量上限未讀取；"
            "另有 1 個媒體無法讀取；另有 2 個媒體逾時未讀取。）",
        )
        self.assertEqual(describe_media([("靜態圖", ("a",))], 0, 0, 0), "")


class ReadMediaTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _session(status: int, content_length: int | None = None) -> SimpleNamespace:
        response = SimpleNamespace(status=status, content_length=content_length)
        return SimpleNamespace(get=lambda url, allow_redirects: nullcontext(response))

    async def test_download_failure_is_none_but_oversize_raises(self) -> None:
        url = f"{CDN}/attachments/1/2/a.png"
        self.assertIsNone(await _download_discord_media(self._session(404), url, total_before=0))
        dropped = SimpleNamespace(get=MagicMock(side_effect=aiohttp.ClientConnectionError))
        self.assertIsNone(await _download_discord_media(dropped, url, total_before=0))
        with self.assertRaises(ImageAttachmentError):
            await _download_discord_media(
                self._session(200, MAX_IMAGE_BYTES + 1), url, total_before=0
            )

    async def test_broken_sources_are_counted_and_skipped(self) -> None:
        payloads = {"gone": None, "text": b"not an image", "video": b"\0" * 16, "png": PNG}
        budget = MediaBudget()
        with (
            patch(
                "src.ai.images._download_discord_media",
                AsyncMock(side_effect=lambda session, url, total_before: payloads[url]),
            ),
            patch("src.ai.images.extract_frames", AsyncMock(return_value=[])),
        ):
            sources = [("media", "gone"), ("media", "text"), ("video", "video"), ("broken", "svg"), ("media", "png")]
            items = await read_message_media(sources, budget=budget)
        self.assertEqual([label for label, _images in items], ["靜態圖"])
        self.assertEqual(budget.broken, 4)

    async def test_deadline_keeps_what_was_read(self) -> None:
        async def download(session, url, total_before):
            await asyncio.sleep(0)
            if url == "slow":
                await asyncio.Event().wait()
            return PNG

        budget = MediaBudget()
        deadline = asyncio.get_running_loop().time() + 0.05
        with patch("src.ai.images._download_discord_media", side_effect=download):
            sources = [("media", "png"), ("media", "slow"), ("media", "late")]
            items = await read_message_media(sources, budget=budget, deadline=deadline)
            later = await read_message_media([("media", "png")], budget=budget, deadline=deadline)
        self.assertEqual([label for label, _images in items], ["靜態圖"])
        self.assertEqual(later, [])
        self.assertEqual((budget.timed_out, budget.broken, budget.skipped), (3, 0, 0))


class ExtractFramesTest(unittest.IsolatedAsyncioTestCase):
    def assertJpegs(self, frames: list[bytes], count: int) -> None:
        self.assertEqual(len(frames), count)
        self.assertTrue(all(frame.startswith(b"\xff\xd8\xff") for frame in frames))

    async def test_gif_samples_four_frames_in_order(self) -> None:
        names = ("red", "orange", "yellow", "green", "blue", "purple")
        frames = await extract_frames("image", _encode(_colors(*names), "GIF"))
        self.assertJpegs(frames, 4)
        first, last = _pixel(frames[0]), _pixel(frames[-1])
        self.assertGreater(first[0], 200)
        self.assertGreater(last[2], 100)

    async def test_short_animation_keeps_all_frames(self) -> None:
        self.assertJpegs(await extract_frames("image", _encode(_colors("red", "blue"), "PNG")), 2)

    async def test_animated_webp(self) -> None:
        frames = await extract_frames("image", _encode(_colors("red", "blue", "green"), "WEBP"))
        self.assertJpegs(frames, 3)
        self.assertGreater(_pixel(frames[0])[0], 200)

    async def test_transparency_becomes_white(self) -> None:
        frames = []
        for color in ("red", "blue"):
            frame = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
            frame.paste(color, (24, 16, 40, 32))
            frames.append(frame)
        for fmt in ("GIF", "PNG"):
            with self.subTest(fmt):
                result = await extract_frames("image", _encode(frames, fmt))
                self.assertJpegs(result, 2)
                self.assertTrue(all(value > 240 for value in _pixel(result[0])))
                self.assertGreater(_pixel(result[0], (32, 24))[0], 200)

    async def test_video(self) -> None:
        names = ("red", "yellow", "green", "blue")
        for name, codec in (("clip.mp4", "mpeg4"), ("clip.webm", "libvpx")):
            with self.subTest(codec), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, name)
                _write_video(path, codec, names)
                with open(path, "rb") as file:
                    frames = await extract_frames("video", file.read())
                self.assertJpegs(frames, 4)
                first, last = _pixel(frames[0]), _pixel(frames[-1])
                self.assertGreater(first[0], 200)
                self.assertGreater(last[2], 200)
                self.assertLess(last[0], 100)

    async def test_lottie(self) -> None:
        transform = {
            key: {"a": 0, "k": value}
            for key, value in (
                ("o", 100),
                ("r", 0),
                ("p", [50, 50, 0]),
                ("a", [50, 50, 0]),
                ("s", [100, 100, 100]),
            )
        }
        solid = {"ty": 1, "ind": 1, "ip": 0, "op": 30, "st": 0, "sw": 100, "sh": 100}
        animation = {
            "v": "5.5.2",
            "fr": 30,
            "ip": 0,
            "op": 30,
            "w": 100,
            "h": 100,
            "layers": [solid | {"sc": "#ff0000", "ks": transform}],
        }
        frames = await extract_frames("lottie", json.dumps(animation).encode())
        self.assertJpegs(frames, 4)
        self.assertGreater(Image.open(io.BytesIO(frames[0])).getpixel((50, 50))[0], 200)

    async def test_invalid_data_yields_no_frames(self) -> None:
        for kind in ("image", "video", "lottie"):
            with self.subTest(kind):
                self.assertEqual(await extract_frames(kind, b"not media"), [])

    async def test_rejects_manifests_and_playlists(self) -> None:
        mpd = (
            b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
            b'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"><Period><AdaptationSet>'
            b'<Representation id="1" bandwidth="1"><BaseURL>http://127.0.0.1:1/x.mp4</BaseURL>'
            b"</Representation></AdaptationSet></Period></MPD>"
        )
        concat = b"ffconcat version 1.0\nfile x.mp4\n"
        m3u8 = (
            b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
            b"http://127.0.0.1:1/x.ts\n#EXT-X-ENDLIST\n"
        )
        for data in (mpd, concat, m3u8):
            with self.subTest(data[:8]):
                self.assertEqual(await extract_frames("video", data), [])

    async def test_whitelist_limits_each_kind(self) -> None:
        self.assertEqual(await extract_frames("video", _encode(_colors("red", "blue"), "GIF")), [])


if __name__ == "__main__":
    unittest.main()
