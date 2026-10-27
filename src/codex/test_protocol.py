from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.codex import protocol
from src.codex.model_settings import find_model, parse_model_catalog
from src.codex.protocol import (
    MAX_CHAT_BUDGET_MS,
    MAX_PROMPT_CHARACTERS,
    PROTOCOL_VERSION,
    CodexArchiveResult,
    CodexBridgeError,
    CodexChatReply,
    completed_frame,
    error_frame,
    parse_archive_result,
    parse_runtime_status,
    parse_stream_frame,
    scope_matches,
    validate_archive_payload,
    validate_chat_payload,
)
from src.codex.thread_store import ThreadStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 8
THREAD_KEY = "guild:1:thread:5"
USER_KEY = "guild:1:channel:2:user:3"


def _data_url(content_type: str, data: bytes) -> str:
    return f"data:{content_type};base64," + base64.b64encode(data).decode()


def _chat(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "conversation_key": USER_KEY,
        "text": "你好",
        "images": [],
        "models": {"primary": {"model": "gpt-x", "effort": None}, "fallback": None},
    }
    payload.update(overrides)
    return payload


def _status(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "available": True,
        "authenticated": True,
        "plan": "plus",
        "sdk_version": "1",
        "runtime_version": "1",
        "thread_count": 0,
        "active_requests": 0,
        "queued_requests": 0,
        "ready": True,
        "reason": "ready",
    }
    body.update(overrides)
    return body


def _model(model: str = "gpt-x", **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "model": model,
        "display_name": model,
        "default_effort": "medium",
        "supported_efforts": ["low", "medium"],
        "input_modalities": ["text"],
    }
    entry.update(overrides)
    return entry


