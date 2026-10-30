import asyncio
import socket
import unittest

from src.ai.adapters.claude.client import ClaudeClient, ClaudeConnectionError, ClaudeHttpError
from tests.support.claude_server import PASSWORD, FakeClewdr, Reply, answer, failure, stream_failure

BODY = {"model": "claude-haiku-5-5", "max_tokens": 16, "messages": [{"role": "user", "content": "嗨"}], "stream": True}


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeClewdr()
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.client = self.connect()

    def connect(self, password: str = PASSWORD, url: str | None = None) -> ClaudeClient:
        client = ClaudeClient(url or self.server.url, password)
        self.addAsyncCleanup(client.close)
        return client

    async def stream(self, client: ClaudeClient | None = None) -> list[dict]:
        events: list[dict] = []
        await (client or self.client).stream_message(BODY, on_event=events.append)
        return events

    async def test_version_is_the_first_line(self) -> None:
        self.assertEqual(await self.client.version(), "v0.13.6")

    async def test_events_arrive_whole_even_when_split_across_reads(self) -> None:
        self.server.replies = [answer("你好", "，世界", chunk=7)]
        events = await self.stream()
        self.assertEqual(self.server.requests, [BODY])
        texts = [event["delta"]["text"] for event in events if event.get("delta", {}).get("type") == "text_delta"]
        self.assertEqual(texts, ["你好", "，世界"])
        self.assertEqual(events[-1], {"type": "message_stop"})

    async def test_search_results_longer_than_a_read_buffer_arrive_whole(self) -> None:
        self.server.replies = [answer("查到了", search=True, chunk=1024)]
        events = await self.stream()
        (result,) = [event for event in events if event.get("content_block", {}).get("type") == "web_search_tool_result"]
        self.assertEqual(len(result["content_block"]["content"][0]["encrypted_content"]), 70_000)

    async def test_error_status_carries_the_upstream_error_type(self) -> None:
        self.server.replies = [failure(529, "overloaded_error", "滿了")]
        with self.assertRaises(ClaudeHttpError) as caught:
            await self.stream()
        self.assertEqual((caught.exception.status, caught.exception.error_type, caught.exception.message),
                         (529, "overloaded_error", "滿了"))

    async def test_non_json_error_body_is_kept_as_text(self) -> None:
        self.server.replies = [Reply(status=502, body="bad gateway")]
        with self.assertRaises(ClaudeHttpError) as caught:
            await self.stream()
        self.assertEqual((caught.exception.status, caught.exception.error_type), (502, "http_error"))

    async def test_wrong_password_is_an_authentication_error(self) -> None:
        with self.assertRaises(ClaudeHttpError) as caught:
            await self.stream(self.connect("wrong"))
        self.assertEqual((caught.exception.status, caught.exception.error_type), (401, "authentication_error"))
        self.assertEqual(self.server.requests, [])

    async def test_error_event_mid_stream_maps_to_its_status(self) -> None:
        self.server.replies = [stream_failure("overloaded_error", "半句")]
        with self.assertRaises(ClaudeHttpError) as caught:
            await self.stream()
        self.assertEqual((caught.exception.status, caught.exception.error_type), (529, "overloaded_error"))

    async def test_stream_that_ends_without_message_stop_is_a_connection_error(self) -> None:
        events = answer("半句").events[:-2]
        self.server.replies = [Reply(events=events)]
        with self.assertRaises(ClaudeConnectionError):
            await self.stream()

    async def test_unreachable_server_is_a_connection_error(self) -> None:
        client = self.connect(url=f"http://127.0.0.1:{closed_port()}")
        with self.assertRaises(ClaudeConnectionError):
            await client.version()
        with self.assertRaises(ClaudeConnectionError):
            await self.stream(client)

    async def test_cancelling_mid_stream_closes_the_connection(self) -> None:
        self.server.replies = [answer("想很久", hang=True)]
        seen = asyncio.Event()
        task = asyncio.create_task(self.client.stream_message(BODY, on_event=lambda event: seen.set()))
        await asyncio.wait_for(seen.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.server.disconnected.wait(), 5)


if __name__ == "__main__":
    unittest.main()
