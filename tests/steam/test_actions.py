import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.app.ports import Actor
from src.steam.actions import steam_actions
from src.steam.models import SteamFetchResult, SteamOffer
from src.testing import text

ADMIN = Actor(1, "管理員", "", frozenset(), True)


class SteamQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_tells_failure_from_no_offers(self) -> None:
        for result, shown in (
            (None, "無法取得"),
            (SteamFetchResult(frozenset(), (), failed_app_count=3), "無法取得"),
            (SteamFetchResult(frozenset(), ()), "沒有符合條件"),
            (SteamFetchResult(frozenset(), (SteamOffer(1, "遊戲", "NT$1", "", (), ""),), failed_app_count=2), "資料不完整：2 款"),
        ):
            with self.subTest(shown=shown):
                steam = SimpleNamespace(fetch_current_offers=AsyncMock(return_value=result))
                (query,) = [spec for spec in steam_actions(steam, SimpleNamespace()).specs if spec.name == "steam_query"]
                self.assertTrue(query.admin)
                self.assertIn(shown, text((await query.effect.render(ADMIN))["view"]))


if __name__ == "__main__":
    unittest.main()
