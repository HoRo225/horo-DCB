from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import tempfile
import warnings
from io import BytesIO

import imageio_ffmpeg
from PIL import Image, UnidentifiedImageError
from rlottie_python import LottieAnimation

from src.ai.protocol import (
    MAX_IMAGE_BYTES,
    MEDIA_HEADER_LIMIT,
    MEDIA_KINDS,
    SUPPORTED_IMAGE_TYPES,
    image_signature_matches,
)

_MAX_SOURCE_PIXELS = 25_000_000
_MAX_FRAMES = 4
_MAX_FRAME_EDGE = 512
_MAX_DECODE_FRAMES = 600


class _WorkerError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_exact(stream, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            raise _WorkerError("media_invalid")
        data.extend(chunk)
    return bytes(data)


def _sample_indices(total: int) -> list[int]:
    if total <= _MAX_FRAMES:
        return list(range(total))
    return [round(i * (total - 1) / (_MAX_FRAMES - 1)) for i in range(_MAX_FRAMES)]


def _check_pixels(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width * height > _MAX_SOURCE_PIXELS:
        raise _WorkerError("image_pixels")


def _contact_sheet(frames: list[Image.Image]) -> bytes:
    if not frames:
        raise _WorkerError("media_invalid")
    prepared = []
    for frame in frames:
        _check_pixels(*frame.size)
        frame = frame.convert("RGBA")
        frame.thumbnail((_MAX_FRAME_EDGE, _MAX_FRAME_EDGE))
        prepared.append(frame)
    gap = 4
    sheet = Image.new(
        "RGBA",
        (
            sum(f.width for f in prepared) + gap * (len(prepared) - 1),
            max(f.height for f in prepared),
        ),
        (0, 0, 0, 0),
    )
    left = 0
    for frame in prepared:
        sheet.paste(frame, (left, 0))
        left += frame.width + gap
    output = BytesIO()
    sheet.save(output, format="PNG", compress_level=6)
    return output.getvalue()


def _raster_contact_sheet(data: bytes) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                _check_pixels(*image.size)
                start = 1 if getattr(image, "default_image", False) else 0
                total = max(1, getattr(image, "n_frames", 1) - start)
                if total > _MAX_DECODE_FRAMES:
                    raise _WorkerError("image_frames")
                frames = []
                for index in _sample_indices(total):
                    image.seek(start + index)
                    frames.append(image.convert("RGBA"))
                return _contact_sheet(frames)
    except _WorkerError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        EOFError,
        ValueError,
    ) as exc:
        raise _WorkerError("image_invalid") from exc


def _normalize_image(data: bytes, content_type: str | None) -> tuple[str, bytes]:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type not in SUPPORTED_IMAGE_TYPES:
        media_type = next(
            (kind for kind in SUPPORTED_IMAGE_TYPES if image_signature_matches(kind, data)),
            None,
        )
        if media_type is None:
            raise _WorkerError("image_format")
    if not image_signature_matches(media_type, data):
        raise _WorkerError("image_format")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                _check_pixels(*image.size)
                if Image.MIME.get(image.format) != media_type:
                    raise _WorkerError("image_format")
                animated = getattr(image, "n_frames", 1) > 1
                if not animated and media_type != "image/gif":
                    image.verify()
            if animated or media_type == "image/gif":
                return "image/png", _raster_contact_sheet(data)
            with Image.open(BytesIO(data)) as image:
                image.load()
            return media_type, data
    except _WorkerError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        EOFError,
        ValueError,
        SyntaxError,
        struct.error,
    ) as exc:
        raise _WorkerError("image_invalid") from exc


def _video_contact_sheet(data: bytes) -> bytes:
    try:
        with tempfile.NamedTemporaryFile(suffix=".video") as source:
            source.write(data)
            source.flush()
            executable = imageio_ffmpeg.get_ffmpeg_exe()
            probe = subprocess.run(
                [executable, "-hide_banner", "-i", source.name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=2,
                check=False,
            )
            header = probe.stderr.decode("utf-8", errors="ignore")
            match = re.search(
                r"Duration: ([0-9]+):([0-9]+):([0-9]+(?:\.[0-9]+)?)",
                header,
            )
            duration = 1.0
            if match is not None:
                hours, minutes, seconds = match.groups()
                duration = max(
                    0.1,
                    int(hours) * 3600 + int(minutes) * 60 + float(seconds),
                )
            fps = min(8.0, _MAX_FRAMES / duration)
            rendered = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-i",
                    source.name,
                    "-vf",
                    (
                        f"fps={fps:.6f},"
                        "scale=512:512:force_original_aspect_ratio=decrease,"
                        "tile=4x1:padding=4"
                    ),
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
        if rendered.returncode != 0 or not rendered.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("ffmpeg did not render an image")
        return rendered.stdout
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise _WorkerError("video_invalid") from exc


def _lottie_contact_sheet(data: bytes) -> bytes:
    try:
        with LottieAnimation.from_data(data.decode("utf-8")) as animation:
            width, height = animation.lottie_animation_get_size()
            _check_pixels(width, height)
            total = max(1, animation.lottie_animation_get_totalframe())
            return _contact_sheet(
                [animation.render_pillow_frame(index) for index in _sample_indices(total)]
            )
    except _WorkerError:
        raise
    except (UnicodeDecodeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _WorkerError("lottie_invalid") from exc


def _read_request() -> tuple[str, bytes, str | None]:
    source = sys.stdin.buffer
    header_length = struct.unpack(">I", _read_exact(source, 4))[0]
    if not 0 < header_length <= MEDIA_HEADER_LIMIT:
        raise _WorkerError("media_invalid")
    try:
        metadata = json.loads(_read_exact(source, header_length))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _WorkerError("media_invalid") from exc
    if not isinstance(metadata, dict) or set(metadata) != {"kind", "content_type", "length"}:
        raise _WorkerError("media_invalid")
    kind = metadata.get("kind")
    content_type = metadata.get("content_type")
    length = metadata.get("length")
    if (
        kind not in MEDIA_KINDS
        or type(length) is not int
        or not 0 <= length <= MAX_IMAGE_BYTES
        or (
            content_type is not None
            and (not isinstance(content_type, str) or len(content_type) > 128)
        )
    ):
        raise _WorkerError("media_invalid")
    body = _read_exact(source, length)
    if source.read(1):
        raise _WorkerError("media_invalid")
    return kind, body, content_type


def _reply(header: dict[str, object], body: bytes = b"") -> None:
    encoded = json.dumps(header, separators=(",", ":")).encode()
    destination = sys.stdout.buffer
    destination.write(struct.pack(">I", len(encoded)))
    destination.write(encoded)
    destination.write(body)
    destination.flush()


def main() -> None:
    try:
        kind, data, content_type = _read_request()
        if kind == "image":
            output_type, output = _normalize_image(data, content_type)
        elif kind == "video":
            output_type, output = "image/png", _video_contact_sheet(data)
        else:
            output_type, output = "image/png", _lottie_contact_sheet(data)
        if not 0 < len(output) <= MAX_IMAGE_BYTES:
            raise _WorkerError("media_invalid")
        _reply({"ok": True, "content_type": output_type, "length": len(output)}, output)
    except _WorkerError as exc:
        _reply({"ok": False, "error": exc.code, "length": 0})
    except Exception:
        _reply({"ok": False, "error": "media_invalid", "length": 0})


if __name__ == "__main__":
    main()
