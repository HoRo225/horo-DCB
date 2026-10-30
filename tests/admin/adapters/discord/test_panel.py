import asyncio
import unittest
from unittest.mock import patch

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, NEUTRAL_COLOUR, WARN_COLOUR
from src.core.adapters.sqlite.db import transaction
from src.games.adapters.sqlite.store import GAMES_CHANNEL_ID, GAMES_HUB_MESSAGE
from src.ai import RuntimeStatus
from tests.support.admin import G, OWNER, PanelCase, text_channel
from tests.support.testing import put_setting, text


class OpenTests(PanelCase):
    async def test_open_publishes_the_overview(self) -> None:
        opened = await self.open_panel()
        opened.response.defer.assert_awaited_once()
        self.assertEqual(self.title(opened), "## 🏠 管理控制台")
        self.assertNotIn("額度", self.screen(opened))

    async def test_only_administrators_may_open_the_panel(self) -> None:
        refused = self.interaction(admin=False)
        await self.command.callback(self.binding, refused)
        self.assertIn("此指令僅限伺服器管理員使用。", text(refused.response.send_message.await_args.kwargs["view"]))
        refused.edit_original_response.assert_not_awaited()

    async def test_overview_lists_four_features(self) -> None:
        opened = await self.open_panel()
        rows = [item for item in self.view.walk_children()
                if isinstance(item, discord.ui.Section) and isinstance(item.accessory, discord.ui.Button)]
        self.assertEqual(len(rows), 4)
        self.assertEqual({row.accessory.label for row in rows}, {"管理"})
        shown = self.screen(opened)
        self.assertIn("🟠 需要處理 · **🎲 遊戲大廳**\n尚未設定遊戲頻道", shown)
        self.assertIn("🟠 需要處理 · **🤖 樂奈**\n白名單頻道尚未設定", shown)
        self.assertIn("-# 4 項功能 · 2 個需要處理 · 2 個停用 · 尚未備份 · 閒置 10 分鐘後自動關閉", shown)
        pending = self.button("待處理項目（0）")
        self.assertEqual((pending.disabled, pending.style), (True, discord.ButtonStyle.secondary))

    async def test_overview_subtitle_says_when_the_last_backup_was_written(self) -> None:
        self.admin.last_backup = 1_793_000_000
        shown = self.screen(await self.open_panel())
        self.assertIn("-# 4 項功能 · 2 個需要處理 · 2 個停用 · 上次備份 <t:1793000000:R> · 閒置 10 分鐘後自動關閉", shown)
        self.assertNotIn("尚未備份", shown)

    async def test_overview_is_one_card_with_attention_above(self) -> None:
        shown = self.screen(await self.open_panel())
        self.assertEqual([card.accent_colour for card in self.view.children], [WARN_COLOUR, BRAND_COLOUR])
        self.assertIn("樂奈、遊戲大廳需要處理，請按「管理」查看。", shown)
        self.assertIsInstance(self.view.children[-1].children[-1], discord.ui.ActionRow)

    async def test_overview_counts_pending_effects_and_opens_them(self) -> None:
        with transaction(self.db):
            self.admin.operations.create_effect("e1", "voice.create", 700, {})
        shown = self.screen(await self.open_panel())
        self.assertIn("有 1 項送出結果待核對", shown)
        pending = self.button("待處理項目（1）")
        self.assertEqual((pending.disabled, pending.style), (False, discord.ButtonStyle.primary))
        self.assertEqual(self.title(await self.click(pending)), f"## {ICONS['warn']} 待處理項目")


