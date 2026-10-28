import unittest
from unittest.mock import patch

import discord

from src.ai import RuntimeStatus
from src.core.adapters.discord.brand import ICONS
from tests.support.admin import G, PanelCase, resolved, text_channel
from tests.support.testing import text


class AiStatusPageTests(PanelCase):
    async def test_rate_limits_only_on_status_page_not_tech(self) -> None:
        await self.open_panel()
        shown = self.screen(await self.go("樂奈"))
        self.assertIn(
            "### 帳號額度\n**5 小時**　▰▰▰▰▰▰▰▰▱▱ 剩餘 80%\n**重設時間**　未提供\n**額度快照**　<t:", shown
        )
        self.assertNotIn("已用", shown)
        shown = self.screen(await self.go("技術"))
        self.assertNotIn("額度", shown)
        self.assertIn("### 安全邊界\n唯讀；指令列、MCP、應用程式、外掛、子代理與全域記憶都停用", shown)

    async def test_toggle_ai_cancels_requests_only_when_turning_off(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        self.guild.me.display_name = "Horo"
        reads = self.codex.get_runtime_status.await_count
        await self.click(self.button("關閉樂奈"))
        clicked = await self.click(self.button("確認關閉"))
        self.assertFalse(self.admin.ai.enabled)
        self.assertIn("✅ 已關閉 Horo。", self.screen(clicked))
        self.assertEqual(self.codex.get_runtime_status.await_count, reads + 1)
        self.codex.cancel.assert_awaited_once_with(G)
        self.view.toggled_at.clear()
        clicked = await self.click(self.button("開啟 Horo"))
        self.assertTrue(self.admin.ai.enabled)
        self.assertIn("✅ 已開啟 Horo。", self.screen(clicked))
        self.codex.cancel.assert_awaited_once()

    async def test_turning_ai_off_asks_first_and_cancel_keeps_it_on(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        shown = self.screen(await self.click(self.button("關閉樂奈")))
        self.assertIn(f"### {ICONS['warn']} 確認關閉樂奈\n關閉後會立即中斷所有進行中的樂奈請求", shown)
        self.assertEqual(self.button("確認關閉").style, discord.ButtonStyle.danger)
        self.assertEqual(self.button("取消").style, discord.ButtonStyle.secondary)
        self.assertEqual(str(self.button("取消").emoji), ICONS["cancel"])
        self.assertTrue(self.admin.ai.enabled)
        self.codex.cancel.assert_not_awaited()
        shown = self.screen(await self.click(self.button("取消")))
        self.assertIn("### 目前狀態", shown)
        self.assertNotIn("確認關閉", shown)
        self.assertTrue(self.admin.ai.enabled)
        self.codex.cancel.assert_not_awaited()

    async def test_last_codex_error_is_explained_with_its_code(self) -> None:
        self.codex.get_runtime_status.return_value = RuntimeStatus(
            reason="ready", authenticated=True, plan="plus", last_error="timeout",
        )
        await self.open_panel()
        shown = self.screen(await self.go("樂奈"))
        self.assertIn("### ⚠️ 最近錯誤\n樂奈 回覆逾時，請稍後再試。\n**錯誤代碼**　`timeout`", shown)

    async def test_unresponsive_codex_is_named_on_ai_page_and_overview(self) -> None:
        self.codex.get_runtime_status.return_value = RuntimeStatus(reason="unresponsive")
        self.admin.ai.set_allowlist(G, frozenset({600}), roles=False)
        self.admin.ai.set_allowlist(G, frozenset({700}), roles=True)
        self.channels[600] = text_channel(600, self.guild)
        self.role(700)
        shown = self.screen(await self.open_panel())
        self.assertIn("**🤖 樂奈**\ncodex 無回應", shown)
        shown = self.screen(await self.go("樂奈"))
        self.assertIn("codex 無回應，請手動重啟 codex 容器後再重新整理。", shown)
        with self.assertRaises(StopIteration):
            self.button("登入")


class AiAccessPageTests(PanelCase):
    CHANNELS = "選擇樂奈白名單文字頻道（可複選）"

    async def open_access(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        await self.go("權限")

    async def test_channel_allowlist_saves_the_selected_channels(self) -> None:
        await self.open_access()
        self.channels[600] = channel = text_channel(600, self.guild)
        clicked = await self.choose(self.select(self.CHANNELS), resolved(channel))
        self.assertEqual(self.admin.ai.channel_ids, frozenset({600}))
        self.assertIn("### 白名單頻道 · 1 個", self.screen(clicked))
        self.assertIn("✅ 已更新 1 個白名單頻道。", self.screen(clicked))

    async def test_channel_removal_saves_at_once_and_stops_only_revoked_requests(self) -> None:
        self.admin.ai.set_allowlist(G, frozenset({600, 601}), roles=False)
        for channel_id in (600, 601):
            self.channels[channel_id] = text_channel(channel_id, self.guild)
        await self.open_access()
        clicked = await self.choose(self.select(self.CHANNELS), resolved(self.channels[600]))
        self.assertEqual(self.admin.ai.channel_ids, frozenset({600}))
        self.assertIn("✅ 已更新 1 個白名單頻道。", self.screen(clicked))
        self.codex.cancel_revoked.assert_awaited_once()
        self.codex.archive_scope.assert_not_awaited()

    async def test_channel_from_another_guild_is_rejected(self) -> None:
        await self.open_access()
        stranger = text_channel(600, discord.Object(id=G + 1))
        shown = self.screen(await self.choose(self.select(self.CHANNELS), resolved(stranger)))
        self.assertIn("只能選擇目前伺服器的一般文字頻道。", shown)
        self.assertEqual(self.admin.ai.channel_ids, frozenset())

    async def test_target_deleted_before_the_change_commits_is_not_saved(self) -> None:
        await self.open_access()
        self.channels[600] = channel = text_channel(600, self.guild)
        access, change = self.admin.access, self.admin.access.change

        async def deleted_first(*args, **kwargs):
            self.channels.pop(600)
            return await change(*args, **kwargs)

        with patch.object(access, "change", side_effect=deleted_first):
            await self.choose(self.select(self.CHANNELS), resolved(channel))
        self.assertEqual(self.admin.ai.channel_ids, frozenset())

    async def test_clearing_roles_says_nobody_may_use_the_bot(self) -> None:
        self.admin.ai.set_allowlist(G, frozenset({600}), roles=False)
        self.admin.ai.set_allowlist(G, frozenset({700}), roles=True)
        self.channels[600] = text_channel(600, self.guild)
        self.role(700)
        await self.open_access()
        roles = self.select("選擇白名單身分組（清空即暫停）")
        shown = self.screen(await self.choose(roles))
        self.assertIn(f"### {ICONS['warn']} 確認清空樂奈白名單身分組\n清空後沒有任何人可以使用樂奈", shown)
        self.assertEqual(self.admin.ai.role_ids, frozenset({700}))
        shown = self.screen(await self.click(self.button("取消")))
        self.assertIn("### 白名單身分組 · 1 個", shown)
        self.assertEqual([value.id for value in roles.default_values], [700])
        self.assertEqual(self.admin.ai.role_ids, frozenset({700}))
        await self.choose(roles)
        shown = self.screen(await self.click(self.button("確認清空")))
        self.assertEqual(self.admin.ai.role_ids, frozenset())
        self.assertIn("已清除白名單身分組，目前沒有人可以使用樂奈。", shown)

    async def test_selected_channels_and_roles_are_listed_and_deleted_ones_named_by_id(self) -> None:
        self.admin.ai.set_allowlist(G, frozenset({600, 601}), roles=False)
        self.admin.ai.set_allowlist(G, frozenset({700, 701}), roles=True)
        self.channels[600] = text_channel(600, self.guild)
        self.role(700)
        await self.open_access()
        shown = text(self.view)
        self.assertIn("### 白名單頻道 · 2 個\n<#600>、（已刪除）ID 601\n", shown)
        self.assertIn("### 白名單身分組 · 2 個\n<@&700>、（已刪除）ID 701\n", shown)

    async def test_long_role_list_is_cut_with_the_total(self) -> None:
        role_ids = frozenset(range(700, 725))
        self.admin.ai.set_allowlist(G, frozenset({600}), roles=False)
        self.admin.ai.set_allowlist(G, role_ids, roles=True)
        self.channels[600] = text_channel(600, self.guild)
        self.guild.get_role.side_effect = lambda role_id: object()
        await self.open_access()
        shown = text(self.view)
        listed = "、".join(f"<@&{role_id}>" for role_id in range(700, 710))
        self.assertIn(f"### 白名單身分組 · 25 個\n{listed} 等 25 個\n", shown)
        self.assertNotIn("<@&710>", shown)


class AiSwitchedOffTests(PanelCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.admin.ai.set_enabled(False)

    async def test_status_page_points_at_its_own_button_and_shows_the_switch_state(self) -> None:
        shown = self.screen(await self.open_panel())
        self.assertIn("⚪ 停用 · **🤖 樂奈**\n未開啟", shown)
        self.assertNotIn("此功能目前關閉", shown)
        shown = self.screen(await self.go("樂奈"))
        self.assertIn("⚪ **此功能目前關閉**　按下方的開啟按鈕即可開啟；設定仍可修改。", shown)
        self.assertIn("### 目前狀態\n⚪ 已關閉\n", shown)
        self.assertNotIn("「AI・狀態」頁", shown)
        self.view.toggled_at.clear()
        shown = self.screen(await self.click(self.button("開啟樂奈")))
        self.assertIn("### 目前狀態\n🟢 已開啟\n", shown)
        self.assertNotIn("此功能目前關閉", shown)

    async def test_other_ai_pages_point_at_the_status_page_and_stay_editable(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        for tab in ("權限", "模型", "技術"):
            with self.subTest(tab):
                shown = self.screen(await self.go(tab))
                self.assertIn("⚪ **此功能目前關閉**　可在「AI・狀態」頁開啟；設定仍可修改。", shown)
        await self.go("權限")
        channels = self.select(AiAccessPageTests.CHANNELS)
        self.assertFalse(channels.disabled)
        self.channels[600] = channel = text_channel(600, self.guild)
        shown = self.screen(await self.choose(channels, resolved(channel)))
        self.assertEqual(self.admin.ai.channel_ids, frozenset({600}))
        self.assertIn("✅ 已更新 1 個白名單頻道。", shown)
        self.assertFalse(self.select("選擇白名單身分組（清空即暫停）").disabled)


if __name__ == "__main__":
    unittest.main()
