import pytest

from horo_dcb.content import exceeds_pixels, image_input, image_mime


@pytest.mark.parametrize(
    ("content_type", "filename", "mime"),
    [
        ("image/png", "photo.png", "image/png"),
        ("image/png; charset=binary", "upload", "image/png"),
        ("IMAGE/JPEG", "upload", "image/jpeg"),
        ("image/webp", "photo.png", "image/webp"),
        (None, "photo.png", "image/png"),
        (None, "photo.JPG", "image/jpeg"),
        (None, "photo.jpeg", "image/jpeg"),
        (None, "sticker.webp", "image/webp"),
        ("application/octet-stream", "clip.gif", "image/gif"),
    ],
)
def test_image_attachments_are_recognised_with_their_mime(content_type, filename, mime):
    assert image_mime(content_type, filename) == mime


@pytest.mark.parametrize(
    ("content_type", "filename"),
    [("text/plain", "notes.txt"), (None, "archive.zip"), ("video/mp4", "clip.mp4"), ("image/bmp", "scan.bmp"), (None, "README")],
)
def test_other_attachments_are_not_images(content_type, filename):
    assert image_mime(content_type, filename) is None


@pytest.mark.parametrize(
    ("width", "height", "exceeds"),
    [(None, None, False), (None, 12000, False), (8000, 6000, False), (10000, 5000, False), (10000, 5001, True), (12000, 12000, True)],
)
def test_only_images_above_fifty_million_pixels_exceed_the_limit(width, height, exceeds):
    assert exceeds_pixels(width, height) is exceeds


def test_image_item_carries_the_exact_bytes_under_the_given_mime():
    assert image_input("image/gif", b"hello") == {"type": "image", "url": "data:image/gif;base64,aGVsbG8="}
