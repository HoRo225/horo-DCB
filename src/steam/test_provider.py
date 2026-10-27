from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from src.steam.models import SteamOffer
from src.steam.provider import SteamOfferProvider, _SteamDetailsFailure


def item(app_id: int, name: str) -> dict[str, str]:
    return {
        "name": name,
        "logo": f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{app_id}/logo.jpg",
    }


def offer(app_id: int) -> SteamOffer:
    return SteamOffer(app_id, f"game{app_id}", "NT$100", "desc", ("dev",), None)


class SteamOfferProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = SteamOfferProvider()
        self.provider._session = MagicMock(closed=False, close=AsyncMock())
        self.provider._request_json = AsyncMock()
        self.provider._fetch_offer = AsyncMock()
        self.provider._with_deadlines = AsyncMock(side_effect=lambda offers: offers)
        self.addAsyncCleanup(self.provider.close)

    async def test_invalid_search_payload_or_shape_returns_none(self) -> None:
        invalid = (
            None,
            [],
            {},
            {"items": None},
            {"items": [item(1, "game")] * 201},
            {"items": [{}]},
            {"items": [{"name": "game", "logo": ""}]},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.provider._request_json.return_value = payload
                self.assertIsNone(await self.provider.fetch_current_offers())
        self.provider._fetch_offer.assert_not_awaited()

    async def test_duplicate_app_ids_keep_first_name_and_invalid_logos_are_skipped(self) -> None:
        self.provider._request_json.return_value = {
            "items": [
                item(2, "  first   name  "),
                item(2, "second name"),
                {"name": "not an app", "logo": "https://example.com/logo.jpg"},
                item(0, "zero"),
            ]
        }
        self.provider._fetch_offer.return_value = offer(2)

        result = await self.provider.fetch_current_offers()

        self.assertEqual(result.active_app_ids, frozenset({2}))
        self.assertEqual(result.offers, (offer(2),))
        self.provider._fetch_offer.assert_awaited_once_with(2, "first name")

    async def test_tracked_ids_join_search_and_cursor_wraps_sorted_work(self) -> None:
        self.provider._detail_cursor = 2
        self.provider._request_json.return_value = {"items": [item(3, "three"), item(1, "one")]}
        self.provider._fetch_offer.side_effect = lambda app_id, _name: offer(app_id)

        result = await self.provider.fetch_current_offers(tracked_app_ids=frozenset({2, 4}))

        self.assertEqual(result.active_app_ids, frozenset({1, 2, 3, 4}))
        self.assertEqual([entry.app_id for entry in result.offers], [3, 4, 1, 2])
        self.assertEqual(
            [call.args for call in self.provider._fetch_offer.await_args_list],
            [(3, "three"), (4, ""), (1, "one"), (2, "")],
        )
        self.assertEqual(self.provider._detail_cursor, 2)

    async def test_explicit_detail_failure_advances_cursor_and_is_counted(self) -> None:
        self.provider._request_json.return_value = {"items": [item(7, "seven")]}
        self.provider._fetch_offer.side_effect = _SteamDetailsFailure

        result = await self.provider.fetch_current_offers(tracked_app_ids=frozenset())

        self.assertEqual(result.active_app_ids, frozenset({7}))
        self.assertEqual(result.offers, ())
        self.assertEqual(result.failed_app_count, 1)
        self.assertEqual(self.provider._detail_cursor, 7)

    async def test_timeout_returns_completed_offers_and_counts_failure_plus_pending(self) -> None:
        self.provider._request_json.return_value = {
            "items": [item(app_id, f"game{app_id}") for app_id in range(1, 5)]
        }
        self.provider._fetch_offer.side_effect = [offer(1), _SteamDetailsFailure(), TimeoutError()]

        with self.assertLogs(level="WARNING"):
            result = await self.provider.fetch_current_offers(tracked_app_ids=frozenset())

        self.assertEqual(result.active_app_ids, frozenset({1, 2, 3, 4}))
        self.assertEqual(result.offers, (offer(1),))
        self.assertEqual(result.failed_app_count, 3)
        self.assertEqual(self.provider._detail_cursor, 2)
        self.assertEqual(self.provider._fetch_offer.await_count, 3)
        self.provider._with_deadlines.assert_not_awaited()
        self.assertFalse(self.provider._fetches)


def details(app_id: int = 10, **data: object) -> dict[str, object]:
    fields = {
        "type": "game",
        "name": "Free &amp; Fun",
        "is_free": True,
        "short_description": "  short\n desc ",
        "developers": ["Dev A", "", "Dev B"],
        "header_image": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/10/header.jpg",
        "price_overview": {
            "currency": "TWD",
            "initial": 34900,
            "final": 0,
            "discount_percent": 100,
            "initial_formatted": "NT$ 349",
            "final_formatted": "NT$ 0",
        },
        **data,
    }
    return {str(app_id): {"success": True, "data": {k: v for k, v in fields.items() if v is not None}}}


class FetchOfferTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = SteamOfferProvider()
        self.provider._request_json = AsyncMock()

    async def fetch(self, payload: object, fallback: str = "fallback") -> SteamOffer | None:
        self.provider._request_json.return_value = payload
        return await self.provider._fetch_offer(10, fallback)

    async def test_free_to_keep_promotion_becomes_offer(self) -> None:
        result = await self.fetch(details())

        self.assertEqual(
            result,
            SteamOffer(
                10,
                "Free & Fun",
                "NT$ 349",
                "short desc",
                ("Dev A", "Dev B"),
                "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/10/header.jpg",
            ),
        )
        self.assertEqual(self.provider._request_json.await_args.kwargs["params"]["appids"], "10")
        self.assertEqual((await self.fetch(details(name=""))).name, "fallback")

    async def test_permanently_free_game_without_price_is_not_offer(self) -> None:
        with self.assertRaises(_SteamDetailsFailure):
            await self.fetch(details(price_overview=None))

    async def test_non_free_to_keep_details_return_none(self) -> None:
        discounted = {"initial": 34900, "final": 17450, "discount_percent": 50}
        cases = {
            "一般折扣": details(is_free=False, price_overview=discounted),
            "is_free=false": details(is_free=False),
            "DLC 限免": details(type="dlc"),
            "原價為 0": details(price_overview={"initial": 0, "final": 0, "discount_percent": 100}),
            "折扣未滿 100": details(price_overview={"initial": 34900, "final": 3490, "discount_percent": 90}),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                self.assertIsNone(await self.fetch(payload))

    async def test_missing_or_invalid_fields_raise_failure(self) -> None:
        cases = {
            "沒有回應": (None, "fallback"),
            "沒有這個 app": ({}, "fallback"),
            "success=false": ({"10": {"success": False}}, "fallback"),
            "沒有 type": (details(type=None), "fallback"),
            "沒有 is_free": (details(is_free=None), "fallback"),
            "沒有折扣": (details(price_overview={"initial": 34900, "final": 0}), "fallback"),
            "沒有名稱": (details(name=None), ""),
        }
        for name, (payload, fallback) in cases.items():
            with self.subTest(name):
                with self.assertRaises(_SteamDetailsFailure):
                    await self.fetch(payload, fallback)


if __name__ == "__main__":
    unittest.main()
