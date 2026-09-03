import unittest

from src.discord_images import (
    ImageAttachmentError,
    MAX_IMAGE_BYTES,
    read_image_attachments,
    select_image_attachments,
)


class FakeAttachment:
    def __init__(
        self,
        filename,
        content_type,
        data,
        *,
        size=None,
        error=None,
    ):
        self.filename = filename
        self.content_type = content_type
        self._data = data
        self.size = len(data) if size is None else size
        self.error = error
        self.read_count = 0

    async def read(self):
        self.read_count += 1
        if self.error is not None:
            raise self.error
        return self._data


class DiscordImagesTest(unittest.IsolatedAsyncioTestCase):
    def test_selection_accepts_only_matching_supported_formats(self):
        png = FakeAttachment("one.png", "image/png", b"\x89PNG\r\n\x1a\n")
        jpeg = FakeAttachment("two.jpg", "image/jpeg", b"\xff\xd8\xff")
        webp = FakeAttachment(
            "three.webp",
            "image/webp",
            b"RIFF" + b"\0" * 4 + b"WEBP",
        )

        self.assertEqual(select_image_attachments([png, jpeg, webp]), [png, jpeg, webp])

        for attachment in (
            FakeAttachment("bad.gif", "image/gif", b"GIF89a"),
            FakeAttachment("bad.png", "image/jpeg", b"\xff\xd8\xff"),
            FakeAttachment("bad.svg", "image/svg+xml", b"<svg>"),
        ):
            with self.subTest(filename=attachment.filename):
                with self.assertRaises(ImageAttachmentError):
                    select_image_attachments([attachment])

    def test_selection_enforces_count_and_declared_size(self):
        images = [
            FakeAttachment(f"{index}.png", "image/png", b"", size=1)
            for index in range(5)
        ]
        with self.assertRaisesRegex(ImageAttachmentError, "最多處理"):
            select_image_attachments(images)

        oversized = FakeAttachment(
            "large.png",
            "image/png",
            b"",
            size=MAX_IMAGE_BYTES + 1,
        )
        with self.assertRaisesRegex(ImageAttachmentError, "8 MB"):
            select_image_attachments([oversized])

    async def test_read_returns_data_urls_after_signature_validation(self):
        png = FakeAttachment(
            "one.png",
            "image/png",
            b"\x89PNG\r\n\x1a\ncontent",
        )

        result = await read_image_attachments([png])

        self.assertEqual(png.read_count, 1)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].startswith("data:image/png;base64,"))

    async def test_bad_signature_is_rejected(self):
        png = FakeAttachment("one.png", "image/png", b"not-a-png")

        with self.assertRaisesRegex(ImageAttachmentError, "格式驗證失敗"):
            await read_image_attachments([png])


if __name__ == "__main__":
    unittest.main()
