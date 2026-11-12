import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from horo_dcb.steam import Health, Steam
from horo_dcb.store import Store
from tests.support.discord_fakes import CHANNEL_ID, Channel, Client, http_error, layout
from tests.support.steam_items import game, paid
from tests.support.waiting import until

STEAM = "<:lgsteam:1559202444884578404>"
NAME = "Hunting Simulator 3 - Classic American Hunter Pack"
DESCRIPTION = "在這款開放世界遊戲中，帶上忠誠的愛犬，暢享狩獵激情！從官方品牌中選擇裝備，探索荒野，尋找下一個獵物。"
HEADER = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/4693960/d37423b5b7941444dfe63d8b185ed680ff03009d/header.jpg?t=1793269525"
APP, PACKAGE, ENDS, LATER = 4693960, 1782986, 1794506400, 1794592800
FIRST_POST = 10_001
STORE_PAGE = f"https://store.steampowered.com/app/{APP}/"


def live(ends: int = ENDS) -> list:
    return [
        {"colour": 0x7F77DD, "parts": [{"images": [HEADER]}, f"## {STEAM} {NAME}\n-# 限時免費 · ~~NT$ 117.00~~ · <t:{ends}:f> 截止（<t:{ends}:R>）", DESCRIPTION]},
        {"buttons": [("前往 Steam 領取", STORE_PAGE)]},
    ]


ENDED = [{"colour": 0x80848E, "parts": [f"## {STEAM} {NAME}\n-# 限免已結束 · 原價 NT$ 117.00 · <t:{ENDS}:f> 截止"]}, {"buttons": [("Steam 商店頁", STORE_PAGE)]}]


def record(message: int | None, ends: int = ENDS, app: int = APP) -> dict:
    return {"app": app, "name": NAME, "price": "NT$ 117.00", "ends": ends, "post": None if message is None else {"channel": str(CHANNEL_ID), "message": str(message)}}


def on_disk(directory) -> dict:
    return json.loads((directory / "steam.json").read_text(encoding="utf-8"))


def end_promotion(steam_server, app: int = APP) -> None:
    steam_server.withdraw(app)
    steam_server.offer(paid(app), listed=False)


def screens(sent: Mock) -> list[list]:
    return [layout(view) for view in sent.views]


@pytest.fixture
async def store(tmp_path):
    value = Store(tmp_path)
    await value.open()
    await value.update_settings(steam_channel_id=CHANNEL_ID)
    return value


@pytest.fixture
def channel():
    return Channel()


@pytest.fixture
def client(channel):
    return Client(channel)


def make(store: Store, client: Client, steam_server) -> Steam:
    bot = SimpleNamespace(store=store, get_channel=client.get_channel, get_partial_messageable=client.get_partial_messageable, console=SimpleNamespace(refresh_panels=AsyncMock()))
    return Steam(bot, search_url=steam_server.url("/search"), items_url=steam_server.url("/items"))


@pytest.fixture
def steam(store, client, steam_server):
    return make(store, client, steam_server)


@pytest.fixture
async def posted(steam, channel, steam_server) -> Mock:
    steam_server.offer(game())
    await steam.check()
    [sent] = channel.sent
    return sent


async def test_a_new_promotion_is_posted_once_and_recorded(tmp_path, steam, channel, steam_server):
    steam_server.offer(game())
    await steam.check()
    await steam.check()
    [sent] = channel.sent
    assert screens(sent) == [live()]
    assert on_disk(tmp_path) == {str(PACKAGE): record(FIRST_POST)}
    assert steam.health is Health.OK


async def test_steam_is_asked_for_free_games_in_taiwan(steam, steam_server):
    steam_server.offer(game())
    await steam.check()
    assert steam_server.searches == [{"maxprice": "free", "specials": "1", "category1": "998", "json": "1", "cc": "tw", "l": "tchinese"}]
    [lookup] = steam_server.lookups
    assert lookup == {"ids": [{"appid": APP}], "context": {"language": "tchinese", "country_code": "TW", "steam_realm": 1}, "data_request": {"include_basic_info": True, "include_assets": True, "include_all_purchase_options": True}}


