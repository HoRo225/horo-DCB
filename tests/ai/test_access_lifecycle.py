import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import discord

from src.ai.access import CodexAccess
from src.ai.client import CodexBridgeClient
from src.preflight import main as preflight
from tests.support.access import configured_access

from tests.support.ai import settle, stop_tasks, make_admin, interaction, role


class AccessRecoveryTest(unittest.TestCase):
    def test_version_three_empty_roles_remain_denied_after_channel_update(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            path.write_text(json.dumps({
                "version": 3, "guild_id": 10, "channel_ids": [20], "role_ids": [],
            }), encoding="utf-8")
            access = CodexAccess(True, 10, state_path=path)
            self.assertFalse(access.allows(10, 20))
            access.set_channels(10, frozenset({20, 21}))
            restarted = CodexAccess(True, 10, state_path=path)
            self.assertFalse(restarted.allows(10, 21))

    def test_corrupt_recovery_requires_channels_then_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            path.write_text("{", encoding="utf-8")
            access = CodexAccess(True, 10, state_path=path)
            with self.assertRaises(ValueError):
                access.set_roles(10, frozenset({70}))
            access.set_channels(10, frozenset({21}))
            self.assertTrue(access.state_available)
            self.assertFalse(access.allows(10, 21))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {
                "version": 3, "guild_id": 10, "channel_ids": [21], "role_ids": [],
            })
            restarted = CodexAccess(True, 10, state_path=path)
            self.assertFalse(restarted.allows(10, 21))
            restarted.set_roles(10, frozenset({70}))
            self.assertTrue(restarted.allows(10, 21, frozenset({70})))
            self.assertFalse(restarted.allows(10, 21))

    def test_unreadable_state_is_not_treated_as_absent_configuration(self):
        with patch.object(Path, "exists", return_value=False), patch.object(
            Path, "read_text", side_effect=PermissionError("state directory denied"),
        ):
            access = CodexAccess(True, 10, state_path="/denied/access.json")
        self.assertFalse(access.allows(10, 20))
        self.assertFalse(access.state_available)

    def test_failed_role_persistence_preserves_previous_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            access = CodexAccess(True, 10, state_path=path)
            access.set_channels(10, frozenset({20}))
            access.set_roles(10, frozenset({80}))
            generation = access.generation
            durable = path.read_bytes()
            with patch("src.state.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    access.set_roles(10, frozenset({70}))
            self.assertEqual(path.read_bytes(), durable)
            self.assertTrue(access.allows(10, 20, frozenset({80})))
            self.assertEqual(access.generation, generation)
            self.assertFalse(access.allows(10, 20, frozenset({70})))

    def test_preflight_empty_roles_reports_denied_roles_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            path.write_text(json.dumps({
                "version": 3, "guild_id": 10, "channel_ids": [20], "role_ids": [],
            }), encoding="utf-8")
            config = SimpleNamespace(
                codex_enabled=True, codex_allowed_guild_id=10,
                temp_voice_enabled=False, steam_free_games_enabled=False,
                ai_text_display_enabled=True,
            )
            output = io.StringIO()
            with patch("src.preflight.AppConfig.from_env", return_value=config), patch("sys.stdout", output):
                preflight(path)
            status = json.loads(output.getvalue())
            self.assertEqual(status["codex_access_mode"], "roles")
            self.assertFalse(status["codex_allowlist_configured"])


class AdminMutationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.access = configured_access(True, 10, channel_ids=(20,))
        self.access.set_roles(10, frozenset({70}))
        self.client = CodexBridgeClient("http://codex:8765", "a" * 64)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.archives = 0
        self.peak_archives = 0
        self.tasks = []
        async def request(_method, path, **_kwargs):
            if path == "/v1/archive":
                self.archives += 1
                self.peak_archives = max(self.peak_archives, self.archives)
                self.entered.set()
                try:
                    await self.release.wait()
                finally:
                    self.archives -= 1
            return {}
        self.client._request = request

    async def asyncTearDown(self):
        self.release.set()
        await stop_tasks(self.tasks)
        await asyncio.wait_for(self.client.close(), 1)

    async def test_separate_panels_serialize_shared_role_mutations(self):
        first_view = make_admin(self.access, self.client)
        second_view = make_admin(self.access, self.client)
        first = asyncio.create_task(first_view.handle_codex_role_select(interaction(), (role(80),)))
        self.tasks.append(first)
        await asyncio.wait_for(self.entered.wait(), 0.3)
        second = asyncio.create_task(second_view.handle_codex_role_select(interaction(), (role(90),)))
        self.tasks.append(second)
        await settle()
        self.assertEqual(self.peak_archives, 1, "separate panels mutated shared access concurrently")
        self.assertFalse(self.access.allows(10, 20, frozenset({70})))
        self.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(self.access.role_ids, frozenset({90}))

    async def test_channel_change_waits_for_shared_role_mutation(self):
        first_view = make_admin(self.access, self.client)
        second_view = make_admin(self.access, self.client)
        first = asyncio.create_task(first_view.handle_codex_role_select(interaction(), (role(80),)))
        self.tasks.append(first)
        await asyncio.wait_for(self.entered.wait(), 0.3)
        channel = SimpleNamespace(id=21, guild=SimpleNamespace(id=10), type=discord.ChannelType.text)
        second = asyncio.create_task(second_view.handle_codex_channel_select(interaction(), (channel,)))
        self.tasks.append(second)
        await settle()
        self.assertEqual(self.access.channel_ids, frozenset({20}))
        self.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(self.access.channel_ids, frozenset({21}))
        self.assertEqual(self.access.role_ids, frozenset({80}))
        self.assertFalse(self.access.allows(10, 20, frozenset({80})))

    async def test_channels_only_repair_keeps_role_selector_usable_and_access_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            path.write_text("{", encoding="utf-8")
            access = CodexAccess(True, 10, state_path=path)
            access.set_channels(10, frozenset({20}))
            view = make_admin(access, self.client)
            overview = repr(view.to_components())
            self.assertIn("0 / 1 已設定", overview)
            self.assertIn("白名單身分組", overview)
            view._render_ai()
            components = view.to_components()
            rendered = repr(components)
            self.assertNotIn("暫用舊使用者白名單", rendered)
            self.assertIn("未允許", rendered)
            selectors = []
            def collect(values):
                for component in values:
                    if component.get("type") == discord.ComponentType.role_select.value:
                        selectors.append(component)
                    collect(component.get("components", []))
            collect(components)
            self.assertEqual(len(selectors), 1)
            self.assertFalse(selectors[0].get("disabled", False))


if __name__ == "__main__":
    unittest.main()
