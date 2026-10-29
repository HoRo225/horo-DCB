import asyncio
import io
import os
import sys
import tempfile
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


def write_video(path: str, codec: str, names: tuple[str, ...]) -> None:
    import av

    with av.open(path, "w") as container:
        stream = container.add_stream(codec, rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for image in colors(*names):
            for _ in range(5):
                container.mux(stream.encode(av.VideoFrame.from_image(image)))
        container.mux(stream.encode())


@unittest.skipIf(sys.platform == "win32", WORKER_NEEDS_POSIX)
class ExtractFramesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.frames = Frames()

    def assertJpegs(self, frames: list[bytes], count: int) -> None:
        self.assertEqual(len(frames), count)
        self.assertTrue(all(frame.startswith(b"\xff\xd8\xff") for frame in frames))

    async def test_gif_samples_four_frames_in_order(self) -> None:
        names = ("red", "orange", "yellow", "green", "blue", "purple")
        frames = await self.frames.extract("image", encode(colors(*names), "GIF"))
        self.assertJpegs(frames, 4)
        self.assertGreater(pixel(frames[0])[0], 200)
        self.assertGreater(pixel(frames[-1])[2], 100)

    async def test_short_animation_keeps_all_frames(self) -> None:
        self.assertJpegs(await self.frames.extract("image", encode(colors("red", "blue"), "PNG")), 2)

    async def test_animated_webp(self) -> None:
        frames = await self.frames.extract("image", encode(colors("red", "blue", "green"), "WEBP"))
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
                result = await self.frames.extract("image", encode(frames, fmt))
                self.assertJpegs(result, 2)
                self.assertTrue(all(value > 240 for value in pixel(result[0])))
                self.assertGreater(pixel(result[0], (32, 24))[0], 200)

    async def test_video(self) -> None:
        names = ("red", "yellow", "green", "blue")
        for name, codec in (("clip.mp4", "mpeg4"), ("clip.webm", "libvpx")):
            with self.subTest(codec), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, name)
                write_video(path, codec, names)
                with open(path, "rb") as file:
                    frames = await self.frames.extract("video", file.read())
                self.assertJpegs(frames, 4)
                first, last = pixel(frames[0]), pixel(frames[-1])
                self.assertGreater(first[0], 200)
                self.assertGreater(last[2], 200)
                self.assertLess(last[0], 100)

    async def test_invalid_data_yields_no_frames(self) -> None:
        for kind in ("image", "video"):
            with self.subTest(kind):
                self.assertEqual(await self.frames.extract(kind, b"not media"), [])

    async def test_rejects_manifests_and_playlists(self) -> None:
        mpd = (
            b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
            b'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"><Period><AdaptationSet>'
            b'<Representation id="1" bandwidth="1"><BaseURL>http://127.0.0.1:1/x.mp4</BaseURL>'
            b"</Representation></AdaptationSet></Period></MPD>"
        )
        concat = b"ffconcat version 1.0\nfile x.mp4\n"
        m3u8 = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:1/x.ts\n#EXT-X-ENDLIST\n"
        for data in (mpd, concat, m3u8):
            with self.subTest(data[:8]):
                self.assertEqual(await self.frames.extract("video", data), [])

    async def test_whitelist_limits_each_kind(self) -> None:
        self.assertEqual(await self.frames.extract("video", encode(colors("red", "blue"), "GIF")), [])


class FrameWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_module_loads_without_discord_integrations_or_config(self) -> None:
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
        self.assertFalse({name for name in modules if name.startswith("src.integrations")})


if __name__ == "__main__":
    unittest.main()
