import math
import unittest

from src.ai_client import AIClient, AIClientError


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
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.last_url = None
        self.last_headers = None
        self.last_json = None

    def post(self, url, headers, json):
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        return self.response


class EmbeddingAIClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_embed_uses_openai_compatible_endpoint_and_reorders_by_index(self):
        session = FakeSession(
            FakeResponse(
                data={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                        {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    ]
                }
            )
        )
        client = AIClient("http://9router:20128/v1", "placeholder", "chat-model")
        client._session = session

        result = await client.embed(
            ["first", "second"],
            model="embedding-model",
            dimensions=3,
        )

        self.assertEqual(
            session.last_url,
            "http://9router:20128/v1/embeddings",
        )
        self.assertEqual(session.last_json["model"], "embedding-model")
        self.assertEqual(session.last_json["input"], ["first", "second"])
        self.assertEqual(session.last_json["dimensions"], 3)
        self.assertEqual(result, [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)])

    async def test_embed_rejects_invalid_response_shapes(self):
        invalid_payloads = [
            {"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
            {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    {"index": 0, "embedding": [0.0, 1.0, 0.0]},
                ]
            },
            {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    {"index": 1, "embedding": [True, 1.0, 0.0]},
                ]
            },
            {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    {"index": 1, "embedding": [math.inf, 1.0, 0.0]},
                ]
            },
            {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                client = AIClient(
                    "http://9router:20128/v1",
                    "placeholder",
                    "chat-model",
                )
                client._session = FakeSession(FakeResponse(data=payload))
                with self.assertRaisesRegex(AIClientError, "invalid response"):
                    await client.embed(
                        ["first", "second"],
                        model="embedding-model",
                        dimensions=3,
                    )

    async def test_embed_http_error_does_not_expose_key_or_body(self):
        client = AIClient(
            "http://9router:20128/v1",
            "placeholder-key",
            "chat-model",
        )
        client._session = FakeSession(
            FakeResponse(status=500, data={"detail": "private-body"})
        )

        with self.assertRaises(AIClientError) as caught:
            await client.embed(
                ["text"],
                model="embedding-model",
                dimensions=3,
            )

        message = str(caught.exception)
        self.assertNotIn("placeholder-key", message)
        self.assertNotIn("private-body", message)
        self.assertIn("HTTP 500", message)

    async def test_embed_rejects_invalid_inputs_before_network(self):
        client = AIClient("http://9router:20128/v1", "placeholder", "chat-model")
        for inputs, model, dimensions in (
            ([], "embedding-model", 3),
            ([""], "embedding-model", 3),
            (["ok"], "", 3),
            (["ok"], "embedding-model", 0),
        ):
            with self.subTest(inputs=inputs, model=model, dimensions=dimensions):
                with self.assertRaises(ValueError):
                    await client.embed(inputs, model=model, dimensions=dimensions)


if __name__ == "__main__":
    unittest.main()
