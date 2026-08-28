import asyncio
import json
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import AsyncMock

from src.agent_tools import (
    AgentTools,
    ResearchContext,
    ResearchImage,
    ResearchSource,
    ToolContext,
)
from src.ai_client import (
    WebFetchResponse,
    WebSearchImage,
    WebSearchResponse,
    WebSearchResult,
)
from src.steam_free_games import SteamFetchResult, SteamOffer


def search_result(
    title=None,
    url=None,
    snippet=None,
    published_at=None,
    image_url=None,
):
    return WebSearchResult(title, url, snippet, published_at, image_url)


def search_response(results=(), images=()):
    return WebSearchResponse(tuple(results), tuple(images))


def fetch_response(content="Fetched content", title="Fetched title"):
    return WebFetchResponse(title, content)


class StubNotifier:
    def __init__(self, result=None):
        self.result = result
        self.fetch_calls = 0

    async def fetch_current_offers(self):
        self.fetch_calls += 1
        return self.result


class StubAIClient:
    def __init__(self):
        self.search_response = search_response()
        self.fetch_response = fetch_response()
        self.search_error = None
        self.fetch_error = None
        self.search_calls = []
        self.fetch_calls = []

    async def web_search(self, query, *, provider, search_type="web", include_images=False):
        self.search_calls.append(
            {"query": query, "provider": provider, "search_type": search_type, "include_images": include_images}
        )
        if self.search_error is not None:
            raise self.search_error
        return self.search_response

    async def web_fetch(
        self,
        url,
        *,
        provider,
        content_format="markdown",
        max_characters=15000,
    ):
        call = {"url": url, "provider": provider}
        if content_format != "markdown":
            call["content_format"] = content_format
        if max_characters != 15000:
            call["max_characters"] = max_characters
        self.fetch_calls.append(call)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_response


class AgentToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.notifier = StubNotifier(SteamFetchResult(frozenset(), ()))
        self.ai_client = StubAIClient()
        self.tools = AgentTools(
            self.notifier,
            self.ai_client,
            search_provider="trusted-search",
            image_search_provider="trusted-images",
            fetch_provider="trusted-fetch",
        )
        self.context = ToolContext(
            guild_name="測試伺服器",
            channel_name="遊戲情報",
            channel_type="text",
        )
        self.research_context = ResearchContext()

    @staticmethod
    def offer(app_id, *, description="Description", developers=("Developer",)):
        return SteamOffer(
            app_id=app_id,
            name=f"Game {app_id}",
            old_price="NT$ 100",
            description=description,
            developers=developers,
            header_image=None,
        )

    def test_tool_context_is_frozen_and_slotted(self):
        self.assertEqual(
            ToolContext.__slots__, ("guild_name", "channel_name", "channel_type")
        )
        self.assertFalse(hasattr(self.context, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            self.context.channel_name = "changed"

    def test_research_context_has_independent_mutable_state(self):
        first = ResearchContext()
        second = ResearchContext()
        first.sources.append(
            ResearchSource(1, "Title", "https://example.com", "", None)
        )
        first.allowed_fetch_urls.add("https://example.com")
        first.search_calls = 1

        self.assertEqual(second.sources, [])
        self.assertEqual(second.allowed_fetch_urls, set())
        self.assertEqual(second.reply_images, [])
        self.assertEqual(second.search_calls, 0)
        self.assertFalse(hasattr(first, "__dict__"))

    def test_schemas_contain_exactly_four_safe_function_tools(self):
        schemas = self.tools.schemas

        self.assertEqual(len(schemas), 4)
        self.assertEqual(
            [schema["function"]["name"] for schema in schemas],
            [
                "get_current_channel_info",
                "get_steam_free_games",
                "web_search",
                "web_fetch",
            ],
        )
        for schema in schemas:
            self.assertEqual(schema["type"], "function")
            self.assertTrue(schema["function"]["description"])

        for schema in schemas[:2]:
            self.assertEqual(
                schema["function"]["parameters"],
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            )
            self.assertNotIn("id", schema["function"]["parameters"]["properties"])

        search_parameters = schemas[2]["function"]["parameters"]
        self.assertEqual(set(search_parameters["properties"]), {"query", "search_type", "include_images"})
        self.assertEqual(search_parameters["required"], ["query", "search_type"])
        self.assertEqual(
            search_parameters["properties"]["search_type"]["enum"],
            ["web", "news"],
        )
        self.assertEqual(search_parameters["properties"]["include_images"], {"type": "boolean"})
        self.assertNotIn("include_images", search_parameters["required"])
        fetch_parameters = schemas[3]["function"]["parameters"]
        self.assertEqual(set(fetch_parameters["properties"]), {"url"})
        self.assertEqual(fetch_parameters["required"], ["url"])
        for parameters in (search_parameters, fetch_parameters):
            self.assertFalse(parameters["additionalProperties"])
            self.assertTrue(
                set(parameters["properties"]).isdisjoint(
                    {"provider", "model", "key", "headers", "method"}
                )
            )

    def test_empty_tool_schemas_have_independent_properties(self):
        schemas = self.tools.schemas
        schemas[0]["function"]["parameters"]["properties"]["changed"] = {}

        self.assertEqual(schemas[1]["function"]["parameters"]["properties"], {})
        self.assertEqual(
            self.tools.schemas[0]["function"]["parameters"]["properties"], {}
        )

    async def test_channel_info_uses_runtime_context(self):
        payload = json.loads(
            await self.tools.execute(
                "get_current_channel_info",
                "{}",
                self.context,
                self.research_context,
            )
        )

        self.assertEqual(
            payload,
            {
                "ok": True,
                "guild_name": "測試伺服器",
                "channel_name": "遊戲情報",
                "channel_type": "text",
            },
        )
        self.assertEqual(self.notifier.fetch_calls, 0)

    async def test_steam_tool_awaits_injected_fetch(self):
        offer = self.offer(10)
        self.notifier.result = SteamFetchResult(frozenset({10}), (offer,))

        payload = json.loads(
            await self.tools.execute(
                "get_steam_free_games", "{}", self.context, self.research_context
            )
        )

        self.assertEqual(self.notifier.fetch_calls, 1)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 1)

    async def test_steam_unavailable_when_fetch_returns_none(self):
        self.notifier.result = None

        payload = json.loads(
            await self.tools.execute(
                "get_steam_free_games", "{}", self.context, self.research_context
            )
        )

        self.assertEqual(payload, {"ok": False, "error": "steam_unavailable"})
        self.assertEqual(self.notifier.fetch_calls, 1)

    async def test_steam_response_limits_offers_but_reports_full_total(self):
        offers = tuple(self.offer(app_id) for app_id in range(1, 26))
        self.notifier.result = SteamFetchResult(
            frozenset(range(1, 26)),
            offers,
        )

        payload = json.loads(
            await self.tools.execute(
                "get_steam_free_games", "{}", self.context, self.research_context
            )
        )

        self.assertEqual(payload["total"], 25)
        self.assertEqual(len(payload["offers"]), 20)
        self.assertEqual(payload["offers"][-1]["name"], "Game 20")

    async def test_offer_serialization_truncates_and_uses_store_url(self):
        offer = self.offer(
            42,
            description="界" * 301,
            developers=("A", "B", "C", "D", "E", "F"),
        )
        self.notifier.result = SteamFetchResult(frozenset({42}), (offer,))

        raw = await self.tools.execute(
            "get_steam_free_games", "{}", self.context, self.research_context
        )
        serialized = json.loads(raw)["offers"][0]

        self.assertEqual(
            set(serialized),
            {"name", "old_price", "description", "developers", "store_url"},
        )
        self.assertEqual(serialized["description"], "界" * 300)
        self.assertEqual(serialized["developers"], ["A", "B", "C", "D", "E"])
        self.assertEqual(
            serialized["store_url"],
            "https://store.steampowered.com/app/42/",
        )
        self.assertIn("界", raw)
        self.assertNotIn("\\u754c", raw)

    async def test_invalid_arguments_are_rejected(self):
        invalid_arguments = ("not-json", "[]", "null", '"value"', '{"id": 1}')

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                payload = json.loads(
                    await self.tools.execute(
                        "get_current_channel_info",
                        arguments,
                        self.context,
                        self.research_context,
                    )
                )
                self.assertEqual(
                    payload,
                    {"ok": False, "error": "invalid_arguments"},
                )

    async def test_unknown_tool_is_not_available(self):
        payload = json.loads(
            await self.tools.execute(
                "unknown", "{}", self.context, self.research_context
            )
        )

        self.assertEqual(payload, {"ok": False, "error": "tool_not_available"})
        self.assertEqual(self.notifier.fetch_calls, 0)

    async def test_tool_failure_does_not_leak_exception_detail(self):
        secret_detail = "sensitive-fetch-detail"

        async def fail():
            raise RuntimeError(secret_detail)

        self.notifier.fetch_current_offers = fail

        with self.assertLogs(level="ERROR") as captured:
            raw = await self.tools.execute(
                "get_steam_free_games", "{}", self.context, self.research_context
            )

        self.assertEqual(json.loads(raw), {"ok": False, "error": "tool_failed"})
        self.assertNotIn(secret_detail, raw)
        self.assertNotIn(secret_detail, "\n".join(captured.output))

    async def test_dispatched_tool_failure_does_not_leak_exception_detail(self):
        secret_detail = "dispatched-handler-secret"
        dispatched_tools = (
            ("web_search", "_execute_web_search", '{"query":"topic","search_type":"web"}'),
            ("search_channel_memory", "_execute_memory_search", '{"query":"topic"}'),
            ("calendar_get_events", "_execute_calendar_get_events", "{}"),
        )

        for name, handler_name, arguments in dispatched_tools:
            with self.subTest(name=name):
                handler = AsyncMock(side_effect=RuntimeError(secret_detail))
                setattr(self.tools, handler_name, handler)
                with self.assertLogs(level="ERROR") as captured:
                    raw = await self.tools.execute(
                        name, arguments, self.context, ResearchContext()
                    )

                self.assertEqual(
                    json.loads(raw), {"ok": False, "error": "tool_failed"}
                )
                self.assertNotIn(secret_detail, raw)
                self.assertNotIn(secret_detail, "\n".join(captured.output))

    async def test_dispatched_tool_cancellation_propagates(self):
        self.tools._execute_web_search = AsyncMock(
            side_effect=asyncio.CancelledError()
        )

        with self.assertRaises(asyncio.CancelledError):
            await self.tools.execute(
                "web_search",
                '{"query":"topic","search_type":"web"}',
                self.context,
                self.research_context,
            )

    async def test_web_search_uses_validated_arguments_and_trusted_provider(self):
        self.ai_client.search_response = search_response(
            [
                search_result(
                    "Current result",
                    "https://example.com/current",
                    "Current summary",
                    "2026-08-19",
                )
            ]
        )

        payload = json.loads(
            await self.tools.execute(
                "web_search",
                json.dumps({"query": "  current topic  ", "search_type": "news"}),
                self.context,
                self.research_context,
            )
        )

        self.assertEqual(
            self.ai_client.search_calls,
            [
                {
                    "query": "current topic",
                    "provider": "trusted-search",
                    "search_type": "news",
                    "include_images": False,
                }
            ],
        )
        self.assertEqual(
            payload,
            {
                "ok": True,
                "total": 1,
                "sources": [
                    {
                        "source_id": 1,
                        "title": "Current result",
                        "url": "https://example.com/current",
                        "snippet": "Current summary",
                        "published_at": "2026-08-19",
                    }
                ],
            },
        )
        self.assertEqual(self.research_context.search_calls, 1)

    async def test_web_search_rejects_invalid_arguments_without_client_call(self):
        invalid_arguments = (
            {"query": "", "search_type": "web"},
            {"query": "   ", "search_type": "web"},
            {"query": "x" * 301, "search_type": "web"},
            {"query": "topic", "search_type": "images"},
            {"query": "topic", "search_type": ["web"]},
            {"query": "topic", "search_type": "web", "provider": "attacker"},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                payload = json.loads(
                    await self.tools.execute(
                        "web_search",
                        json.dumps(arguments),
                        self.context,
                        self.research_context,
                    )
                )
                self.assertEqual(
                    payload, {"ok": False, "error": "invalid_arguments"}
                )

        self.assertEqual(self.ai_client.search_calls, [])
        self.assertEqual(self.research_context.search_calls, 0)

    async def test_web_search_limits_and_bounds_normalized_results(self):
        urls = [f"https://example.com/result/{index}" for index in range(6)]
        self.ai_client.search_response = search_response(
            [
                search_result("T" * 201, url, "S" * 1001, "2026-08-19")
                for url in urls
            ]
        )

        payload = json.loads(
            await self.tools.execute(
                "web_search",
                '{"query":"topic","search_type":"web"}',
                self.context,
                self.research_context,
            )
        )

        self.assertEqual(payload["total"], 5)
        self.assertEqual(len(payload["sources"]), 5)
        self.assertEqual(
            [source["source_id"] for source in payload["sources"]],
            [1, 2, 3, 4, 5],
        )
        self.assertTrue(all(len(source["title"]) == 200 for source in payload["sources"]))
        self.assertTrue(
            all(len(source["snippet"]) == 1000 for source in payload["sources"])
        )
        self.assertTrue(all(len(source["url"]) <= 2048 for source in payload["sources"]))
        self.assertEqual(self.research_context.allowed_fetch_urls, set(urls[:5]))
        self.assertNotIn(urls[5], self.research_context.allowed_fetch_urls)

    async def test_web_search_skips_unsafe_or_malformed_result_urls(self):
        bad_urls = (
            None,
            "",
            "not-a-url",
            "http:///missing-host",
            "https://example.com:invalid-port/page",
            "http://user:password@example.com/page",
            "https://example.com/" + "x" * 2049,
        )

        for bad_url in bad_urls:
            with self.subTest(url=bad_url):
                research_context = ResearchContext()
                self.ai_client.search_response = search_response(
                    [search_result("Unsafe", bad_url, "Ignored")]
                )

                payload = json.loads(
                    await self.tools.execute(
                        "web_search",
                        '{"query":"topic","search_type":"web"}',
                        self.context,
                        research_context,
                    )
                )

                self.assertEqual(payload, {"ok": True, "total": 0, "sources": []})
                self.assertEqual(research_context.sources, [])
                self.assertEqual(research_context.allowed_fetch_urls, set())

    async def test_image_search_collects_safe_unique_images_without_fetch_allowlist(self):
        self.ai_client.search_response = search_response(
            [
                search_result(
                    str(index),
                    f"https://example.com/{index}",
                    image_url=image_url,
                )
                for index, image_url in enumerate(
                    (
                        "https://images.example.com/1.jpg",
                        "https://images.example.com/1.jpg",
                        "http://127.0.0.1/private.jpg",
                        "https://images.example.com/2.jpg",
                        "https://images.example.com/3.jpg",
                        "https://images.example.com/4.jpg",
                        "https://images.example.com/5.jpg",
                    )
                )
            ]
        )

        await self.tools.execute(
            "web_search",
            '{"query":"topic","search_type":"web","include_images":true}',
            self.context,
            self.research_context,
        )

        self.assertEqual(
            [(image.url, image.description) for image in self.research_context.reply_images],
            [
                ("https://images.example.com/1.jpg", "0"),
                ("https://images.example.com/2.jpg", "3"),
                ("https://images.example.com/3.jpg", "4"),
            ],
        )
        self.assertFalse(
            set(image.url for image in self.research_context.reply_images)
            & self.research_context.allowed_fetch_urls
        )

    async def test_image_search_uses_dedicated_provider(self):
        await self.tools.execute(
            "web_search",
            '{"query":"topic","search_type":"web","include_images":true}',
            self.context,
            self.research_context,
        )

        self.assertEqual(self.ai_client.search_calls[0]["provider"], "trusted-images")

    async def test_failed_image_provider_retries_general_search_provider(self):
        class FailingImageProviderClient(StubAIClient):
            async def web_search(
                self,
                query,
                *,
                provider,
                search_type="web",
                include_images=False,
            ):
                self.search_calls.append(
                    {
                        "query": query,
                        "provider": provider,
                        "search_type": search_type,
                        "include_images": include_images,
                    }
                )
                if provider == "trusted-images":
                    raise RuntimeError("image provider unavailable")
                return search_response(
                    [
                        search_result(
                            "Fallback source",
                            "https://example.com/fallback",
                            image_url="https://images.example.com/fallback.jpg",
                        )
                    ]
                )

        ai_client = FailingImageProviderClient()
        tools = AgentTools(
            self.notifier,
            ai_client,
            search_provider="trusted-search",
            image_search_provider="trusted-images",
            fetch_provider="trusted-fetch",
        )
        research = ResearchContext()

        payload = json.loads(
            await tools.execute(
                "web_search",
                '{"query":"topic","search_type":"web","include_images":true}',
                self.context,
                research,
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["image_count"], 1)
        self.assertEqual(
            [call["provider"] for call in ai_client.search_calls],
            ["trusted-images", "trusted-search"],
        )
        self.assertEqual(
            research.reply_images,
            [
                ResearchImage(
                    "https://images.example.com/fallback.jpg",
                    "Fallback source",
                )
            ],
        )

    async def test_image_search_falls_back_to_allowed_html_page(self):
        source_url = "https://example.com/gallery"
        image_url = "https://cdn.example.com/dish.jpg?width=1200&height=800"
        self.ai_client.search_response = search_response(
            [search_result("Official gallery", source_url, "Gallery")]
        )
        self.ai_client.fetch_response = fetch_response(
            f'<meta property="og:image" content="{image_url}">'
            f'<img src="{image_url}">'
            '<img src="http://127.0.0.1/private.jpg">',
            title=None,
        )

        payload = json.loads(
            await self.tools.execute(
                "web_search",
                '{"query":"food photos","search_type":"web","include_images":true}',
                self.context,
                self.research_context,
            )
        )

        self.assertEqual(payload["image_count"], 1)
        self.assertEqual(
            self.research_context.reply_images,
            [ResearchImage(image_url, "Official gallery")],
        )
        self.assertEqual(
            self.ai_client.fetch_calls,
            [
                {
                    "url": source_url,
                    "provider": "trusted-fetch",
                    "content_format": "html",
                    "max_characters": 50000,
                }
            ],
        )
        self.assertEqual(self.research_context.fetch_calls, 1)
        self.assertEqual(self.research_context.allowed_fetch_urls, {source_url})
        self.assertNotIn(
            image_url,
            self.research_context.allowed_fetch_urls,
        )

    async def test_image_fallback_uses_only_one_source_fetch(self):
        class SequentialFetchClient(StubAIClient):
            def __init__(self):
                super().__init__()
                self.fetch_responses = [
                    fetch_response("No image on the first source", title=None),
                    fetch_response(
                        "https://cdn.example.com/second-source.jpg", title=None
                    ),
                ]

            async def web_fetch(
                self,
                url,
                *,
                provider,
                content_format="markdown",
                max_characters=15000,
            ):
                self.fetch_calls.append(
                    {
                        "url": url,
                        "provider": provider,
                        "content_format": content_format,
                        "max_characters": max_characters,
                    }
                )
                return self.fetch_responses.pop(0)

        ai_client = SequentialFetchClient()
        ai_client.search_response = search_response(
            [
                search_result("First", "https://example.com/first"),
                search_result("Second", "https://example.com/second"),
            ]
        )
        tools = AgentTools(
            self.notifier,
            ai_client,
            search_provider="trusted-search",
            image_search_provider="trusted-images",
            fetch_provider="trusted-fetch",
        )
        research = ResearchContext()

        payload = json.loads(
            await tools.execute(
                "web_search",
                '{"query":"topic","search_type":"web","include_images":true}',
                self.context,
                research,
            )
        )

        self.assertEqual(payload["image_count"], 0)
        self.assertEqual(len(ai_client.fetch_calls), 1)
        self.assertEqual(ai_client.fetch_calls[0]["url"], "https://example.com/first")
        self.assertEqual(research.fetch_calls, 1)

    async def test_image_fallback_skips_social_profile_and_uses_markdown_alt_images(self):
        social_url = "https://www.instagram.com/eatogether_tw"
        content_url = "https://booking.example.com/eatogether"

        class SourceAwareFetchClient(StubAIClient):
            async def web_fetch(
                self,
                url,
                *,
                provider,
                content_format="markdown",
                max_characters=15000,
            ):
                self.fetch_calls.append(
                    {
                        "url": url,
                        "provider": provider,
                        "content_format": content_format,
                        "max_characters": max_characters,
                    }
                )
                if url == social_url:
                    return fetch_response(
                        "![highlight story picture]"
                        "(https://images.example.com/highlight.jpg)",
                        title=None,
                    )
                return fetch_response(
                    "".join(
                        (
                            "![EZTABLE Logo](https://images.example.com/logo.png)",
                            "![台北信義店 訂位](https://images.example.com/xinyi.webp)",
                            "![台北京站店 訂位](https://images.example.com/q-square.webp)",
                            "![新北板橋店 訂位](https://images.example.com/banqiao.webp)",
                            "![林口三井店 訂位](https://images.example.com/linkou.webp)",
                        )
                    ),
                    title=None,
                )

        ai_client = SourceAwareFetchClient()
        ai_client.search_response = search_response(
            [
                search_result("Official Instagram", social_url),
                search_result("饗食天堂訂位", content_url),
            ]
        )
        tools = AgentTools(
            self.notifier,
            ai_client,
            search_provider="trusted-search",
            image_search_provider="trusted-images",
            fetch_provider="trusted-fetch",
        )
        research = ResearchContext()

        payload = json.loads(
            await tools.execute(
                "web_search",
                '{"query":"饗食天堂 圖片","search_type":"web","include_images":true}',
                self.context,
                research,
            )
        )

        self.assertEqual(payload["image_count"], 4)
        self.assertEqual(
            ai_client.fetch_calls,
            [
                {
                    "url": content_url,
                    "provider": "trusted-fetch",
                    "content_format": "html",
                    "max_characters": 50000,
                }
            ],
        )
        self.assertEqual(
            [(image.url, image.description) for image in research.reply_images],
            [
                ("https://images.example.com/xinyi.webp", "台北信義店 訂位"),
                ("https://images.example.com/q-square.webp", "台北京站店 訂位"),
                ("https://images.example.com/banqiao.webp", "新北板橋店 訂位"),
                ("https://images.example.com/linkou.webp", "林口三井店 訂位"),
            ],
        )

    def test_social_profile_detection_allows_instagram_post_and_reel(self):
        self.assertTrue(
            self.tools._is_social_profile_url(
                "https://www.instagram.com/eatogether_tw"
            )
        )
        self.assertFalse(
            self.tools._is_social_profile_url(
                "https://www.instagram.com/p/post-id/"
            )
        )
        self.assertFalse(
            self.tools._is_social_profile_url(
                "https://www.instagram.com/reel/reel-id/"
            )
        )

    async def test_image_fallback_prefers_structured_page_metadata(self):
        source_url = "https://example.com/gallery"
        hero_url = "https://cdn.example.com/hero.jpg"
        logo_url = "https://cdn.example.com/logo.png"
        self.ai_client.search_response = search_response(
            [search_result("Gallery", source_url)]
        )
        self.ai_client.fetch_response = fetch_response(
            f'<img src="{logo_url}">'
            f'<meta property="og:image" content="{hero_url}">',
            title=None,
        )

        await self.tools.execute(
            "web_search",
            '{"query":"topic","search_type":"web","include_images":true}',
            self.context,
            self.research_context,
        )

        self.assertEqual(self.research_context.reply_images[0].url, hero_url)
        self.assertIn(
            ResearchImage(logo_url, "Gallery"),
            self.research_context.reply_images,
        )

    def test_markdown_image_url_stops_before_outer_link_suffix(self):
        image_url = "https://cdn.example.com/dish.jpg?width=1200&height=800"
        page_text = f"[![Dish photo]({image_url})](/post)"

        self.assertEqual(
            self.tools._extract_safe_image_urls(page_text),
            [image_url],
        )

    def test_unanchored_direct_image_url_is_not_used(self):
        self.assertEqual(
            self.tools._extract_safe_image_urls(
                "Unrelated asset https://cdn.example.com/advertisement.jpg"
            ),
            [],
        )

    def test_html_image_url_reads_json_ld_and_resolves_relative_url(self):
        page_text = (
            '<script type="application/ld+json">'
            '{"url":"https://example.com/article",'
            '"image":{"url":"/media/hero.webp"}}'
            "</script>"
        )

        self.assertEqual(
            self.tools._extract_safe_image_urls(
                page_text,
                base_url="https://example.com/gallery",
            ),
            ["https://example.com/media/hero.webp"],
        )

    async def test_image_search_caps_first_five_results_at_four_images(self):
        self.ai_client.search_response = search_response(
            [
                search_result(
                    "T" * 300,
                    f"https://example.com/{index}",
                    image_url=f"https://images.example.com/{index}.jpg",
                )
                for index in range(6)
            ]
        )

        await self.tools.execute(
            "web_search",
            '{"query":"topic","search_type":"web","include_images":true}',
            self.context,
            self.research_context,
        )

        self.assertEqual(len(self.research_context.reply_images), 4)
        self.assertTrue(
            all(len(image.description) == 200 for image in self.research_context.reply_images)
        )
        self.assertNotIn(
            "https://images.example.com/4.jpg",
            [image.url for image in self.research_context.reply_images],
        )

    async def test_image_search_collects_safe_top_level_images_with_bounded_descriptions(self):
        self.ai_client.search_response = search_response(
            [search_result("Source", "https://example.com/source")],
            [
                WebSearchImage("https://images.example.com/1.jpg", None),
                WebSearchImage("https://images.example.com/1.jpg", None),
                WebSearchImage("https://images.example.com/2.jpg", "D" * 300),
                WebSearchImage("http://127.0.0.1/private.jpg", "Unsafe"),
                WebSearchImage("https://images.example.com/3.jpg", None),
                WebSearchImage("https://images.example.com/4.jpg", None),
                WebSearchImage("https://images.example.com/5.jpg", None),
            ],
        )

        payload = json.loads(
            await self.tools.execute(
                "web_search",
                '{"query":"  top-level topic  ","search_type":"web","include_images":true}',
                self.context,
                self.research_context,
            )
        )

        self.assertEqual(payload["image_count"], 4)
        self.assertEqual(
            [(image.url, image.description) for image in self.research_context.reply_images],
            [
                ("https://images.example.com/1.jpg", "top-level topic"),
                ("https://images.example.com/2.jpg", "D" * 200),
                ("https://images.example.com/3.jpg", "top-level topic"),
                ("https://images.example.com/4.jpg", "top-level topic"),
            ],
        )
        self.assertEqual(
            self.research_context.allowed_fetch_urls,
            {"https://example.com/source"},
        )

    async def test_image_search_dedupes_result_and_top_level_images(self):
        shared_url = "https://images.example.com/shared.jpg"
        self.ai_client.search_response = search_response(
            [
                search_result(
                    "Result description",
                    "https://example.com/source",
                    image_url=shared_url,
                )
            ],
            [
                WebSearchImage(shared_url, "Provider description"),
                WebSearchImage("https://images.example.com/top-level.jpg", None),
            ],
        )

        await self.tools.execute(
            "web_search",
            '{"query":"topic","search_type":"web","include_images":true}',
            self.context,
            self.research_context,
        )

        self.assertEqual(
            [(image.url, image.description) for image in self.research_context.reply_images],
            [
                (shared_url, "Result description"),
                ("https://images.example.com/top-level.jpg", "topic"),
            ],
        )

    async def test_third_web_search_does_not_call_client(self):
        arguments = '{"query":"topic","search_type":"web"}'

        first = json.loads(
            await self.tools.execute(
                "web_search", arguments, self.context, self.research_context
            )
        )
        second = json.loads(
            await self.tools.execute(
                "web_search", arguments, self.context, self.research_context
            )
        )
        third = json.loads(
            await self.tools.execute(
                "web_search", arguments, self.context, self.research_context
            )
        )

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(third, {"ok": False, "error": "search_limit_reached"})
        self.assertEqual(len(self.ai_client.search_calls), 2)

    async def test_web_fetch_exact_searched_url_truncates(self):
        url = "https://example.com/article"
        self.ai_client.search_response = search_response(
            [search_result("Search title", url, "Summary")]
        )
        self.ai_client.fetch_response = fetch_response("文" * 15001)
        await self.tools.execute(
            "web_search",
            '{"query":"topic","search_type":"web"}',
            self.context,
            self.research_context,
        )

        payload = json.loads(
            await self.tools.execute(
                "web_fetch",
                json.dumps({"url": url}),
                self.context,
                self.research_context,
            )
        )

        self.assertEqual(
            self.ai_client.fetch_calls,
            [{"url": url, "provider": "trusted-fetch"}],
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source_id"], 1)
        self.assertEqual(payload["title"], "Fetched title")
        self.assertEqual(payload["url"], url)
        self.assertEqual(payload["content"], "文" * 15000)
        self.assertTrue(payload["truncated"])

    async def test_web_fetch_unknown_url_does_not_call_client(self):
        payload = json.loads(
            await self.tools.execute(
                "web_fetch",
                '{"url":"https://example.com/not-searched"}',
                self.context,
                self.research_context,
            )
        )

        self.assertEqual(payload, {"ok": False, "error": "url_not_allowed"})
        self.assertEqual(self.ai_client.fetch_calls, [])
        self.assertEqual(self.research_context.fetch_calls, 0)

    async def test_web_fetch_rejects_unsafe_targets_even_if_context_is_tampered(self):
        unsafe_urls = (
            "http://localhost/page",
            "http://127.0.0.1/page",
            "http://0.0.0.0/page",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.1/page",
            "http://172.16.0.1/page",
            "http://192.168.1.1/page",
            "http://[::1]/page",
            "http://[fc00::1]/page",
            "file:///etc/passwd",
        )

        for url in unsafe_urls:
            with self.subTest(url=url):
                research_context = ResearchContext(
                    sources=[ResearchSource(1, "Unsafe", url, "", None)],
                    allowed_fetch_urls={url},
                )
                payload = json.loads(
                    await self.tools.execute(
                        "web_fetch",
                        json.dumps({"url": url}),
                        self.context,
                        research_context,
                    )
                )
                self.assertEqual(
                    payload, {"ok": False, "error": "url_not_allowed"}
                )

        self.assertEqual(self.ai_client.fetch_calls, [])

    async def test_third_web_fetch_does_not_call_client(self):
        url = "https://example.com/article"
        self.research_context.sources.append(
            ResearchSource(1, "Article", url, "Summary", None)
        )
        self.research_context.allowed_fetch_urls.add(url)
        arguments = json.dumps({"url": url})

        first = json.loads(
            await self.tools.execute(
                "web_fetch", arguments, self.context, self.research_context
            )
        )
        second = json.loads(
            await self.tools.execute(
                "web_fetch", arguments, self.context, self.research_context
            )
        )
        third = json.loads(
            await self.tools.execute(
                "web_fetch", arguments, self.context, self.research_context
            )
        )

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(third, {"ok": False, "error": "fetch_limit_reached"})
        self.assertEqual(len(self.ai_client.fetch_calls), 2)

    async def test_web_exceptions_do_not_leak_detail(self):
        secret_detail = "private-provider-detail"
        self.ai_client.search_error = RuntimeError(secret_detail)

        with self.assertLogs(level="ERROR") as search_logs:
            search_raw = await self.tools.execute(
                "web_search",
                '{"query":"topic","search_type":"web"}',
                self.context,
                self.research_context,
            )

        self.assertEqual(
            json.loads(search_raw),
            {"ok": False, "error": "web_search_unavailable"},
        )
        self.assertNotIn(secret_detail, search_raw)
        self.assertNotIn(secret_detail, "\n".join(search_logs.output))

        url = "https://example.com/article"
        fetch_context = ResearchContext(
            sources=[ResearchSource(1, "Article", url, "", None)],
            allowed_fetch_urls={url},
        )
        self.ai_client.fetch_error = RuntimeError(secret_detail)
        with self.assertLogs(level="ERROR") as fetch_logs:
            fetch_raw = await self.tools.execute(
                "web_fetch",
                json.dumps({"url": url}),
                self.context,
                fetch_context,
            )

        self.assertEqual(
            json.loads(fetch_raw),
            {"ok": False, "error": "web_fetch_unavailable"},
        )
        self.assertNotIn(secret_detail, fetch_raw)
        self.assertNotIn(secret_detail, "\n".join(fetch_logs.output))


if __name__ == "__main__":
    unittest.main()
