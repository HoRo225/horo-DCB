from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
import logging
import re
from urllib.parse import urlparse

import aiohttp


REQUEST_TIMEOUT_SECONDS = 30
FETCH_BATCH_TIMEOUT_SECONDS = 90
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) "
    "Gecko/20100101 Firefox/134.0"
)
STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_APP_ID_PATTERN = re.compile(r"/apps/(\d+)/")


class _SteamDetailsFailure(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SteamOffer:
    app_id: int
    name: str
    old_price: str
    description: str
    developers: tuple[str, ...]
    header_image: str | None

    @property
    def store_url(self) -> str:
        return f"https://store.steampowered.com/app/{self.app_id}/"


@dataclass(frozen=True, slots=True)
class SteamFetchResult:
    active_app_ids: frozenset[int]
    offers: tuple[SteamOffer, ...]
    failed_app_count: int = 0


class SteamOfferProvider:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._closed = False
        self._fetches: set[asyncio.Task[object]] = set()

    async def close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        pending = {task for task in self._fetches if task is not current}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @staticmethod
    def _extract_app_id(logo_url: object) -> int | None:
        if not isinstance(logo_url, str):
            return None
        match = _APP_ID_PATTERN.search(logo_url)
        if match is None:
            return None
        try:
            app_id = int(match.group(1))
        except ValueError:
            return None
        return app_id if app_id > 0 else None

    @staticmethod
    def _clean_text(value: object, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(html.unescape(value).split())[:limit]

    @staticmethod
    def _safe_https_url(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = urlparse(value)
        except ValueError:
            return None
        return value if parsed.scheme == "https" and parsed.netloc else None

    async def _request_json(
        self, url: str, *, params: dict[str, str] | None = None,
    ) -> object | None:
        if self._session is None or self._session.closed:
            return None
        try:
            async with self._session.get(url, params=params) as response:
                if response.status != 200:
                    logging.warning("Steam 免費遊戲請求失敗：HTTP %s", response.status)
                    return None
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            logging.exception("Steam 免費遊戲 HTTP 請求失敗。")
            return None

    async def _fetch_offer(self, app_id: int, fallback_name: str) -> SteamOffer | None:
        payload = await self._request_json(
            STEAM_APPDETAILS_URL,
            params={
                "appids": str(app_id),
                "l": "tchinese",
                "filters": "basic,short_description,developers,price_overview",
            },
        )
        if not isinstance(payload, dict):
            raise _SteamDetailsFailure
        entry = payload.get(str(app_id))
        if not isinstance(entry, dict):
            raise _SteamDetailsFailure
        success = entry.get("success")
        if type(success) is not bool:
            raise _SteamDetailsFailure
        if not success:
            raise _SteamDetailsFailure
        data = entry.get("data")
        if not isinstance(data, dict):
            raise _SteamDetailsFailure
        if "type" not in data:
            raise _SteamDetailsFailure
        if data["type"] != "game":
            return None
        is_free = data.get("is_free")
        if type(is_free) is not bool:
            raise _SteamDetailsFailure
        if not is_free:
            return None
        price = data.get("price_overview")
        if not isinstance(price, dict):
            return None
        initial = price.get("initial")
        if (
            type(initial) is not int
            or initial <= 0
            or price.get("discount_percent") != 100
        ):
            return None
        name = self._clean_text(data.get("name"), 200) or self._clean_text(
            fallback_name, 200,
        )
        if not name:
            return None
        developers_value = data.get("developers")
        developers: tuple[str, ...] = ()
        if isinstance(developers_value, list):
            developers = tuple(
                cleaned
                for item in developers_value[:5]
                if (cleaned := self._clean_text(item, 100))
            )
        return SteamOffer(
            app_id=app_id,
            name=name,
            old_price=self._clean_text(price.get("initial_formatted"), 80),
            description=self._clean_text(data.get("short_description"), 1000),
            developers=developers,
            header_image=self._safe_https_url(data.get("header_image")),
        )

    async def fetch_current_offers(self) -> SteamFetchResult | None:
        if self._closed:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                headers={"User-Agent": USER_AGENT},
            )
        active_app_ids: frozenset[int] | None = None
        offers: list[SteamOffer] = []
        completed_count = 0
        explicit_failed_count = 0
        task = asyncio.current_task()
        if task is not None:
            self._fetches.add(task)
        try:
            async with asyncio.timeout(FETCH_BATCH_TIMEOUT_SECONDS):
                payload = await self._request_json(
                    STEAM_SEARCH_URL,
                    params={
                        "maxprice": "free",
                        "specials": "1",
                        "category1": "994,998,21",
                        "json": "1",
                        "l": "tchinese",
                    },
                )
                if not isinstance(payload, dict):
                    return None
                items = payload.get("items")
                if not isinstance(items, list) or len(items) > 200:
                    logging.error("Steam 免費遊戲搜尋回傳格式不正確。")
                    return None
                search_items: dict[int, str] = {}
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    app_id = self._extract_app_id(item.get("logo"))
                    name = self._clean_text(item.get("name"), 200)
                    if app_id is not None and name:
                        search_items.setdefault(app_id, name)
                active_app_ids = frozenset(search_items)
                for app_id, name in search_items.items():
                    try:
                        offer = await self._fetch_offer(app_id, name)
                    except _SteamDetailsFailure:
                        explicit_failed_count += 1
                        completed_count += 1
                        continue
                    completed_count += 1
                    if offer is not None:
                        offers.append(offer)
                pending_count = max(0, len(active_app_ids) - completed_count)
                return SteamFetchResult(
                    active_app_ids, tuple(offers),
                    failed_app_count=explicit_failed_count + pending_count,
                )
        except TimeoutError:
            logging.warning(
                "Steam 免費遊戲整批查詢超過 %s 秒，已取消。",
                FETCH_BATCH_TIMEOUT_SECONDS,
            )
            if active_app_ids is None:
                return None
            pending_count = max(0, len(active_app_ids) - completed_count)
            return SteamFetchResult(
                active_app_ids, tuple(offers),
                failed_app_count=explicit_failed_count + pending_count,
            )
        finally:
            if task is not None:
                self._fetches.discard(task)