class ChatPayloadTests(unittest.TestCase):
    def assertInvalid(self, payload: object) -> None:
        with self.assertRaises(CodexBridgeError) as caught:
            validate_chat_payload(payload)
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_minimal_payload_passes(self) -> None:
        result = validate_chat_payload(_chat())
        self.assertEqual(result.conversation_key, USER_KEY)
        self.assertEqual(result.budget_ms, MAX_CHAT_BUDGET_MS)
        image = _data_url("image/png", PNG)
        self.assertEqual(validate_chat_payload(_chat(images=[image])).images, (image,))

    def test_rejects_bad_fields_and_limits(self) -> None:
        missing = _chat()
        del missing["models"]
        cases = {
            "missing": missing,
            "extra": _chat(extra=1),
            "text too long": _chat(text="a" * (MAX_PROMPT_CHARACTERS + 1)),
            "budget zero": _chat(budget_ms=0),
            "budget over": _chat(budget_ms=MAX_CHAT_BUDGET_MS + 1),
            "budget bool": _chat(budget_ms=True),
            "parent on user key": _chat(parent_channel_id=2),
            "parent is self": _chat(conversation_key=THREAD_KEY, parent_channel_id=5),
            "blank text no image": _chat(text="   "),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                self.assertInvalid(payload)
        self.assertEqual(
            validate_chat_payload(_chat(conversation_key=THREAD_KEY, parent_channel_id=2))
            .parent_channel_id,
            2,
        )

    def test_rejects_bad_images(self) -> None:
        cases = {
            "invalid base64": "data:image/png;base64,@@@@",
            "signature mismatch": _data_url("image/png", JPEG),
            "gif": _data_url("image/gif", b"GIF89a" + b"\0" * 8),
        }
        for name, image in cases.items():
            with self.subTest(name):
                self.assertInvalid(_chat(images=[image]))

    def test_rejects_oversized_image(self) -> None:
        with (
            patch.object(protocol, "MAX_IMAGE_BYTES", 16),
            patch.object(protocol, "_MAX_ENCODED_IMAGE_CHARS", 24),
        ):
            validate_chat_payload(_chat(images=[_data_url("image/png", PNG)]))
            self.assertInvalid(_chat(images=[_data_url("image/png", PNG + b"\0")]))


class ArchivePayloadTests(unittest.TestCase):
    def test_validation(self) -> None:
        for payload in (
            {"guild_id": 1, "include_children": True},
            {"guild_id": True},
            {"guild_id": 1, "extra": 1},
        ):
            with self.subTest(payload), self.assertRaises(CodexBridgeError) as caught:
                validate_archive_payload(payload)
            self.assertEqual(caught.exception.code, "invalid_request")
        result = validate_archive_payload({"guild_id": 1, "channel_id": 2, "include_children": True})
        self.assertEqual((result.guild_id, result.channel_id, result.include_children), (1, 2, True))


class ArchiveResultTests(unittest.TestCase):
    def test_count_rules_are_shared_by_all_archive_responses(self) -> None:
        for detached, archived, unconfirmed in ((0, 0, 0), (2, 1, 1), (2, 1, 0)):
            self.assertEqual(parse_archive_result({"detached_count": detached, "archived_count": archived,
                                                   "archive_unconfirmed_count": unconfirmed}),
                             CodexArchiveResult(detached, archived, unconfirmed))
        for detached, archived, unconfirmed in ((0, 1, 0), (1, 1, 1), (1, 0, 2), (True, 0, 0)):
            with self.subTest(counts=(detached, archived, unconfirmed)), self.assertRaises(ValueError):
                parse_archive_result({"detached_count": detached, "archived_count": archived,
                                      "archive_unconfirmed_count": unconfirmed})


class RuntimeStatusTests(unittest.TestCase):
    def test_valid_status(self) -> None:
        status = parse_runtime_status(_status(last_error="not_a_code"))
        self.assertTrue(status.ready)
        self.assertIsNone(status.last_error)
        self.assertEqual(parse_runtime_status(_status(last_error="busy")).last_error, "busy")

    def test_rejects_inconsistent_status(self) -> None:
        cases = {
            "version": _status(protocol_version=PROTOCOL_VERSION + 1),
            "ready without reason": _status(reason="initializing"),
            "reason without ready": _status(ready=False),
            "ready but unauthenticated": _status(authenticated=False),
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_runtime_status(body)


class WireErrorTests(unittest.TestCase):
    def test_unknown_or_bot_only_code_becomes_unavailable(self) -> None:
        for code, expected in {
            "busy": "busy",
            "nope": "unavailable",
            "revoked": "unavailable",
            "not_allowed": "unavailable",
            "model_configuration_invalid": "unavailable",
        }.items():
            with self.subTest(code), self.assertRaises(CodexBridgeError) as caught:
                parse_stream_frame(error_frame(code))
            self.assertEqual(caught.exception.code, expected)


class CompletedFrameTests(unittest.TestCase):
    def test_restarted_round_trips_and_defaults_for_old_bridge(self) -> None:
        reply = CodexChatReply("好", restarted=True)
        self.assertEqual(parse_stream_frame(completed_frame(reply)), reply)
        self.assertEqual(
            parse_stream_frame({"type": "completed", "reply": "好"}), CodexChatReply("好")
        )
        with self.assertRaises(CodexBridgeError):
            parse_stream_frame({"type": "completed", "reply": "好", "restarted": 1})


class ScopeMatchesTests(unittest.TestCase):
    def test_scopes(self) -> None:
        self.assertTrue(scope_matches(USER_KEY, 1))
        self.assertFalse(scope_matches(USER_KEY, 9))
        self.assertTrue(scope_matches(USER_KEY, 1, 2))
        self.assertFalse(scope_matches(USER_KEY, 1, 4))
        self.assertTrue(scope_matches(THREAD_KEY, 1, 5))
        self.assertFalse(scope_matches(THREAD_KEY, 1, 2))
        self.assertTrue(scope_matches(THREAD_KEY, 1, 2, include_children=True))
        self.assertTrue(
            scope_matches(THREAD_KEY, 1, 2, parent_channel_id=2, include_children=True)
        )
        self.assertFalse(
            scope_matches(THREAD_KEY, 1, 2, parent_channel_id=7, include_children=True)
        )


class ThreadStoreTests(unittest.TestCase):
    def test_round_trip_and_parent_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            ThreadStore(path).set(THREAD_KEY, "t1", parent_channel_id=2)
            store = ThreadStore(path)
            self.assertEqual((store.get(THREAD_KEY), store.get_parent(THREAD_KEY)), ("t1", 2))
            with self.assertRaises(ValueError):
                store.set(THREAD_KEY, "t2", parent_channel_id=3)

    def test_pop_many_dedupes_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            store = ThreadStore(path)
            self.assertTrue(store.available)
            store.set(USER_KEY, "t1")
            self.assertEqual(store.pop_many([USER_KEY, USER_KEY]), ["t1"])
            self.assertEqual(len(ThreadStore(path)), 0)

    def test_old_updated_at_is_read_and_no_longer_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            old = {"thread_id": "t1", "updated_at": 1, "parent_channel_id": None}
            path.write_text(
                json.dumps({"version": 2, "threads": {USER_KEY: old}}), encoding="utf-8"
            )
            store = ThreadStore(path)
            self.assertEqual(store.get(USER_KEY), "t1")
            store.set(THREAD_KEY, "t2", parent_channel_id=2)
            saved = json.loads(path.read_text(encoding="utf-8"))["threads"]
            self.assertEqual(saved[USER_KEY], {"thread_id": "t1", "parent_channel_id": None})
            self.assertNotIn("updated_at", saved[THREAD_KEY])
            path.write_text(
                json.dumps({"version": 2, "threads": {USER_KEY: old | {"extra": 1}}}),
                encoding="utf-8",
            )
            with self.assertLogs(level="ERROR"):
                self.assertFalse(ThreadStore(path).available)

    def test_corrupt_file_disables_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            path.write_text("{", encoding="utf-8")
            with self.assertLogs(level="ERROR"):
                store = ThreadStore(path)
            self.assertFalse(store.available)
            with self.assertRaises(OSError):
                store.set(USER_KEY, "t1")
            self.assertFalse(store.recover())
            self.assertEqual(path.read_text(encoding="utf-8"), "{")

    def test_failed_write_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            store = ThreadStore(path)
            store.set(USER_KEY, "t1")
            with patch("src.codex.thread_store.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.set(THREAD_KEY, "t2", parent_channel_id=2)
                self.assertFalse(store.recover())
            self.assertFalse(store.available)
            self.assertIsNone(store.get(THREAD_KEY))
            store.set(THREAD_KEY, "t2", parent_channel_id=2)
            self.assertTrue(store.available)
            reopened = ThreadStore(path)
            self.assertEqual((reopened.get(USER_KEY), reopened.get(THREAD_KEY)), ("t1", "t2"))


class ModelCatalogTests(unittest.TestCase):
    def test_find_model(self) -> None:
        catalog = parse_model_catalog({"models": [_model("a"), _model("b")], "fetched_at": 1})
        self.assertEqual(find_model(catalog, "b").model, "b")
        self.assertIsNone(find_model(catalog, "c"))
        self.assertIsNone(find_model(catalog, None))

    def test_rejects_inconsistent_catalog(self) -> None:
        for name, models in {
            "duplicate": [_model("a"), _model("a")],
            "default not supported": [_model(default_effort="high")],
        }.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_model_catalog({"models": models, "fetched_at": 1})


if __name__ == "__main__":
    unittest.main()
