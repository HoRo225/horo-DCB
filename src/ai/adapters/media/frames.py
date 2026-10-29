from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_FRAMES = 4
_FRAME_EDGE = 1024
_MAX_SOURCE_PIXELS = 25_000_000
_MAX_DECODE_FRAMES = 600
_ROOT = Path(__file__).resolve().parents[4]
_FORMATS = ("GIF", "PNG", "WEBP")
WORKERS = 2


class Frames:
    def __init__(self, workers: int = WORKERS) -> None:
        self._slots = asyncio.Semaphore(workers)

    async def extract(self, data: bytes) -> list[bytes]:
        async with self._slots:
            with tempfile.TemporaryDirectory(prefix="horo-frames-") as directory:
                source = Path(directory, "source")
                source.write_bytes(data)
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-E",
                    "-s",
                    "-B",
                    "-m",
                    "src.ai.adapters.media.frames",
                    str(source),
                    directory,
                    cwd=_ROOT,
                    env={"PATH": os.environ.get("PATH", ""), "TMPDIR": directory},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    code = await process.wait()
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                frames = sorted(Path(directory).glob("*.jpg"), key=lambda path: int(path.stem))
                if code != 0:
                    return []
                return [path.read_bytes() for path in frames]


def _sample_indices(total: int) -> list[int]:
    if total <= MAX_FRAMES:
        return list(range(total))
    return [round(i * (total - 1) / (MAX_FRAMES - 1)) for i in range(MAX_FRAMES)]


def _check_pixels(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width * height > _MAX_SOURCE_PIXELS:
        raise ValueError("too many pixels")


def _animated_frames(source: Path) -> list:
    from PIL import Image

    with Image.open(source, formats=_FORMATS) as image:
        _check_pixels(*image.size)
        total = getattr(image, "n_frames", 1)
        if total > _MAX_DECODE_FRAMES:
            raise ValueError("too many frames")
        frames = []
        for index in _sample_indices(total):
            image.seek(index)
            frames.append(image.convert("RGBA"))
        return frames


def _save_frames(frames: list, out: Path) -> None:
    from PIL import Image

    for number, frame in enumerate(frames, 1):
        frame = frame.convert("RGBA")
        frame.thumbnail((_FRAME_EDGE, _FRAME_EDGE))
        canvas = Image.new("RGB", frame.size, "white")
        canvas.paste(frame, mask=frame.getchannel("A"))
        canvas.save(out / f"{number}.jpg", "JPEG", quality=85)


def main(argv: list[str]) -> int:
    signal.alarm(10)
    source, out = Path(argv[1]), Path(argv[2])
    try:
        _save_frames(_animated_frames(source), out)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