async def test_an_empty_search_asks_nothing_more(steam, channel, steam_server):
    await steam.check()
    assert len(steam_server.searches) == 1
    assert steam_server.lookups == []
    assert channel.sent == []
    assert steam.health is Health.OK


async def test_a_restart_does_not_post_again(tmp_path, posted, client, channel, steam_server):
    reopened = Store(tmp_path)
    await reopened.open()
    await make(reopened, client, steam_server).check()
    assert channel.sent == [posted]
    assert screens(posted) == [live()]


async def test_changing_the_channel_does_not_post_again(store, posted, steam, steam_server):
    other = Channel(201)
    steam.bot.get_channel = Client(other).get_channel
    await store.update_settings(steam_channel_id=201)
    await steam.check()
    assert other.sent == []


async def test_an_ended_promotion_turns_the_card_grey(tmp_path, posted, steam, channel, steam_server):
    end_promotion(steam_server)
    await steam.check()
    assert steam_server.looked_up()[-1] == [APP]
    assert screens(posted) == [live(), ENDED]
    assert on_disk(tmp_path) == {str(PACKAGE): record(None)}
    await steam.check()
    assert screens(posted) == [live(), ENDED]
    assert channel.sent == [posted]


async def test_a_moved_deadline_edits_the_card(tmp_path, posted, steam, channel, steam_server):
    steam_server.offer(game(ends=LATER))
    await steam.check()
    assert screens(posted) == [live(), live(LATER)]
    assert on_disk(tmp_path) == {str(PACKAGE): record(FIRST_POST, LATER)}
    await steam.check()
    assert len(posted.views) == 2
    assert channel.sent == [posted]


@pytest.mark.parametrize("change", ["ended", "moved"])
async def test_a_deleted_card_is_marked_ended_and_not_posted_again(tmp_path, posted, steam, channel, steam_server, change):
    await posted.delete()
    if change == "ended":
        end_promotion(steam_server)
    else:
        steam_server.offer(game(ends=LATER))
    await steam.check()
    await steam.check()
    assert channel.sent == [posted]
    assert on_disk(tmp_path)[str(PACKAGE)]["post"] is None


async def test_a_card_in_a_deleted_channel_is_marked_ended(tmp_path, posted, steam, steam_server):
    steam.bot.get_partial_messageable = Client().get_partial_messageable
    end_promotion(steam_server)
    await steam.check()
    assert on_disk(tmp_path) == {str(PACKAGE): record(None)}


async def test_a_card_the_bot_may_no_longer_edit_is_marked_ended(tmp_path, posted, steam, channel, steam_server):
    channel.edit_error = http_error(discord.Forbidden, 403)
    end_promotion(steam_server)
    await steam.check()
    assert screens(posted) == [live()]
    assert on_disk(tmp_path) == {str(PACKAGE): record(None)}


async def test_a_temporary_discord_error_retries_the_edit_next_time(tmp_path, posted, steam, channel, steam_server):
    channel.edit_error = http_error(discord.HTTPException, 500)
    end_promotion(steam_server)
    await steam.check()
    assert screens(posted) == [live()]
    assert on_disk(tmp_path) == {str(PACKAGE): record(FIRST_POST)}
    channel.edit_error = None
    await steam.check()
    assert screens(posted) == [live(), ENDED]
    assert on_disk(tmp_path) == {str(PACKAGE): record(None)}


async def test_without_a_channel_nothing_is_posted_but_live_cards_still_end(tmp_path, store, posted, steam, channel, steam_server):
    await store.update_settings(steam_channel_id=None)
    steam_server.offer(game(77, 88))
    end_promotion(steam_server)
    await steam.check()
    assert len(steam_server.searches) == 1
    assert steam_server.looked_up()[-1] == [APP]
    assert channel.sent == [posted]
    assert screens(posted) == [live(), ENDED]
    assert on_disk(tmp_path) == {str(PACKAGE): record(None)}


async def test_without_a_channel_or_live_cards_steam_is_not_asked(store, steam, steam_server):
    await store.update_settings(steam_channel_id=None)
    steam_server.offer(game())
    await steam.check()
    assert steam_server.searches == []
    assert steam_server.lookups == []


