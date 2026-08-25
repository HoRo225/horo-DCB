import unittest
from unittest.mock import patch

from src.ai_client import AIClient, AIClientError, AIToolCall


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self.data


class FakeSession:
    def __init__(self, response, *, get_responses=None):
        self.responses = response if isinstance(response, list) else [response]
        self.get_responses = get_responses or {}
        self.closed = False
        self.last_url = None
        self.last_headers = None
        self.last_json = None
        self.requests = []
        self.get_requests = []

    def post(self, url, headers, json):
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        self.requests.append((url, headers, json))
        return self.responses[len(self.requests) - 1]

    def get(self, url, *, headers, params=None, timeout=None):
        self.get_requests.append((url, headers, params, timeout))
        return self.get_responses[url]

    async def close(self):
        self.closed = True


class AIClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_reuses_shared_session(self):
        session = FakeSession(FakeResponse())
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")

        with patch("src.ai_client.aiohttp.ClientSession", return_value=session) as make:
            await client.start()
            await client.start()

        make.assert_called_once()
        self.assertIs(client._session, session)

    async def test_router_root_is_derived_from_v1_base_url(self):
        client = AIClient("http://9router:20128/v1/", "secret-key", "model-a")

        self.assertEqual(client.router_root_url, "http://9router:20128")

    async def test_runtime_status_reads_safe_9router_metadata(self):
        root = "http://9router:20128"
        session = FakeSession(
            FakeResponse(),
            get_responses={
                f"{root}/v1/models": FakeResponse(
                    data={
                        "object": "list",
                        "data": [
                            {
                                "id": "horo-main",
                                "object": "model",
                                "owned_by": "combo",
                            }
                        ],
                    }
                ),
                f"{root}/v1/models/info": FakeResponse(
                    data={
                        "resolved": {
                            "name": "GPT 5.6 Luna",
                            "effort": "auto",
                        }
                    }
                ),
                f"{root}/api/health": FakeResponse(data={"ok": True}),
                f"{root}/api/version": FakeResponse(
                    data={"currentVersion": "0.5.55"}
                ),
            },
        )
        client = AIClient(f"{root}/v1", "secret-key", "horo-main")
        client._session = session

        status = await client.get_runtime_status()

        self.assertEqual(status.model_name, "GPT 5.6 Luna")
        self.assertEqual(status.effort, "auto")
        self.assertTrue(status.router_available)
        self.assertEqual(status.router_version, "0.5.55")
        requests = {url: (headers, params) for url, headers, params, _ in session.get_requests}
        list_headers, list_params = requests[f"{root}/v1/models"]
        self.assertEqual(list_headers["Authorization"], "Bearer secret-key")
        self.assertIsNone(list_params)
        model_headers, model_params = requests[f"{root}/v1/models/info"]
        self.assertEqual(model_headers["Authorization"], "Bearer secret-key")
        self.assertEqual(model_params, {"id": "horo-main"})
        self.assertNotIn("Authorization", requests[f"{root}/api/health"][0])
        self.assertNotIn("Authorization", requests[f"{root}/api/version"][0])

    async def test_runtime_status_degrades_independent_failures(self):
        root = "http://9router:20128"
        session = FakeSession(
            FakeResponse(),
            get_responses={
                f"{root}/v1/models": FakeResponse(status=500),
                f"{root}/v1/models/info": FakeResponse(status=404),
                f"{root}/api/health": FakeResponse(data={"ok": True}),
                f"{root}/api/version": FakeResponse(status=500),
            },
        )
        client = AIClient(f"{root}/v1", "secret-key", "horo-main")
        client._session = session

        status = await client.get_runtime_status()

        self.assertIsNone(status.model_name)
        self.assertIsNone(status.effort)
        self.assertTrue(status.router_available)
        self.assertIsNone(status.router_version)

    async def test_runtime_status_uses_configured_combo_from_public_model_list(self):
        root = "http://9router:20128"
        session = FakeSession(
            FakeResponse(),
            get_responses={
                f"{root}/v1/models": FakeResponse(
                    data={
                        "object": "list",
                        "data": [
                            {
                                "id": "horo-main",
                                "object": "model",
                                "owned_by": "combo",
                            }
                        ],
                    }
                ),
                f"{root}/v1/models/info": FakeResponse(status=404),
                f"{root}/api/health": FakeResponse(data={"ok": True}),
                f"{root}/api/version": FakeResponse(
                    data={"currentVersion": "0.5.55"}
                ),
            },
        )
        client = AIClient(f"{root}/v1", "secret-key", "horo-main")
        client._session = session

        status = await client.get_runtime_status()

        self.assertEqual(status.model_name, "horo-main")
        self.assertIsNone(status.effort)
        self.assertTrue(status.router_available)
        self.assertEqual(status.router_version, "0.5.55")

    async def test_invalid_base_url_is_rejected(self):
        invalid_urls = (
            "https://9router:20128/v1",
            "http://9router:20128/v2",
            "http://9router:20128/v1/chat",
            "http://user:password@9router:20128/v1",
            "http://9router:20128/v1?query=value",
        )

        for base_url in invalid_urls:
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ValueError, "HTTP URL ending in /v1"):
                    AIClient(base_url, "secret-key", "model-a")

    async def test_chat_uses_openai_compatible_endpoint(self):
        session = FakeSession(
            FakeResponse(
                data={"choices": [{"message": {"content": " hello "}}]}
            )
        )
        client = AIClient("http://9router:20128/v1/", "secret-key", "model-a")
        client._session = session

        result = await client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(result.content, "hello")
        self.assertEqual(result.tool_calls, ())
        self.assertEqual(
            session.last_url, "http://9router:20128/v1/chat/completions"
        )
        self.assertEqual(session.last_headers["Authorization"], "Bearer secret-key")
        self.assertEqual(session.last_json["model"], "model-a")
        self.assertFalse(session.last_json["stream"])
        self.assertNotIn("tools", session.last_json)

    async def test_chat_includes_tools_when_provided(self):
        session = FakeSession(
            FakeResponse(data={"choices": [{"message": {"content": "done"}}]})
        )
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object"},
                },
            }
        ]

        await client.chat([{"role": "user", "content": "weather?"}], tools=tools)

        self.assertEqual(session.last_json["tools"], tools)

    async def test_chat_parses_tool_only_response(self):
        session = FakeSession(
            FakeResponse(
                data={
                    "choices": [
                        {
                            "message": {
                                "content": "   ",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": '{"city":"Paris"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        )
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        result = await client.chat([{"role": "user", "content": "weather?"}])

        self.assertIsNone(result.content)
        self.assertEqual(
            result.tool_calls,
            (
                AIToolCall(
                    id="call-1",
                    name="get_weather",
                    arguments='{"city":"Paris"}',
                ),
            ),
        )

    async def test_chat_parses_mixed_content_and_tool_calls(self):
        session = FakeSession(
            FakeResponse(
                data={
                    "choices": [
                        {
                            "message": {
                                "content": " Checking now. ",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        )
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        result = await client.chat([{"role": "user", "content": "check"}])

        self.assertEqual(result.content, "Checking now.")
        self.assertEqual(
            result.tool_calls,
            (AIToolCall(id="call-1", name="lookup", arguments="{}"),),
        )

    async def test_malformed_tool_call_is_rejected(self):
        session = FakeSession(
            FakeResponse(
                data={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": {},
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        )
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        with self.assertRaisesRegex(
            AIClientError, "9Router returned an invalid response"
        ):
            await client.chat([{"role": "user", "content": "check"}])

    async def test_empty_response_is_rejected(self):
        session = FakeSession(
            FakeResponse(data={"choices": [{"message": {"content": "  "}}]})
        )
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        with self.assertRaisesRegex(
            AIClientError, "9Router returned an empty response"
        ):
            await client.chat([{"role": "user", "content": "hi"}])

    async def test_http_error_does_not_expose_api_key_or_body(self):
        session = FakeSession(FakeResponse(status=500, data={"secret": "body-secret"}))
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        with self.assertRaises(AIClientError) as context:
            await client.chat([{"role": "user", "content": "hi"}])

        message = str(context.exception)
        self.assertIn("HTTP 500", message)
        self.assertNotIn("secret-key", message)
        self.assertNotIn("body-secret", message)

    async def test_invalid_response_is_rejected(self):
        session = FakeSession(FakeResponse(data={"choices": []}))
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        with self.assertRaises(AIClientError):
            await client.chat([{"role": "user", "content": "hi"}])

    async def test_web_search_uses_verified_endpoint_and_fixed_payload(self):
        response_data = {
            "provider": "trusted-search",
            "query": "recent news",
            "results": [
                {
                    "title": "Result",
                    "url": "https://example.com/result",
                    "snippet": "Summary",
                    "published_at": "2026-08-19",
                }
            ],
            "answer": None,
        }
        session = FakeSession(FakeResponse(data=response_data))
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        result = await client.web_search(
            "recent news", provider="trusted-search", search_type="news"
        )

        self.assertIs(result, response_data)
        self.assertEqual(session.last_url, "http://9router:20128/v1/search")
        self.assertEqual(
            session.last_json,
            {
                "provider": "trusted-search",
                "query": "recent news",
                "max_results": 5,
                "search_type": "news",
            },
        )

    async def test_web_search_true_adds_only_trusted_image_option(self):
        session = FakeSession(FakeResponse(data={"results": []}))
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        await client.web_search("images", provider="trusted-search", include_images=True)

        self.assertEqual(
            session.last_json,
            {
                "provider": "trusted-search",
                "query": "images",
                "max_results": 5,
                "search_type": "web",
                "content_options": {"images": True},
            },
        )

    async def test_web_fetch_uses_verified_endpoint_and_fixed_payload(self):
        response_data = {
            "provider": "trusted-fetch",
            "url": "https://example.com/article",
            "title": "Article",
            "content": {"format": "markdown", "text": "Body", "length": 4},
        }
        session = FakeSession(FakeResponse(data=response_data))
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        result = await client.web_fetch(
            "https://example.com/article", provider="trusted-fetch"
        )

        self.assertIs(result, response_data)
        self.assertEqual(session.last_url, "http://9router:20128/v1/web/fetch")
        self.assertEqual(
            session.last_json,
            {
                "provider": "trusted-fetch",
                "url": "https://example.com/article",
                "format": "markdown",
                "max_characters": 15000,
            },
        )

    async def test_web_fetch_supports_bounded_html_for_image_fallback(self):
        response_data = {
            "content": {"format": "html", "text": "page", "length": 4},
        }
        session = FakeSession(FakeResponse(data=response_data))
        client = AIClient("http://9router:20128/v1", "configured", "model-a")
        client._session = session

        result = await client.web_fetch(
            "https://example.com/gallery",
            provider="trusted-fetch",
            content_format="html",
            max_characters=50000,
        )

        self.assertIs(result, response_data)
        self.assertEqual(
            session.last_json,
            {
                "provider": "trusted-fetch",
                "url": "https://example.com/gallery",
                "format": "html",
                "max_characters": 50000,
            },
        )

    async def test_web_fetch_rejects_invalid_internal_options_before_network(self):
        session = FakeSession(FakeResponse(data={"content": {"text": "unused"}}))
        client = AIClient("http://9router:20128/v1", "configured", "model-a")
        client._session = session

        invalid_options = (
            {"content_format": "xml", "max_characters": 50000},
            {"content_format": "html", "max_characters": 0},
            {"content_format": "html", "max_characters": True},
        )
        for options in invalid_options:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    await client.web_fetch(
                        "https://example.com/gallery",
                        provider="trusted-fetch",
                        **options,
                    )

        self.assertEqual(session.requests, [])

    async def test_web_methods_reuse_the_same_session(self):
        session = FakeSession(
            [
                FakeResponse(data={"results": []}),
                FakeResponse(data={"content": {"text": "Body"}}),
            ]
        )
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        await client.web_search("query", provider="trusted-search")
        await client.web_fetch("https://example.com", provider="trusted-fetch")

        self.assertIs(client._session, session)
        self.assertEqual(len(session.requests), 2)

    async def test_malformed_web_responses_are_rejected(self):
        cases = (
            ("search", {"results": "not-a-list"}),
            ("search", {"results": ["not-an-object"]}),
            ("fetch", {"content": "not-an-object"}),
            ("fetch", {"content": {"text": 123}}),
        )

        for method, response_data in cases:
            with self.subTest(method=method, response_data=response_data):
                session = FakeSession(FakeResponse(data=response_data))
                client = AIClient(
                    "http://9router:20128/v1", "secret-key", "model-a"
                )
                client._session = session

                with self.assertRaisesRegex(
                    AIClientError, "9Router returned an invalid response"
                ):
                    if method == "search":
                        await client.web_search("query", provider="trusted-search")
                    else:
                        await client.web_fetch(
                            "https://example.com", provider="trusted-fetch"
                        )

    async def test_web_http_errors_do_not_expose_api_key_or_body(self):
        session = FakeSession(
            [
                FakeResponse(status=429, data={"detail": "provider-body-secret"}),
                FakeResponse(status=503, data={"detail": "provider-body-secret"}),
            ]
        )
        client = AIClient("http://9router:20128/v1", "secret-key", "model-a")
        client._session = session

        calls = (
            client.web_search("query", provider="trusted-search"),
            client.web_fetch("https://example.com", provider="trusted-fetch"),
        )
        for call, status in zip(calls, (429, 503)):
            with self.subTest(status=status):
                with self.assertRaises(AIClientError) as context:
                    await call

                message = str(context.exception)
                self.assertIn(f"HTTP {status}", message)
                self.assertNotIn("secret-key", message)
                self.assertNotIn("provider-body-secret", message)


if __name__ == "__main__":
    unittest.main()
