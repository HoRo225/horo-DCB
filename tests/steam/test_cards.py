import discord

from horo_dcb.steam import Offer, ended_card, live_card
from horo_dcb.store import Post, Promotion
from tests.support.discord_fakes import layout

STEAM = "<:lgsteam:1559202444884578404>"
PURPLE, GREY = 0x7F77DD, 0x80848E
HEADER = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/785890/abc/header.jpg?t=1"
OFFER = Offer(1782986, 785890, "Hexologic", "數獨與六角形的解謎遊戲。", HEADER, "NT$ 102.00", 1793466000)
CLAIM = {"buttons": [("前往 Steam 領取", "https://store.steampowered.com/app/785890/")]}
STORE = {"buttons": [("Steam 商店頁", "https://store.steampowered.com/app/785890/")]}


def test_a_live_card_mentions_roles_above_a_purple_card_with_cover_and_claim_button():
    assert layout(live_card(OFFER, [discord.Object(301), discord.Object(302)])) == [
        "<@&301> <@&302>",
        {"colour": PURPLE, "parts": [{"images": [HEADER]}, f"## {STEAM} Hexologic\n-# 限時免費 · ~~NT$ 102.00~~ · <t:1793466000:f> 截止（<t:1793466000:R>）", "數獨與六角形的解謎遊戲。"]},
        CLAIM,
    ]


def test_a_live_card_drops_what_it_lacks():
    bare = Offer(1782986, 785890, "Hexologic", "", None, "NT$ 102.00", None)
    assert layout(live_card(bare, [])) == [{"colour": PURPLE, "parts": [f"## {STEAM} Hexologic\n-# 限時免費 · ~~NT$ 102.00~~"]}, CLAIM]


def test_markdown_in_the_name_and_description_is_shown_literally():
    offer = Offer(1782986, 785890, "*Star* _Game_", "**粗體** `程式`", None, "NT$ 102.00", None)
    [card, _] = layout(live_card(offer, []))
    assert card["parts"] == [f"## {STEAM} \\*Star\\* \\_Game\\_\n-# 限時免費 · ~~NT$ 102.00~~", "\\*\\*粗體\\*\\* \\`程式\\`"]


def test_an_ended_card_is_grey_and_keeps_only_name_price_deadline_and_store_link():
    ended = Promotion(785890, "*Hexologic*", "NT$ 102.00", 1793466000, Post(200, 10_001))
    assert layout(ended_card(ended)) == [{"colour": GREY, "parts": [f"## {STEAM} \\*Hexologic\\*\n-# 限免已結束 · 原價 NT$ 102.00 · <t:1793466000:f> 截止"]}, STORE]


def test_an_ended_card_without_a_deadline_omits_it():
    ended = Promotion(785890, "Hexologic", "NT$ 102.00", None, None)
    assert layout(ended_card(ended)) == [{"colour": GREY, "parts": [f"## {STEAM} Hexologic\n-# 限免已結束 · 原價 NT$ 102.00"]}, STORE]
