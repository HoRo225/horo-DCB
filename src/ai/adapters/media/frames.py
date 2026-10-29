from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
from itertools import islice
from pathlib import Path

MAX_FRAMES = 4
_FRAME_EDGE = 1024
_MAX_SOURCE_PIXELS = 25_000_000
_MAX_DECODE_FRAMES = 600
_ROOT = Path(__file__).resolve().parents[4]
_VIDEO_FORMATS = "mov,matroska"
_ANIMATED_FORMATS = "gif,apng,webp_anim"
WORKERS = 2


class Frames:
    def __init__(self, workers: int = WORKERS) -> None:
        self._slots = asyncio.Semaphore(workers)

    async def extract(self, kind: str, data: bytes) -> list[bytes]:
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
                    kind,
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


def _open(source: Path, formats: str):
    import av

    return av.open(
        str(source), container_options={"format_whitelist": formats, "protocol_whitelist": "file"}
    )


def _rgba(frame):
    from PIL import Image

    plane = frame.reformat(format="rgba").planes[0]
    size = (plane.width, plane.height)
    return Image.frombuffer("RGBA", size, bytes(plane), "raw", "RGBA", plane.line_size, 1)


def _animated_frames(source: Path) -> list:
    with _open(source, _ANIMATED_FORMATS) as container:
        stream = container.streams.video[0]
        _check_pixels(stream.width, stream.height)
        total = sum(1 for packet in container.demux(stream) if packet.size)
    if total > _MAX_DECODE_FRAMES:
        raise ValueError("too many frames")
    wanted = set(_sample_indices(total))
    with _open(source, _ANIMATED_FORMATS) as container:
        decoded = enumerate(container.decode(container.streams.video[0]))
        return [_rgba(frame) for index, frame in decoded if index in wanted]


def _save_frames(frames: list, out: Path) -> None:
    from PIL import Image

    for number, frame in enumerate(frames, 1):
        frame = frame.convert("RGBA")
        frame.thumbnail((_FRAME_EDGE, _FRAME_EDGE))
        canvas = Image.new("RGB", frame.size, "white")
        canvas.paste(frame, mask=frame.getchannel("A"))
        canvas.save(out / f"{number}.jpg", "JPEG", quality=85)


def _video_frames(source: Path) -> list:
    import av

    with _open(source, _VIDEO_FORMATS) as container:
        stream = container.streams.video[0]
        _check_pixels(stream.width, stream.height)
        if not container.duration:
            return [_rgba(frame) for frame in islice(container.decode(stream), MAX_FRAMES)]
        frames = {}
        for index in range(MAX_FRAMES):
            offset = (container.start_time or 0) + container.duration * index // MAX_FRAMES
            moment = offset / av.time_base
            container.seek(int(moment / stream.time_base), stream=stream)
            frame = next(
                (f for f in container.decode(stream) if f.time is not None and f.time >= moment),
                None,
            )
            if frame is not None and frame.pts not in frames:
                frames[frame.pts] = _rgba(frame)
        return list(frames.values())


def main(argv: list[str]) -> int:
    signal.alarm(10)
    kind, source, out = argv[1], Path(argv[2]), Path(argv[3])
    try:
        if kind == "video":
            frames = _video_frames(source)
        else:
            frames = _animated_frames(source)
        _save_frames(frames, out)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
