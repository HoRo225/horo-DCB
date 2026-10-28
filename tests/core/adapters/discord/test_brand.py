import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord

from src.core.adapters.discord.brand import (
    APP_EMOJIS,
    BRAND_COLOUR,
    ERROR_COLOUR,
    GOLD_COLOUR,
    ICONS,
    LOSS_COLOUR,
    NEUTRAL_COLOUR,
    WARN_COLOUR,
    WHITE,
    bar,
    card_title,
    info_card,
    load_app_emojis,
    note_text,
    notice_view,
    rana_says,
    tint,
)


def accent(view: discord.ui.LayoutView) -> discord.Colour:
    return view.children[0].accent_colour


class ToneTests(unittest.TestCase):
    def test_tone_picks_accent(self):
        cases = {"ok": GOLD_COLOUR, "info": GOLD_COLOUR, "warn": WARN_COLOUR, "error": ERROR_COLOUR,
                 "deadline": NEUTRAL_COLOUR}
        for tone, expected in cases.items():
            with self.subTest(tone=tone):
                self.assertEqual(accent(notice_view("x", tone, colour=GOLD_COLOUR)), expected)

    def test_expiry_is_grey_with_a_clock(self):
        view = notice_view("操作已過期，請重新開啟。", "deadline")
        self.assertEqual(accent(view), NEUTRAL_COLOUR)
        self.assertEqual(view.children[0].children[0].content, f"{ICONS['deadline']} 操作已過期，請重新開啟。")

    def test_defaults_to_warn_and_brand(self):
        self.assertEqual(accent(notice_view("x")), WARN_COLOUR)
        self.assertTrue(notice_view("x").children[0].children[0].content.startswith(ICONS["warn"]))
        self.assertEqual(accent(notice_view("x", "ok")), BRAND_COLOUR)

    def test_notice_buttons_sit_in_one_row_under_the_card(self):
        links = [discord.ui.Button(label=n, url=f"https://example.com/{n}") for n in "ab"]
        card, row = notice_view("x", items=links).children
        self.assertEqual(len(card.children), 1)
        self.assertIsInstance(row, discord.ui.ActionRow)
        self.assertEqual([b.label for b in row.children], ["a", "b"])
        self.assertEqual(len(notice_view("x").children), 1)

    def test_note_text(self):
        self.assertEqual(note_text(("error", "失敗")), f"{ICONS['error']} 失敗")


class CardTitleTests(unittest.TestCase):
    def test_plain_title(self):
        item = card_title("標題", "副標")
        self.assertIsInstance(item, discord.ui.TextDisplay)
        self.assertEqual(item.content, "## 標題\n-# 副標")
        self.assertEqual(card_title("標題").content, "## 標題")

    def test_thumbnail_title(self):
        item = card_title("標題", thumbnail="https://example.com/a.png")
        self.assertIsInstance(item, discord.ui.Section)
        self.assertIsInstance(item.accessory, discord.ui.Thumbnail)
        self.assertEqual(item.accessory.media.url, "https://example.com/a.png")

    def test_button_title(self):
        button = discord.ui.Button(label="重新整理")
        item = card_title("標題", accessory=button)
        self.assertIsInstance(item, discord.ui.Section)
        self.assertIs(item.accessory, button)


def app_emoji(name: str) -> MagicMock:
    emoji = MagicMock()
    emoji.name = name
    emoji.__str__.return_value = f"<:{name}:1234567890123456789>"
    return emoji


class LoadAppEmojisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(ICONS))
        self.enterContext(patch.dict(APP_EMOJIS, clear=True))
        self.enterContext(patch.dict(WHITE, clear=True))

    async def test_loaded_icons_replace_unicode(self):
        names = ["lgok", "lwok", "lgwarn", "lowarn", "lwwarn", "lrerror", "lgdots", "icinfo", "bj_AS"]
        client = MagicMock()
        client.fetch_application_emojis = AsyncMock(return_value=[app_emoji(name) for name in names])
        await load_app_emojis(client)
        self.assertEqual(ICONS["ok"], "<:lgok:1234567890123456789>")
        self.assertEqual(ICONS["warn"], "<:lowarn:1234567890123456789>")
        self.assertEqual(ICONS["error"], "<:lrerror:1234567890123456789>")
        self.assertEqual(ICONS["dots"], "<:lgdots:1234567890123456789>")
        self.assertEqual(ICONS["info"], "ℹ️")
        self.assertEqual(APP_EMOJIS["bj_AS"], "<:bj_AS:1234567890123456789>")
        self.assertEqual(WHITE, {
            "<:lgok:1234567890123456789>": "<:lwok:1234567890123456789>",
            "<:lowarn:1234567890123456789>": "<:lwwarn:1234567890123456789>",
        })

    async def test_failure_keeps_unicode(self):
        for error in (discord.HTTPException(MagicMock(status=500), "down"), aiohttp.ClientError()):
            with self.subTest(error=type(error).__name__):
                client = MagicMock()
                client.fetch_application_emojis = AsyncMock(side_effect=error)
                with self.assertLogs(level="ERROR"):
                    await load_app_emojis(client)
                self.assertEqual(ICONS["ok"], "✅")
                self.assertEqual(APP_EMOJIS, {})
                self.assertEqual(WHITE, {})


class TintTests(unittest.TestCase):
    def test_white_on_blue_and_red_only(self):
        with patch.dict(WHITE, {"<:lgok:1>": "<:lwok:1>"}, clear=True):
            for style in (discord.ButtonStyle.primary, discord.ButtonStyle.danger):
                self.assertEqual(tint("<:lgok:1>", style), "<:lwok:1>")
            for style in (discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.link):
                self.assertEqual(tint("<:lgok:1>", style), "<:lgok:1>")
            self.assertEqual(tint("✅", discord.ButtonStyle.primary), "✅")


class BarTests(unittest.TestCase):
    def test_falls_back_to_text_until_loaded(self):
        with patch.dict(APP_EMOJIS, clear=True):
            self.assertEqual(bar(0.26), "▰▰▰▱▱▱▱▱▱▱")
            self.assertEqual(bar(2, width=4), "▰▰▰▰")
            self.assertEqual(bar(-1, width=4), "▱▱▱▱")
            APP_EMOJIS["bargoldl"] = "<:bargoldl:1234567890123456789>"
            self.assertEqual(bar(0.5, width=4), "▰▰▱▱")

    def test_loaded_bar_has_rounded_ends(self):
        names = [f"bar{colour}{part}" for colour in ("gold", "empty") for part in "lmr"]
        with patch.dict(APP_EMOJIS, {name: f"<{name}>" for name in names}, clear=True):
            self.assertEqual(bar(0.5, width=4), "<bargoldl><bargoldm><baremptym><baremptyr>")
            self.assertEqual(bar(1, width=3), "<bargoldl><bargoldm><bargoldr>")
            self.assertEqual(bar(0, width=3), "<baremptyl><baremptym><baremptyr>")


class FormatTests(unittest.TestCase):
    def test_rana_says(self):
        with patch.dict(APP_EMOJIS, clear=True):
            self.assertEqual(rana_says("好", "樂奈"), "> **樂奈**　好")
            self.assertEqual(rana_says("一\n二", "樂奈"), "> **樂奈**　一\n> 二")
            APP_EMOJIS["rana"] = "<:rana:1>"
            self.assertEqual(rana_says("好", "HoRo"), "> <:rana:1> **HoRo**　好")
            self.assertEqual(rana_says("好", "睦_頭"), "> <:rana:1> **睦\\_頭**　好")

    def test_info_card_takes_an_accent(self):
        self.assertEqual(accent(info_card(discord.ui.TextDisplay("x"))), BRAND_COLOUR)
        self.assertEqual(accent(info_card(discord.ui.TextDisplay("x"), colour=WARN_COLOUR)), WARN_COLOUR)

    def test_loss_is_its_own_colour(self):
        self.assertNotIn(LOSS_COLOUR, (ERROR_COLOUR, BRAND_COLOUR, NEUTRAL_COLOUR, WARN_COLOUR, GOLD_COLOUR))


if __name__ == "__main__":
    unittest.main()