async def test_the_same_game_free_again_later_is_posted_again(tmp_path, posted, steam, channel, steam_server):
    end_promotion(steam_server)
    await steam.check()
    steam_server.offer(game(package=1900000))
    await steam.check()
    assert [sent.id for sent in channel.sent] == [FIRST_POST, FIRST_POST + 1]
    assert screens(channel.sent[1]) == [live()]
    assert on_disk(tmp_path) == {str(PACKAGE): record(None), "1900000": record(FIRST_POST + 1)}


async def test_mentions_only_chosen_roles_that_still_exist(store, steam, channel, steam_server):
    channel.roles.update({301: Mock(spec=discord.Role, id=301), 302: Mock(spec=discord.Role, id=302)})
    await store.update_settings(steam_role_ids=[302, 303, 301])
    steam_server.offer(game())
    await steam.check()
    [sent] = channel.sent
    assert layout(sent.views[-1]) == ["<@&301> <@&302>", *live()]
    allowed = sent.send_options["allowed_mentions"].to_dict()
    assert (allowed["parse"], allowed["roles"]) == ([], [301, 302])


async def test_without_roles_nobody_may_be_mentioned(steam, channel, steam_server):
    steam_server.offer(game())
    await steam.check()
    allowed = channel.sent[0].send_options["allowed_mentions"].to_dict()
    assert (allowed["parse"], allowed["roles"]) == ([], [])


def failing_search(steam_server) -> None:
    steam_server.statuses["search"] = 500


def failing_lookup(steam_server) -> None:
    steam_server.statuses["items"] = 503


def malformed_search(steam_server) -> None:
    steam_server.bodies["search"] = []


def malformed_lookup(steam_server) -> None:
    steam_server.bodies["items"] = {"response": {}}


def malformed_offer(steam_server) -> None:
    item = game(77, 88)
    item["best_purchase_option"]["packageid"] = "88"
    steam_server.offer(item)


@pytest.mark.parametrize("breakage", [failing_search, failing_lookup, malformed_search, malformed_lookup, malformed_offer])
async def test_steam_trouble_changes_nothing(tmp_path, posted, steam, channel, steam_server, breakage):
    end_promotion(steam_server)
    steam_server.offer(game(55, 66))
    breakage(steam_server)
    await steam.check()
    assert channel.sent == [posted]
    assert screens(posted) == [live()]
    assert on_disk(tmp_path) == {str(PACKAGE): record(FIRST_POST)}
    assert steam.health is Health.UNREACHABLE


async def test_a_failed_send_is_not_recorded_and_retried_next_time(tmp_path, steam, channel, steam_server):
    channel.mock.send.side_effect = http_error(discord.Forbidden, 403)
    steam_server.offer(game())
    await steam.check()
    assert channel.sent == []
    assert on_disk(tmp_path) == {}
    assert steam.health is Health.UNSENDABLE
    channel.mock.send.side_effect = channel.send
    await steam.check()
    assert len(channel.sent) == 1
    assert on_disk(tmp_path) == {str(PACKAGE): record(FIRST_POST)}
    assert steam.health is Health.OK


async def test_a_promotion_that_ends_before_it_could_be_sent_is_never_sent(tmp_path, steam, channel, steam_server):
    channel.mock.send.side_effect = http_error(discord.Forbidden, 403)
    steam_server.offer(game())
    await steam.check()
    channel.mock.send.side_effect = channel.send
    end_promotion(steam_server)
    await steam.check()
    assert channel.sent == []
    assert on_disk(tmp_path) == {}


async def test_a_missing_channel_cannot_be_sent_to(tmp_path, store, steam, steam_server):
    await store.update_settings(steam_channel_id=999)
    steam_server.offer(game())
    await steam.check()
    assert on_disk(tmp_path) == {}
    assert steam.health is Health.UNSENDABLE


async def test_starting_twice_runs_one_schedule(steam, channel, steam_server):
    steam_server.offer(game())
    steam.start()
    steam.start()
    await until(lambda: channel.sent)
    await asyncio.sleep(0.1)
    await steam.close()
    assert len(steam_server.searches) == 1
    assert len(channel.sent) == 1
