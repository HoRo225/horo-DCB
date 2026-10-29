import asyncio
import json
import logging
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import patch

import aiohttp

from src.steam.adapters.http import provider
from src.steam.adapters.http.provider import (
    STEAM_APPDETAILS_URL,
    STEAM_GETITEMS_URL,
    STEAM_SEARCH_URL,
    SteamOfferProvider,
)
from src.steam.domain.offers import SteamOffer

HEADER = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/10/header.jpg"


@contextmanager
def quiet() -> Iterator[None]:
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def item(app_id: int, name: str) -> dict[str, str]:
    return {"name": name, "logo": f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{app_id}/logo.jpg"}


def details(**data: object) -> dict[str, object]:
    fields = {
        "type": "game",
        "name": "Free &amp; Fun",
        "is_free": True,
        "short_description": "  short\n desc ",
        "header_image": HEADER,
        "price_overview": {"currency": "TWD", "initial": 34900, "final": 0, "discount_percent": 100,
                           "initial_formatted": "NT$ 349", "final_formatted": "NT$ 0"},
        **data,
    }
    return {"success": True, "data": {key: value for key, value in fields.items() if value is not None}}


class Response:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.status = answer[0] if isinstance(answer, tuple) else 200

    async def __aenter__(self) -> "Response":
        if isinstance(self.answer, BaseException):
            raise self.answer
        if callable(self.answer):
            self.answer = await self.answer()
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def json(self, content_type: str | None = None) -> object:
        return self.answer[1] if isinstance(self.answer, tuple) else self.answer


class FakeSteam:
    def __init__(self) -> None:
        self.search: object = {"items": []}
        self.details: dict[int, object] = {}
        self.items: object = {"response": {"store_items": []}}
        self.requests: list[tuple[str, dict[str, str] | None]] = []
        self.sessions: list[dict[str, object]] = []
        self.closed = False

    def __call__(self, **options: object) -> "FakeSteam":
        self.sessions.append(options)
        self.closed = False
        return self

    def get(self, url: str, params: dict[str, str] | None = None) -> Response:
        self.requests.append((url, params))
        if url == STEAM_SEARCH_URL:
            return Response(self.search)
        if url == STEAM_APPDETAILS_URL:
            app_id = int(params["appids"])
            answer = self.details.get(app_id, {})
            return Response(answer if isinstance(answer, BaseException) or callable(answer) else {str(app_id): answer})
        return Response(self.items)

    async def close(self) -> None:
        self.closed = True

    def detail_order(self) -> list[int]:
        return [int(params["appids"]) for url, params in self.requests if url == STEAM_APPDETAILS_URL]

    def asked_deadlines(self) -> list[int]:
        return [entry["appid"] for url, params in self.requests if url == STEAM_GETITEMS_URL
                for entry in json.loads(params["input_json"])["ids"]]


class ProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.steam = FakeSteam()
        session = patch.object(provider.aiohttp, "ClientSession", self.steam)
        session.start()
        self.addCleanup(session.stop)
        self.provider = SteamOfferProvider()
        self.addAsyncCleanup(self.provider.close)

    def listed(self, *app_ids: int, detail: Callable[[int], object] = lambda _app_id: details()) -> None:
        self.steam.search = {"items": [item(app_id, f"game{app_id}") for app_id in app_ids]}
        self.steam.details = {app_id: detail(app_id) for app_id in app_ids}

    async def test_free_to_keep_promotion_becomes_a_clean_offer(self) -> None:
        self.listed(10)
        result = await self.provider.fetch_current_offers()
        self.assertEqual(result.offers, (SteamOffer(10, "Free & Fun", "NT$ 349", "short desc", HEADER),))
        self.assertEqual((result.active_app_ids, result.failed_app_count), (frozenset({10}), 0))
        self.assertEqual(self.steam.requests[0][1]["maxprice"], "free")
        self.assertEqual(self.steam.requests[1][1]["appids"], "10")

    async def test_search_name_fills_a_missing_detail_name(self) -> None:
        self.listed(10, detail=lambda _app_id: details(name=""))
        self.assertEqual((await self.provider.fetch_current_offers()).offers[0].name, "game10")

    async def test_unsafe_header_image_is_dropped(self) -> None:
        self.listed(10, detail=lambda _app_id: details(header_image="http://example.com/h.jpg"))
        self.assertIsNone((await self.provider.fetch_current_offers()).offers[0].header_image)

    async def test_invalid_search_payload_or_shape_returns_none(self) -> None:
        for payload in (None, [], {}, {"items": None}, {"items": [item(1, "game")] * 201}, {"items": [{}]},
                        {"items": [{"name": "game", "logo": ""}]}, (500, {"items": []})):
            with self.subTest(payload=payload):
                self.steam.search = payload
                with quiet():
                    self.assertIsNone(await self.provider.fetch_current_offers())
        self.assertEqual(self.steam.detail_order(), [])

    async def test_duplicate_app_ids_keep_the_first_name_and_unknown_logos_are_skipped(self) -> None:
        self.steam.search = {"items": [item(2, "  first   name  "), item(2, "second name"),
                                       {"name": "not an app", "logo": "https://example.com/logo.jpg"}, item(0, "zero")]}
        self.steam.details = {2: details(name=None)}
        result = await self.provider.fetch_current_offers()
        self.assertEqual(result.active_app_ids, frozenset({2}))
        self.assertEqual([offer.name for offer in result.offers], ["first name"])
        self.assertEqual(self.steam.detail_order(), [2])

    async def test_background_checks_resume_after_the_last_app_and_include_tracked_apps(self) -> None:
        self.listed(1, 2)
        await self.provider.fetch_current_offers(tracked_app_ids=frozenset())
        self.listed(3, 1)
        self.steam.details |= {2: details(), 4: details()}
        self.steam.requests.clear()
        result = await self.provider.fetch_current_offers(tracked_app_ids=frozenset({2, 4}))
        self.assertEqual(result.active_app_ids, frozenset({1, 2, 3, 4}))
        self.assertEqual(self.steam.detail_order(), [3, 4, 1, 2])
        self.assertEqual([offer.app_id for offer in result.offers], [3, 4, 1, 2])

    async def test_manual_queries_neither_rotate_nor_move_the_background_position(self) -> None:
        async def stalled() -> object:
            await asyncio.sleep(10)

        self.listed(1, 2, 3)
        self.steam.details[2] = stalled
        with patch.object(provider, "FETCH_BATCH_TIMEOUT_SECONDS", 0.05), quiet():
            await self.provider.fetch_current_offers(tracked_app_ids=frozenset())
        self.steam.details[2] = details()
        self.steam.requests.clear()
        await self.provider.fetch_current_offers()
        self.assertEqual(self.steam.detail_order(), [1, 2, 3])
        self.steam.requests.clear()
        await self.provider.fetch_current_offers(tracked_app_ids=frozenset())
        self.assertEqual(self.steam.detail_order(), [2, 3, 1])

    async def test_detail_failures_are_counted_and_keep_the_app_active(self) -> None:
        failures = {
            "沒有回應": None, "success=false": {"success": False}, "沒有 type": details(type=None),
            "沒有 is_free": details(is_free=None), "沒有價格": details(price_overview=None),
            "沒有折扣": details(price_overview={"initial": 34900, "final": 0}),
        }
        for name, detail in failures.items():
            with self.subTest(name):
                self.listed(7, 8, detail=lambda app_id: detail if app_id == 7 else details())
                result = await self.provider.fetch_current_offers(tracked_app_ids=frozenset())
                self.assertEqual((result.active_app_ids, result.failed_app_count), (frozenset({7, 8}), 1))
                self.assertEqual([offer.app_id for offer in result.offers], [8])

    async def test_nameless_detail_without_search_name_is_a_failure(self) -> None:
        self.steam.search = {"items": [item(7, "   ")]}
        self.steam.details = {7: details(name=None)}
        self.assertIsNone(await self.provider.fetch_current_offers())
        self.steam.search = {"items": []}
        result = await self.provider.fetch_current_offers(tracked_app_ids=frozenset({7}))
        self.assertEqual((result.offers, result.failed_app_count), ((), 1))

    async def test_promotions_that_are_not_free_to_keep_leave_the_active_set(self) -> None:
        discounted = {"initial": 34900, "final": 17450, "discount_percent": 50}
        for name, detail in {
            "一般折扣": details(is_free=False, price_overview=discounted), "is_free=false": details(is_free=False),
            "DLC 限免": details(type="dlc"), "原價為 0": details(price_overview={"initial": 0, "final": 0, "discount_percent": 100}),
            "折扣未滿 100": details(price_overview={"initial": 34900, "final": 3490, "discount_percent": 90}),
        }.items():
            with self.subTest(name):
                self.listed(7, detail=lambda _app_id: detail)
                result = await self.provider.fetch_current_offers()
                self.assertEqual((result.active_app_ids, result.offers, result.failed_app_count), (frozenset(), (), 0))

    async def test_batch_timeout_returns_finished_offers_and_counts_the_rest(self) -> None:
        async def stalled() -> object:
            await asyncio.sleep(10)

        self.listed(1, 2, 3, 4)
        self.steam.details[2] = None
        self.steam.details[3] = stalled
        with patch.object(provider, "FETCH_BATCH_TIMEOUT_SECONDS", 0.05), self.assertLogs(level="WARNING"):
            result = await self.provider.fetch_current_offers(tracked_app_ids=frozenset())
        self.assertEqual(result.active_app_ids, frozenset({1, 2, 3, 4}))
        self.assertEqual([offer.app_id for offer in result.offers], [1])
        self.assertEqual(result.failed_app_count, 3)
        self.assertEqual(self.steam.detail_order(), [1, 2, 3])
        self.assertEqual(self.steam.asked_deadlines(), [])
        self.steam.requests.clear()
        self.steam.details[3] = details()
        await self.provider.fetch_current_offers(tracked_app_ids=frozenset())
        self.assertEqual(self.steam.detail_order(), [3, 4, 1, 2])

    async def test_search_timeout_returns_none(self) -> None:
        async def stalled() -> object:
            await asyncio.sleep(10)

        self.steam.search = stalled
        with patch.object(provider, "FETCH_BATCH_TIMEOUT_SECONDS", 0.05), self.assertLogs(level="WARNING"):
            self.assertIsNone(await self.provider.fetch_current_offers())

    async def test_free_to_keep_deadlines_are_attached(self) -> None:
        self.listed(1, 2)
        option = {"is_free_to_keep": True, "free_to_keep_ends": 1792083600}
        self.steam.items = {"response": {"store_items": [{"appid": 1, "best_purchase_option": option}]}}
        result = await self.provider.fetch_current_offers()
        self.assertEqual([offer.ends_at for offer in result.offers], [1792083600, None])
        self.assertEqual(self.steam.asked_deadlines(), [1, 2])

    async def test_unusable_deadlines_are_ignored(self) -> None:
        for items in (
            {"response": {"store_items": [{"appid": 1, "best_purchase_option": {"is_free_to_keep": False, "free_to_keep_ends": 9}}]}},
            {"response": {"store_items": [{"appid": 1, "best_purchase_option": {"is_free_to_keep": True}}]}},
            {"response": {"store_items": [None]}},
            {"response": {}},
            (503, None),
            aiohttp.ClientError(),
        ):
            with self.subTest(items=items):
                self.listed(1)
                self.steam.items = items
                with quiet():
                    result = await self.provider.fetch_current_offers()
                self.assertEqual([(offer.app_id, offer.ends_at) for offer in result.offers], [(1, None)])

    async def test_no_offers_ask_for_no_deadlines(self) -> None:
        self.listed(1, detail=lambda _app_id: details(type="dlc"))
        await self.provider.fetch_current_offers()
        self.assertEqual(self.steam.asked_deadlines(), [])
        self.assertEqual([url for url, _params in self.steam.requests].count(STEAM_GETITEMS_URL), 0)

    async def test_session_identifies_itself_and_closes(self) -> None:
        await self.provider.fetch_current_offers()
        self.assertIn("Mozilla/5.0", self.steam.sessions[0]["headers"]["User-Agent"])
        await self.provider.close()
        self.assertTrue(self.steam.closed)
        self.assertIsNone(await self.provider.fetch_current_offers())
        self.assertEqual(len(self.steam.sessions), 1)


if __name__ == "__main__":
    unittest.main()
