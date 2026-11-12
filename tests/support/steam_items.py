import copy
import json
from functools import cache
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "steam"
RECORDED_APP = 4693960
RECORDED_PACKAGE = 1782986


@cache
def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def recorded_search() -> dict:
    return copy.deepcopy(_fixture("search-all-2026-11-12.json"))


def recorded_items() -> dict:
    return copy.deepcopy(_fixture("getitems-dlc-free-to-keep-2026-11-12.json"))


def recorded_mixed() -> dict:
    return {"response": copy.deepcopy(_fixture("getitems-mixed-2026-11-12.json"))}


def game(app: int = RECORDED_APP, package: int = RECORDED_PACKAGE, *, name: str | None = None, ends: int | None = 1794506400) -> dict:
    [item] = recorded_items()["response"]["store_items"]
    item["type"] = 0
    item["id"] = item["appid"] = app
    item["assets"]["asset_url_format"] = item["assets"]["asset_url_format"].replace(str(RECORDED_APP), str(app))
    for option in (item["best_purchase_option"], item["purchase_options"][0]):
        option["packageid"] = package
        if ends is None:
            del option["free_to_keep_ends"]
        else:
            option["free_to_keep_ends"] = ends
    if name is not None:
        item["name"] = name
    return item


def paid(app: int = RECORDED_APP) -> dict:
    item = game(app)
    item["best_purchase_option"] = item["purchase_options"][1]
    del item["is_free_temporarily"]
    return item


def missing(app: int) -> dict:
    return {"item_type": 0, "id": app, "success": 15, "visible": False, "name": "", "store_url_path": "", "store_url_slug": "", "appid": app}