class NavigationTests(PanelCase):
    async def test_navigation_shows_each_page_title(self) -> None:
        await self.open_panel()
        for label, title in (
            ("樂奈", "## 🤖 樂奈"),
            ("權限", "## 🔐 樂奈使用權限"),
            ("模型", "## 🎛️ 樂奈模型設定"),
            ("狀態", "## 🤖 樂奈"),
            ("臨時語音", "## 🎙️ 臨時語音"),
            ("Steam 免費遊戲", "## 🎮 Steam 免費遊戲"),
            ("遊戲大廳", "## 🎲 遊戲大廳"),
            ("返回總覽", "## 🏠 管理控制台"),
        ):
            with self.subTest(label):
                self.assertEqual(self.title(await self.go(label)), title)

    async def test_current_tab_only_defers(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        self.assertEqual(self.button("狀態").style, discord.ButtonStyle.primary)
        self.assertEqual(self.button("權限").style, discord.ButtonStyle.secondary)
        reads = self.model.get_runtime_status.await_count
        clicked = await self.click(self.button("狀態"))
        clicked.response.defer.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()
        clicked.edit_original_response.assert_not_awaited()
        self.assertEqual(self.model.get_runtime_status.await_count, reads)

    async def test_back_button_reloads_the_overview(self) -> None:
        await self.open_panel()
        await self.go("遊戲大廳")
        with transaction(self.db):
            self.admin.operations.create_effect("e1", "voice.create", 700, {})
        clicked = await self.click(self.button("返回總覽"))
        self.assertEqual(self.title(clicked), "## 🏠 管理控制台")
        shown = self.screen(clicked)
        self.assertIn("有 1 項送出結果待核對", shown)
        clicked.response.edit_message.assert_awaited_once()
        with self.assertRaises(StopIteration):
            self.button("返回總覽")

    async def test_page_menu_under_every_page_opens_the_others(self) -> None:
        await self.open_panel()

        def menu() -> list[str]:
            row = self.view.children[-1].children[-1]
            [item] = row.children
            self.assertEqual(item.placeholder, "前往其他頁面…")
            self.menu = item
            return [option.label for option in item.options]

        pages = ["總覽", "AI・狀態", "AI・權限", "AI・模型", "臨時語音", "Steam 免費遊戲", "遊戲大廳"]
        self.assertEqual(menu(), pages[1:])
        self.assertEqual(self.title(await self.choose(self.menu, "games")), "## 🎲 遊戲大廳")
        self.assertEqual(menu(), [page for page in pages if page != "遊戲大廳"])
        self.assertEqual(self.title(await self.choose(self.menu, "ai_access")), "## 🔐 樂奈使用權限")
        self.assertEqual(menu(), [page for page in pages if page != "AI・權限"])
        self.assertEqual(self.title(await self.choose(self.menu, "ai_models")), "## 🎛️ 樂奈模型設定")
        self.assertEqual(self.title(await self.choose(self.menu, "overview")), "## 🏠 管理控制台")
        with transaction(self.db):
            self.admin.operations.create_effect("e1", "voice.create", 700, {})
        await self.click(self.button("重新整理"))
        self.assertEqual(menu(), [*pages[1:], "待處理項目"])
        self.assertEqual(self.title(await self.choose(self.menu, "effects")), f"## {ICONS['warn']} 待處理項目")
        self.assertEqual(menu(), pages)

    async def test_refresh_redraws_current_page(self) -> None:
        await self.open_panel()
        clicked = await self.click(self.button("重新整理"))
        self.assertEqual(self.title(clicked), "## 🏠 管理控制台")
        await self.go("樂奈")
        self.model.get_runtime_status.return_value = RuntimeStatus("ready", "v0.13.7")
        clicked = await self.click(self.button("重新整理"))
        self.assertEqual(self.title(clicked), "## 🤖 樂奈")
        self.assertIn("clewdr 已連線（v0.13.7）", self.screen(clicked))

    async def test_quick_refresh_edits_in_one_reply(self) -> None:
        await self.open_panel()
        clicked = await self.click(self.button("重新整理"))
        clicked.response.defer.assert_not_awaited()
        clicked.edit_original_response.assert_not_awaited()
        self.assertEqual(self.title(clicked), "## 🏠 管理控制台")

    async def test_slow_refresh_defers_then_edits(self) -> None:
        await self.open_panel()

        async def slow_status() -> RuntimeStatus:
            await asyncio.sleep(0.01)
            return RuntimeStatus("ready", "v0.13.6")

        self.model.get_runtime_status.side_effect = slow_status
        with patch("src.admin.adapters.discord.panel.QUICK_REPLY_SECONDS", 0):
            clicked = await self.click(self.button("重新整理"))
        clicked.response.defer.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()
        self.assertEqual(self.title(clicked), "## 🏠 管理控制台")

    async def test_warning_note_turns_page_orange(self) -> None:
        await self.open_panel()
        self.channels[800] = text_channel(800, self.guild)
        put_setting(self.db, GAMES_CHANNEL_ID, 800)
        put_setting(self.db, GAMES_HUB_MESSAGE, [800, 801])
        await self.go("遊戲大廳")
        roles = self.select("選擇可用身分組（清空即所有人可用）")
        outsider = self.role(901)
        outsider.guild = discord.Object(id=G + 1)
        shown = self.screen(await self.choose(roles, outsider))
        self.assertIn("⚠️ 只能選擇目前伺服器的一般身分組。", shown)
        self.assertEqual([card.accent_colour for card in self.view.children], [WARN_COLOUR])
        await self.choose(roles, self.role(900))
        self.assertEqual([card.accent_colour for card in self.view.children], [BRAND_COLOUR])


class AuthorityTests(PanelCase):
    async def test_rejects_other_user_non_admin_and_idle(self) -> None:
        await self.open_panel()
        for case, interaction, denial in (
            ("other user", self.interaction(OWNER + 1), "只有開啟這個控制台的人可以操作。"),
            ("not admin", self.interaction(admin=False), "你已不是伺服器管理員，無法操作控制台。"),
            ("idle", None, "控制台已過期，請重新輸入 /控制台。"),
        ):
            with self.subTest(case):
                if interaction is None:
                    await self.view.on_timeout()
                    interaction = self.interaction()
                self.assertFalse(await self.view.interaction_check(interaction))
                notice = interaction.response.send_message.await_args.kwargs["view"]
                self.assertEqual(text(notice), f"{ICONS['deadline' if case == 'idle' else 'warn']} {denial}")
                self.assertEqual(notice.children[0].accent_colour, NEUTRAL_COLOUR if case == "idle" else WARN_COLOUR)

    async def test_missing_current_guild_blocks_a_previously_authorized_panel(self) -> None:
        await self.open_panel()
        self.admin.client.get_guild.side_effect = lambda guild_id: None
        clicked = self.interaction()
        self.assertTrue(clicked.permissions.administrator)
        self.assertFalse(await self.view.interaction_check(clicked))

    async def test_revoked_member_admin_blocks_the_panel_even_with_interaction_permission(self) -> None:
        await self.open_panel()
        self.revoke_admin()
        self.assertFalse(await self.view.interaction_check(self.interaction()))


class RetirementTests(PanelCase):
    async def test_idle_timeout_greys_out_panel(self) -> None:
        opened = await self.open_panel()
        self.assertEqual(self.view.timeout, 600)
        await self.view.on_timeout()
        await asyncio.wait({self.view.retirement_task})
        self.assertFalse(self.view.alive)
        opened.delete_original_response.assert_not_awaited()
        self.assertEqual(opened.edit_original_response.await_count, 2)
        self.assertIs(opened.edit_original_response.await_args.kwargs["view"], self.view)
        self.assertIn(f"## {ICONS['closed']} 管理控制台\n-# 閒置 10 分鐘，已自動關閉", text(self.view))
        self.assertEqual([card.accent_colour for card in self.view.children], [NEUTRAL_COLOUR])
        self.assertFalse([item for item in self.view.walk_children()
                          if isinstance(item, (discord.ui.Button, discord.ui.Select))])

    async def test_replaced_or_shut_down_panel_is_deleted(self) -> None:
        first = await self.open_panel()
        old = self.view
        second = await self.open_panel()
        await asyncio.wait({old.retirement_task})
        first.delete_original_response.assert_awaited_once()
        first.edit_original_response.assert_awaited_once()
        await self.admin.panels.close(deadline=asyncio.get_running_loop().time() + 5)
        second.delete_original_response.assert_awaited_once()
        second.edit_original_response.assert_awaited_once()

    async def test_panel_that_cannot_be_deleted_is_greyed_out_as_closed(self) -> None:
        first = await self.open_panel()
        old = self.view
        first.delete_original_response.side_effect = discord.HTTPException(
            type("Response", (), {"status": 500, "reason": "boom"})(), "boom"
        )
        with self.assertLogs(level="ERROR"):
            await self.open_panel()
            await asyncio.wait({old.retirement_task})
        self.assertIs(first.edit_original_response.await_args.kwargs["view"], old)
        self.assertIn(f"## {ICONS['closed']} 管理控制台\n-# 已關閉", text(old))
        self.assertEqual(old.children[0].accent_colour, NEUTRAL_COLOUR)


if __name__ == "__main__":
    unittest.main()
