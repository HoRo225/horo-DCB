import asyncio
import unittest
from unittest.mock import PropertyMock, patch

import discord

from tests.support.admin import PanelCase, resolved, voice_channel

ENTRY = "選擇臨時語音入口頻道"


class VoicePageTests(PanelCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.open_panel()
        await self.go("臨時語音")

    async def test_toggle_leaves_the_entry_sync_to_voice(self) -> None:
        voice = self.admin.voice
        with patch.object(voice, "reconcile", wraps=voice.reconcile) as reconcile:
            clicked = await self.click(self.button("開啟臨時語音"))
            self.assertTrue(voice.enabled)
            shown = self.screen(clicked)
            self.assertIn("✅ 已開啟臨時語音。", shown)
            self.assertNotIn("入口同步失敗", shown)
            self.assertIn("### 目前狀態\n🟢 已開啟\n", shown)
            self.assertNotIn("此功能目前關閉", shown)
            self.view.toggled_at.clear()
            clicked = await self.click(self.button("關閉臨時語音"))
            self.assertFalse(voice.enabled)
            shown = self.screen(clicked)
            self.assertIn("✅ 已關閉臨時語音。", shown)
            self.assertIn("⚪ **此功能目前關閉**　按下方的開啟按鈕即可開啟；設定仍可修改。\n### 目前狀態\n⚪ 已關閉\n", shown)
            reconcile.assert_not_awaited()
        self.button("開啟臨時語音")

    async def test_failed_entry_sync_after_turning_on_is_shown(self) -> None:
        self.channels[500] = entry = voice_channel(500, self.guild)
        entry.permissions_for.return_value = discord.Permissions.all()
        await self.admin.voice.bind_entry(500)
        type(entry).members = PropertyMock(side_effect=RuntimeError("member cache unavailable"))
        with self.assertLogs(level="ERROR"):
            clicked = await self.click(self.button("開啟臨時語音"))
        self.assertTrue(self.admin.voice.enabled)
        self.assertIn("已開啟臨時語音。 入口同步失敗，請重新同步。", self.screen(clicked))

    async def test_double_click_toggle_keeps_state(self) -> None:
        button = self.button("開啟臨時語音")
        first, second = self.interaction(), self.interaction()
        self.assertTrue(await self.view.interaction_check(first))
        self.assertTrue(await self.view.interaction_check(second))
        await asyncio.gather(button.callback(first), button.callback(second))
        self.assertTrue(self.admin.voice.enabled)
        self.view.toggled_at["temp_voice"] -= 2
        await self.click(button)
        self.assertFalse(self.admin.voice.enabled)

    async def test_entry_select_binds_the_entry(self) -> None:
        self.channels[500] = entry = voice_channel(500, self.guild)
        clicked = await self.choose(self.select(ENTRY), resolved(entry))
        self.assertEqual(self.admin.voice.status().entry_channel_id, 500)
        self.assertIn("已綁定入口；開啟臨時語音後生效。", self.screen(clicked))

    async def test_admin_revoked_before_the_entry_commits_cannot_save_it(self) -> None:
        self.channels[500] = entry = voice_channel(500, self.guild)
        voice, bind_entry = self.admin.voice, self.admin.voice.bind_entry

        async def revoked_first(*args, **kwargs):
            self.revoke_admin()
            return await bind_entry(*args, **kwargs)

        with patch.object(voice, "bind_entry", side_effect=revoked_first):
            await self.choose(self.select(ENTRY), resolved(entry))
        self.assertIsNone(voice.status().entry_channel_id)

    async def test_sync_button_turns_blue_only_when_the_entry_needs_attention(self) -> None:
        primary, secondary = discord.ButtonStyle.primary, discord.ButtonStyle.secondary
        sync = self.button("重新同步")
        self.assertEqual((sync.style, sync.disabled), (secondary, True))
        await self.admin.voice.set_enabled(True)
        await self.click(self.button("重新整理"))
        self.assertEqual(sync.style, primary)
        self.channels[500] = entry = voice_channel(500, self.guild)
        entry.permissions_for.return_value = discord.Permissions.all()
        await self.admin.voice.bind_entry(500)
        await self.click(self.button("重新整理"))
        self.assertEqual((sync.style, sync.disabled), (secondary, False))


if __name__ == "__main__":
    unittest.main()
