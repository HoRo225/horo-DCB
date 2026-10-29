import unittest
from types import SimpleNamespace

from src.ai.adapters.discord.context import select_media, select_message_media

CDN = "https://cdn.discordapp.com"
EMOJI_ID = 1100000000000
ANIMATED_EMOJI_ID = 1200000000000


def attachment(id: int, filename: str, content_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=id, filename=filename, content_type=content_type,
        proxy_url=f"https://media.discordapp.net/attachments/1/{id}/{filename}",
        url=f"{CDN}/attachments/1/{id}/{filename}",
    )


def sticker(id: int, format_name: str, ext: str = "png") -> SimpleNamespace:
    return SimpleNamespace(id=id, format=SimpleNamespace(name=format_name), url=f"{CDN}/stickers/{id}.{ext}")


def message(**fields: object) -> SimpleNamespace:
    defaults = {"attachments": (), "content": "", "embeds": (), "stickers": (), "components": ()}
    return SimpleNamespace(**(defaults | fields))


class SelectMessageMediaTest(unittest.TestCase):
    def test_kinds_and_dedupe(self) -> None:
        seen: set[str] = set()
        source = message(
            attachments=(
                attachment(1, "a.png", "image/png"),
                attachment(2, "notes.txt", "text/plain"),
                attachment(3, "a.gif", "image/gif"),
                attachment(4, "clip.mp4", "video/mp4"),
            ),
            content=f"<:ok:{EMOJI_ID}> <:ok:{EMOJI_ID}> <a:dance:{ANIMATED_EMOJI_ID}>",
            stickers=(sticker(21, "png"), sticker(22, "apng"), sticker(23, "lottie", "json")),
            embeds=(
                SimpleNamespace(
                    video=SimpleNamespace(proxy_url=f"{CDN}/v.mp4", url=None),
                    image=None,
                    thumbnail=SimpleNamespace(proxy_url=f"{CDN}/t.png", url=None),
                ),
                SimpleNamespace(video=None, image=SimpleNamespace(url="https://example.com/x.png"), thumbnail=None),
            ),
        )
        self.assertEqual(
            select_message_media([source], seen=seen),
            [
                ("media", f"{CDN}/attachments/1/1/a.png"),
                ("media", f"{CDN}/attachments/1/3/a.gif"),
                ("video", "https://media.discordapp.net/attachments/1/4/clip.mp4"),
                ("media", f"{CDN}/emojis/{EMOJI_ID}.png"),
                ("media", f"{CDN}/emojis/{ANIMATED_EMOJI_ID}.webp?animated=true"),
                ("video", f"{CDN}/v.mp4"),
                ("media", f"{CDN}/stickers/21.png"),
                ("media", f"{CDN}/stickers/22.png"),
                ("broken", f"{CDN}/stickers/23.json"),
            ],
        )
        self.assertEqual(select_message_media([source], seen=seen), [])

    def test_unsupported_image_attachments_are_broken(self) -> None:
        source = message(
            attachments=(
                attachment(1, "a.svg", "image/svg+xml"),
                attachment(2, "a.heic", "image/heic"),
                attachment(3, "a.jpg", "image/png"),
                attachment(4, "b.png", "image/png"),
            )
        )
        self.assertEqual([kind for kind, _url in select_message_media([source])], ["broken", "broken", "broken", "media"])

    def test_groups_follow_messages_and_share_dedupe(self) -> None:
        shared = attachment(1, "a.png", "image/png")
        quoted, current = message(attachments=(shared,)), message(attachments=(shared, attachment(2, "b.png", "image/png")))
        self.assertEqual(select_media([quoted, current]),
                         [[("media", shared.url)], [("media", f"{CDN}/attachments/1/2/b.png")]])


if __name__ == "__main__":
    unittest.main()
