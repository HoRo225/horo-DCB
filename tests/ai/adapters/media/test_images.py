import asyncio
import io
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from PIL import Image

from src.ai.adapters.media.images import (
    ImageAttachmentError,
    MediaBudget,
    MediaReader,
    describe_media,
    download_discord_media,
    read_message_media,
)
from src.ai.domain.conversation import MAX_IMAGE_BYTES

CDN = "https://cdn.discordapp.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 8
WEBP = b"RIFF\0\0\0\0WEBPVP8 "
GIF = b"GIF89a" + b"\0" * 8
FRAME = b"\xff\xd8\xff frame"


def encode(colors: tuple[str, ...], fmt: str) -> bytes:
    frames = [Image.new("RGB", (8, 8), name) for name in colors]
    out = io.BytesIO()
    frames[0].save(out, fmt, save_all=len(frames) > 1, append_images=frames[1:], duration=100)
    return out.getvalue()


def frames(sampled: list[bytes] | None = None) -> SimpleNamespace:
    return SimpleNamespace(extract=AsyncMock(return_value=[FRAME, FRAME] if sampled is None else sampled))


def fetch_from(payloads: dict[str, bytes | None]):
    async def fetch(url: str, total_before: int) -> bytes | None:
        return payloads[url]
    return fetch


class ReadMediaTest(unittest.IsolatedAsyncioTestCase):
    async def read(self, sources, payloads, sampler=None, budget=None):
        budget = budget or MediaBudget()
        sampler = sampler or frames()
        items = await read_message_media(sources, budget=budget, frames=sampler, fetch=fetch_from(payloads))
        return items, budget, sampler

    async def test_still_images_become_data_urls_by_signature(self) -> None:
        payloads = {"png": PNG, "jpeg": JPEG, "webp": WEBP}
        items, budget, sampler = await self.read([("media", name) for name in payloads], payloads)
        self.assertEqual([(label, image[:22]) for label, (image,) in items],
                         [("靜態圖", "data:image/png;base64,"), ("靜態圖", "data:image/jpeg;base64"),
                          ("靜態圖", "data:image/webp;base64")])
        sampler.extract.assert_not_awaited()
        self.assertEqual(budget.images_left, 9)

    async def test_animations_and_videos_are_sampled(self) -> None:
        payloads = {
            "gif": GIF, "apng": encode(("red", "blue"), "PNG"), "awebp": encode(("red", "blue"), "WEBP"),
            "video": b"\0" * 16,
        }
        sources = [("media", "gif"), ("media", "apng"), ("media", "awebp"), ("video", "video")]
        items, _budget, sampler = await self.read(sources, payloads)
        self.assertEqual([label for label, _images in items], ["動畫", "動畫", "動畫", "影片"])
        self.assertEqual([call.args[0] for call in sampler.extract.await_args_list],
                         ["image", "image", "image", "video"])
        self.assertTrue(all(images[0].startswith("data:image/jpeg;base64,") for _label, images in items))

    async def test_still_png_and_webp_are_not_sampled(self) -> None:
        payloads = {"png": encode(("red",), "PNG"), "webp": encode(("red",), "WEBP"), "jpg": encode(("red",), "JPEG")}
        items, _budget, sampler = await self.read([("media", name) for name in payloads], payloads)
        self.assertEqual([label for label, _images in items], ["靜態圖"] * 3)
        sampler.extract.assert_not_awaited()

    async def test_broken_sources_are_counted_and_skipped(self) -> None:
        payloads = {"gone": None, "text": b"not an image", "video": b"\0" * 16, "png": PNG, "svg": b"<svg/>"}
        sources = [("media", "gone"), ("media", "text"), ("video", "video"), ("broken", "svg"), ("media", "png")]
        items, budget, _sampler = await self.read(sources, payloads, frames([]))
        self.assertEqual([label for label, _images in items], ["靜態圖"])
        self.assertEqual(budget.broken, 4)

    async def test_image_quota_skips_the_rest(self) -> None:
        payloads = {"a": PNG, "gif": GIF, "b": PNG}
        items, budget, _sampler = await self.read([("media", "a"), ("media", "gif"), ("media", "b")], payloads,
                                                  frames([FRAME] * 4), MediaBudget(images_left=3))
        self.assertEqual([len(images) for _label, images in items], [1, 2])
        self.assertEqual((budget.images_left, budget.skipped), (0, 1))

    async def test_deadline_keeps_what_was_read(self) -> None:
        async def fetch(url: str, total_before: int) -> bytes:
            await asyncio.sleep(0)
            if url == "slow":
                await asyncio.Event().wait()
            return PNG

        budget = MediaBudget()
        deadline = asyncio.get_running_loop().time() + 0.05
        sources = [("media", "png"), ("media", "slow"), ("media", "late")]
        items = await read_message_media(sources, budget=budget, frames=frames(), fetch=fetch, deadline=deadline)
        later = await read_message_media([("media", "png")], budget=budget, frames=frames(), fetch=fetch,
                                         deadline=deadline)
        self.assertEqual([label for label, _images in items], ["靜態圖"])
        self.assertEqual(later, [])
        self.assertEqual((budget.timed_out, budget.broken, budget.skipped), (3, 0, 0))

    async def test_reader_without_media_opens_no_connection(self) -> None:
        self.assertEqual(await MediaReader(frames()).read([[], []], deadline=0), ([[], []], 0, 0, 0))


class DownloadTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def session(status: int, content_length: int | None = None, chunks: tuple[bytes, ...] = ()) -> SimpleNamespace:
        async def iter_chunked(size):
            for chunk in chunks:
                yield chunk

        response = SimpleNamespace(status=status, content_length=content_length,
                                   content=SimpleNamespace(iter_chunked=iter_chunked))
        return SimpleNamespace(get=lambda url, allow_redirects: nullcontext(response))

    async def test_body_is_read_from_discord_only(self) -> None:
        url = f"{CDN}/attachments/1/2/a.png"
        self.assertEqual(await download_discord_media(self.session(200, None, (PNG[:4], PNG[4:])), url, total_before=0),
                         PNG)
        with self.assertRaises(ImageAttachmentError):
            await download_discord_media(self.session(200), "https://example.com/a.png", total_before=0)

    async def test_failure_is_none_but_oversize_raises(self) -> None:
        url = f"{CDN}/attachments/1/2/a.png"
        self.assertIsNone(await download_discord_media(self.session(404), url, total_before=0))
        dropped = SimpleNamespace(get=MagicMock(side_effect=aiohttp.ClientConnectionError))
        self.assertIsNone(await download_discord_media(dropped, url, total_before=0))
        with self.assertRaises(ImageAttachmentError):
            await download_discord_media(self.session(200, MAX_IMAGE_BYTES + 1), url, total_before=0)
        with self.assertRaises(ImageAttachmentError):
            await download_discord_media(self.session(200, None, (b"\0" * 10,)), url, total_before=MAX_IMAGE_BYTES * 5)


class DescribeMediaTest(unittest.TestCase):
    def test_animations_and_misses_are_noted(self) -> None:
        note = describe_media([("靜態圖", ("a",)), ("動畫", ("b", "c", "d", "e"))], 3, 1, 2)
        self.assertEqual(
            note,
            "\n（圖 2–5：動畫依時間順序取樣的 4 格；另有 3 個媒體因數量上限未讀取；"
            "另有 1 個媒體無法讀取；另有 2 個媒體逾時未讀取。）",
        )
        self.assertEqual(describe_media([("影片", ("a",))], 0, 0, 0), "\n（圖 1：影片依時間順序取樣的 1 格。）")
        self.assertEqual(describe_media([("靜態圖", ("a",))], 0, 0, 0), "")


if __name__ == "__main__":
    unittest.main()
