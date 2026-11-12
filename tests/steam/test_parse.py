import pytest

from horo_dcb.steam import Offer, parse_items, search_apps
from tests.support.steam_items import game, recorded_items, recorded_mixed, recorded_search

NAME = "Hunting Simulator 3 - Classic American Hunter Pack"
DESCRIPTION = "在這款開放世界遊戲中，帶上忠誠的愛犬，暢享狩獵激情！從官方品牌中選擇裝備，探索荒野，尋找下一個獵物。"
HEADER = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app}/d37423b5b7941444dfe63d8b185ed680ff03009d/header.jpg?t=1793269525"


def items(*values: dict) -> dict:
    return {"response": {"store_items": list(values)}}


def test_the_search_yields_the_app_ids_in_its_logos():
    assert search_apps(recorded_search()) == {4693960}


def test_an_empty_search_yields_nothing():
    assert search_apps({"desc": "", "items": []}) == set()


def test_a_limited_free_game_becomes_an_offer():
    assert parse_items(items(game())) == {1782986: Offer(1782986, 4693960, NAME, DESCRIPTION, HEADER.format(app=4693960), "NT$ 117.00", 1794506400)}


def test_dlc_permanently_free_discounted_and_unknown_items_are_not_offers():
    mixed = recorded_mixed()
    assert parse_items(mixed) == {}
    mixed["response"]["store_items"].append(game(77, 88))
    assert parse_items(mixed) == {88: Offer(88, 77, NAME, DESCRIPTION, HEADER.format(app=77), "NT$ 117.00", 1794506400)}


def test_the_recorded_free_dlc_is_not_an_offer():
    assert parse_items(recorded_items()) == {}


def change(path: tuple[str, ...], value):
    def apply(item: dict) -> dict:
        target = item
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return item

    return apply


@pytest.mark.parametrize(
    "edit",
    [
        change(("type",), 4),
        change(("success",), 15),
        change(("best_purchase_option", "is_free_to_keep"), False),
        change(("best_purchase_option", "discount_pct"), 90),
        change(("best_purchase_option", "original_price_in_cents"), "0"),
    ],
    ids=["dlc", "unknown", "not-free-to-keep", "partly-discounted", "always-free"],
)
def test_a_game_missing_any_condition_is_not_an_offer(edit):
    assert parse_items(items(edit(game()))) == {}


def test_a_game_without_cover_description_or_deadline_still_becomes_an_offer():
    item = game(ends=None)
    del item["assets"]
    item["basic_info"]["short_description"] = ""
    assert parse_items(items(item)) == {1782986: Offer(1782986, 4693960, NAME, "", None, "NT$ 117.00", None)}
