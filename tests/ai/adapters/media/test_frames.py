import asyncio
import io
import sys
import unittest
from pathlib import Path

from PIL import Image

from src.ai.adapters.media.frames import Frames

ROOT = Path(__file__).resolve().parents[4]
WORKER_NEEDS_POSIX = "the frame worker uses signal.alarm, which Windows lacks; Docker runs these"


def encode(frames: list[Image.Image], fmt: str) -> bytes:
    out = io.BytesIO()
    frames[0].save(out, fmt, save_all=len(frames) > 1, append_images=frames[1:], duration=100)
    return out.getvalue()


def colors(*names: str) -> list[Image.Image]:
    return [Image.new("RGB", (64, 48), name) for name in names]


def pixel(frame: bytes, xy: tuple[int, int] = (5, 5)) -> tuple[int, ...]:
    return Image.open(io.BytesIO(frame)).getpixel(xy)


@unittest.skipIf(sys.platform == "win32", WORKER_NEEDS_POSIX)
class ExtractFramesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.frames = Frames()

    def assertJpegs(self, frames: list[bytes], count: int) -> None:
        self.assertEqual(len(frames), count)
        self.assertTrue(all(frame.startswith(b"\xff\xd8\xff") for frame in frames))

    async def test_gif_samples_four_frames_in_order(self) -> None:
        names = ("red", "orange", "yellow", "green", "blue", "purple")
        frames = await self.frames.extract(encode(colors(*names), "GIF"))
        self.assertJpegs(frames, 4)
        self.assertGreater(pixel(frames[0])[0], 200)
        self.assertGreater(pixel(frames[-1])[2], 100)

    async def test_short_animation_keeps_all_frames(self) -> None:
        self.assertJpegs(await self.frames.extract(encode(colors("red", "blue"), "PNG")), 2)

    async def test_animated_webp(self) -> None:
        frames = await self.frames.extract(encode(colors("red", "blue", "green"), "WEBP"))
        self.assertJpegs(frames, 3)
        self.assertGreater(pixel(frames[0])[0], 200)

    async def test_transparency_becomes_white(self) -> None:
        frames = []
        for color in ("red", "blue"):
            frame = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
            frame.paste(color, (24, 16, 40, 32))
            frames.append(frame)
        for fmt in ("GIF", "PNG"):
            with self.subTest(fmt):
                result = await self.frames.extract(encode(frames, fmt))
                self.assertJpegs(result, 2)
                self.assertTrue(all(value > 240 for value in pixel(result[0])))
                self.assertGreater(pixel(result[0], (32, 24))[0], 200)

    async def test_invalid_data_yields_no_frames(self) -> None:
        self.assertEqual(await self.frames.extract(b"not media"), [])

    async def test_only_gif_png_and_webp_are_decoded(self) -> None:
        self.assertEqual(await self.frames.extract(encode(colors("red", "blue"), "TIFF")), [])


class FrameWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_module_loads_without_discord_ai_backend_or_config(self) -> None:
        code = "import sys, src.ai.adapters.media.frames; print(*sys.modules, sep=chr(10))"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-E", "-s", "-B", "-c", code, cwd=ROOT, stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await process.communicate()
        self.assertEqual(process.returncode, 0)
        modules = set(out.decode().split())
        self.assertIn("src.ai.adapters.media.frames", modules)
        self.assertNotIn("discord", modules)
        self.assertNotIn("src.core.app.config", modules)
        self.assertFalse({name for name in modules if name.startswith("src.ai.adapters.claude")})


if __name__ == "__main__":
    unittest.main()
