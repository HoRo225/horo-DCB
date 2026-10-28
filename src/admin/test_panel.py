import asyncio
import dataclasses
import itertools
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import discord

from src.admin.pages.games import games_state
from src.codex.model_settings import ModelInfo
from src.codex.protocol import CodexRuntimeStatus
from src.codex.rate_limits import CodexRateLimits, CodexRateWindow
from src.codex.service import DeviceLogin
from src.admin.cog import AdminCog
from src.bot import HoroBot
from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, NEUTRAL_COLOUR, WARN_COLOUR
from src.core.adapters.sqlite.db import open_db, transaction
from src.testing import put_setting
from src.steam.models import SteamFetchResult, SteamOffer
from src.testing import group, text
from src.core.app.supervisor import PRESENTATION, WORK
from src.ai.access import AI_ENABLED
from src.games.store import GAMES_HUB_MESSAGE
from src.steam.notifier import STEAM_CHANNEL_ID
from src.voice.manager import VOICE_ENABLED, VOICE_ENTRY_CHANNEL_ID

G, OWNER = 100, 200


class PanelShutdownTests(unittest.IsolatedAsyncioTestCase):
    def registry(self):
        from src.admin.sessions import PanelSessionRegistry

        self.presentations, self.mutations = group(PRESENTATION), group(WORK)
        return PanelSessionRegistry(self.presentations, self.mutations)

    async def test_retired_sessions_mutations_and_shutdown_stay_owned(self) -> None:
        registry = self.registry()
        session = registry.begin((G, OWNER))
        gate = asyncio.Event()
        presentation = registry.spawn(session, gate.wait())
        mutation = registry.mutate(gate.wait())
        self.assertEqual((self.presentations.pending(), self.mutations.pending()), ({presentation}, {mutation}))
        registry.retire(session)
        self.assertEqual((self.presentations.pending(), self.mutations.pending()), ({presentation}, {mutation}))
        closing = asyncio.create_task(registry.close(deadline=asyncio.get_running_loop().time() + 1))
        await asyncio.sleep(0)
        self.assertEqual(len(self.mutations.pending() - {mutation}), 1)
        gate.set()
        await closing
        self.assertEqual((self.presentations.pending(), self.mutations.pending()), (set(), set()))

    async def test_shutdown_does_not_cancel_screen_retirement_cleanup(self) -> None:
        from types import SimpleNamespace

        registry = self.registry()
        session = registry.begin((G, OWNER))
        deleted = []

        async def cleanup():
            await asyncio.sleep(0.02)
            deleted.append(True)

        view = SimpleNamespace(_retirement_task=None, is_finished=lambda: True)

        def retire():
            if view._retirement_task is None:
                view._retirement_task = registry.spawn(session, cleanup())

        view.retire_session = retire
        registry.attach_view(session, view)
        await registry.close(deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(deleted, [True])
        self.assertFalse(view._retirement_task.cancelled())

    async def test_retirement_and_cancelled_close_keep_mutation_owned(self) -> None:
        registry = self.registry()
        session = registry.begin((G, OWNER))
        gate = asyncio.Event()
        mutation = registry.mutate(gate.wait())
        registry.stop_new_work()
        self.assertTrue(session.retired)
        self.assertFalse(registry.is_current(session))
        self.assertIn(mutation, self.mutations.pending())
        self.assertFalse(mutation.cancelling())
        deadline = asyncio.get_running_loop().time() + 1
        caller = asyncio.create_task(registry.close(deadline=deadline))
        await asyncio.sleep(0)
        [shared] = self.mutations.pending() - {mutation}
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(shared.done())
        self.assertIn(mutation, self.mutations.pending())
        gate.set()
        await registry.close(deadline=deadline + 1)
        self.assertTrue(shared.done())
        self.assertFalse(self.mutations.pending())

STATUS = CodexRuntimeStatus(reason="ready", authenticated=True, plan="plus", runtime_version="1.0", thread_count=3)
LOGGED_OUT = CodexRuntimeStatus(reason="auth_required", runtime_version="1.0")
LOGIN = DeviceLogin("login-1", "https://auth.openai.com/codex/device", "ABCD-1234")
MODELS = (
    ModelInfo("gpt-a", "Model A", "medium", ("low", "medium"), ("text",)),
    ModelInfo("gpt-b", "Model B", "high", ("high",), ("text",)),
)


def limits(used: int) -> CodexRateLimits:
    return CodexRateLimits(int(time.time()), (CodexRateWindow("primary", used, 300, None),), None)


def resolved(channel: object) -> MagicMock:
    return MagicMock(resolve=MagicMock(return_value=channel))


def text_channel(channel_id: int, guild: object, **permissions: bool) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.type, channel.guild = channel_id, discord.ChannelType.text, guild
    channel.permissions_for.return_value = discord.Permissions(
        **({"view_channel": True, "send_messages": True} | permissions)
    )
    return channel


class AdminPanelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = db = open_db(":memory:")
        self.addCleanup(db.close)
        put_setting(db, AI_ENABLED, True)
        self.codex = MagicMock()
        self.codex.get_runtime_status = AsyncMock(return_value=STATUS)
        self.codex.get_rate_limits = AsyncMock(return_value=limits(20))
        self.codex.get_models = AsyncMock(return_value=MODELS)
        self.codex.cancel = AsyncMock()
        self.codex.request_counts.return_value = (0, 0)
        self.bot = HoroBot(self.codex, guild_id=G, db=db)
        await self.bot.add_cog(AdminCog(self.bot, self.bot.live), guild=discord.Object(id=G))
        self.bot.steam_free_games.provider = AsyncMock()
        self.guild = MagicMock()
        self.guild.id = G
        self.guild.unavailable = False
        self.guild.me.display_name = "樂奈"
        self.guild.get_member.return_value.guild_permissions.administrator = True
        self.guild.get_member.return_value.bot = False
        self.guild.get_member.return_value.display_name = "成員"
        self.bot.get_guild = MagicMock(return_value=self.guild)
        self.channels: dict[int, object] = {}
        self.guild.get_channel.side_effect = self.channels.get
        self.guild.get_role.return_value = None
        self.ids = itertools.count(1)

    async def asyncTearDown(self) -> None:
        await self.bot.admin_panels.close(deadline=asyncio.get_running_loop().time() + 5)

    def interaction(self, user_id: int = OWNER, *, admin: bool = True) -> MagicMock:
        fake = MagicMock()
        fake.id = next(self.ids)
        fake.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        fake.user.id = user_id
        fake.user.roles = []
        fake.guild_id = G
        fake.guild = self.guild
        fake.permissions.administrator = admin
        fake.response.is_done = MagicMock(return_value=False)

        def respond(*args: object, **kwargs: object) -> None:
            if fake.response.is_done():
                raise discord.InteractionResponded(fake)
            fake.response.is_done.return_value = True

        fake.response.defer = AsyncMock(side_effect=respond)
        fake.response.send_message = AsyncMock(side_effect=respond)
        fake.response.edit_message = AsyncMock(side_effect=respond)
        fake.response.send_modal = AsyncMock()
        fake.edit_original_response = AsyncMock()
        fake.delete_original_response = AsyncMock()
        fake.followup.send = AsyncMock()
        return fake

    async def open_panel(self) -> MagicMock:
        command = self.bot.tree.get_command("控制台", guild=discord.Object(id=G))
        opened = self.interaction()
        await command.callback(command.binding, opened)
        self.view = opened.edit_original_response.await_args.kwargs["view"]
        return opened

    def screen(self, interaction: MagicMock) -> str:
        edited = interaction.response.edit_message.await_args
        view = (edited or interaction.edit_original_response.await_args).kwargs["view"]
        self.assertIs(view, self.view)
        return text(view)

    def title(self, interaction: MagicMock) -> str:
        return next(line for line in self.screen(interaction).splitlines() if line.startswith("## "))

    def button(self, label: str) -> discord.ui.Button:
        return next(
            item
            for item in self.view.walk_children()
            if isinstance(item, discord.ui.Button) and item.label == label
        )

    def role(self, role_id: int) -> MagicMock:
        role = MagicMock(spec=discord.Role)
        role.configure_mock(id=role_id, guild=self.guild, mentionable=True)
        role.is_default.return_value = False
        self.guild.get_role.side_effect = {role_id: role}.get
        return role

    def select(self, placeholder: str) -> discord.ui.Item:
        return next(
            item
            for item in self.view.walk_children()
            if getattr(item, "placeholder", None) == placeholder
        )

    def option_select(self, label: str) -> tuple[discord.ui.Select, str]:
        for item in self.view.walk_children():
            if isinstance(item, discord.ui.Select):
                for option in item.options:
                    if option.label == label:
                        return item, option.value
        raise AssertionError(f"no option {label}")

    async def click(self, item: discord.ui.Item, interaction: MagicMock | None = None) -> MagicMock:
        interaction = interaction or self.interaction()
        self.assertTrue(await self.view.interaction_check(interaction))
        await item.callback(interaction)
        return interaction

    async def choose(self, item: discord.ui.Item, *values: object) -> MagicMock:
        item._values = list(values)
        return await self.click(item)

    async def go(self, label: str) -> MagicMock:
        try:
            return await self.click(self.button(label))
        except StopIteration:
            pass
        if self.view.page != "overview":
            await self.click(self.button("返回總覽"))
        row = next(
            item
            for item in self.view.walk_children()
            if isinstance(item, discord.ui.Section)
            and any(f"{label}**" in child.content for child in item.children)
        )
        return await self.click(row.accessory)

    async def test_open_publishes_overview(self) -> None:
        opened = await self.open_panel()
        opened.response.defer.assert_awaited_once()
        self.assertEqual(self.title(opened), "## 🏠 管理控制台")
        self.assertIn("**5 小時**　▰▰▰▰▰▰▰▰▱▱ 剩餘 80%", self.screen(opened))

    async def test_overview_lists_four_features(self) -> None:
        opened = await self.open_panel()
        rows = [
            item
            for item in self.view.walk_children()
            if isinstance(item, discord.ui.Section) and isinstance(item.accessory, discord.ui.Button)
        ]
        self.assertEqual(len(rows), 4)
        shown = self.screen(opened)
        self.assertIn("4 項功能", shown)
        self.assertIn("🟠 需要處理 · **🎲 遊戲大廳**\n尚未設定遊戲頻道", shown)
        pending = self.button("待處理項目（0）")
        self.assertEqual((pending.disabled, pending.style), (True, discord.ButtonStyle.secondary))
        self.assertIn("🟠 需要處理 · **🤖 樂奈**\n白名單頻道尚未設定", shown)
        self.assertNotIn("發言頻道", shown)
        self.assertNotIn("樂奈金庫", shown)
        self.assertEqual({row.accessory.label for row in rows}, {"管理"})
        self.assertIn("-# 4 項功能 · 2 個需要處理 · 2 個停用 · 閒置 10 分鐘後自動關閉", shown)

    async def test_overview_is_one_card_with_attention_above(self) -> None:
        shown = self.screen(await self.open_panel())
        self.assertEqual([card.accent_colour for card in self.view.children], [WARN_COLOUR, BRAND_COLOUR])
        self.assertIn("樂奈、遊戲大廳需要處理，請按「管理」查看。", shown)
        self.assertIsInstance(self.view.children[-1].children[-1], discord.ui.ActionRow)

    async def test_overview_counts_pending_effects_and_opens_them(self) -> None:
        with transaction(self.db):
            self.bot.effects.operations.create_effect("e1", "action.receipt", 700, {})
        shown = self.screen(await self.open_panel())
        self.assertIn("有 1 項送出結果待核對", shown)
        pending = self.button("待處理項目（1）")
        self.assertEqual((pending.disabled, pending.style), (False, discord.ButtonStyle.primary))
        clicked = await self.click(pending)
        self.assertEqual(self.title(clicked), f"## {ICONS['warn']} 待處理項目")
        await self.go("Steam 免費遊戲")
        pending = self.button("待處理項目（1）")
        self.assertEqual(str(pending.emoji), ICONS["warn"])
        self.assertEqual(self.title(await self.click(pending)), f"## {ICONS['warn']} 待處理項目")

    async def test_navigation_shows_each_page_title(self) -> None:
        await self.open_panel()
        for label, title in (
            ("樂奈", "## 🤖 樂奈"),
            ("權限", "## 🔐 樂奈使用權限"),
            ("模型", "## 🎛️ 樂奈模型設定"),
            ("技術", "## 🛠️ 樂奈技術資訊"),
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
        reads = self.codex.get_runtime_status.await_count
        clicked = await self.click(self.button("狀態"))
        clicked.response.defer.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()
        clicked.edit_original_response.assert_not_awaited()
        self.assertEqual(self.codex.get_runtime_status.await_count, reads)

    async def test_back_button_returns_to_overview_without_reading_codex(self) -> None:
        await self.open_panel()
        await self.go("遊戲大廳")
        reads = (self.codex.get_runtime_status.await_count, self.codex.get_rate_limits.await_count)
        self.codex.get_rate_limits.return_value = limits(50)
        clicked = await self.click(self.button("返回總覽"))
        self.assertEqual(self.title(clicked), "## 🏠 管理控制台")
        self.assertEqual((self.codex.get_runtime_status.await_count, self.codex.get_rate_limits.await_count), reads)
        self.assertIn("**5 小時**　▰▰▰▰▰▰▰▰▱▱ 剩餘 80%", self.screen(clicked))
        clicked.response.edit_message.assert_awaited_once()
        with self.assertRaises(StopIteration):
            self.button("返回總覽")
        self.assertIn("50%", self.screen(await self.click(self.button("重新整理"))))

    async def test_page_menu_under_every_page_opens_the_others(self) -> None:
        await self.open_panel()

        def menu() -> list[str]:
            row = self.view.children[-1].children[-1]
            [item] = row.children
            self.assertEqual(item.placeholder, "前往其他頁面…")
            self.menu = item
            return [option.label for option in item.options]

        pages = ["樂奈", "臨時語音", "Steam 免費遊戲", "遊戲大廳", "待處理項目"]
        self.assertEqual(menu(), pages)
        clicked = await self.choose(self.menu, "games")
        self.assertEqual(self.title(clicked), "## 🎲 遊戲大廳")
        self.assertEqual(menu(), [page for page in pages if page != "遊戲大廳"])
        self.guild.me.display_name = "Horo"
        await self.choose(self.menu, "ai")
        await self.go("權限")
        self.assertEqual(menu(), pages[1:])
        await self.go("返回總覽")
        self.assertEqual(menu(), ["Horo", *pages[1:]])
        clicked = await self.choose(self.menu, "effects")
        self.assertEqual(self.title(clicked), f"## {ICONS['warn']} 待處理項目")
        self.assertEqual(menu(), ["Horo", *pages[1:-1]])

    async def test_refresh_redraws_current_page(self) -> None:
        await self.open_panel()
        self.codex.get_rate_limits.return_value = limits(50)
        clicked = await self.click(self.button("重新整理"))
        self.assertEqual(self.title(clicked), "## 🏠 管理控制台")
        self.assertIn("**5 小時**　▰▰▰▰▰▱▱▱▱▱ 剩餘 50%", self.screen(clicked))

        await self.go("樂奈")
        await self.go("技術")
        self.codex.get_runtime_status.return_value = dataclasses.replace(STATUS, thread_count=7)
        clicked = await self.click(self.button("重新整理"))
        self.assertEqual(self.title(clicked), "## 🛠️ 樂奈技術資訊")
        self.assertIn("**持久討論串**　7 條", self.screen(clicked))

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

    async def test_quick_refresh_edits_in_one_reply(self) -> None:
        await self.open_panel()
        clicked = await self.click(self.button("重新整理"))
        clicked.response.defer.assert_not_awaited()
        clicked.edit_original_response.assert_not_awaited()
        self.assertEqual(self.title(clicked), "## 🏠 管理控制台")

    async def test_slow_refresh_defers_then_edits(self) -> None:
        await self.open_panel()
        with patch("src.admin.panel.QUICK_REPLY_SECONDS", 0):
            clicked = await self.click(self.button("重新整理"))
        clicked.response.defer.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()
        self.assertEqual(self.title(clicked), "## 🏠 管理控制台")

    async def test_toggle_temp_voice(self) -> None:
        await self.open_panel()
        await self.go("臨時語音")
        voice = self.bot.temp_voice
        with patch.object(voice, "reconcile", wraps=voice.reconcile) as reconcile:
            clicked = await self.click(self.button("開啟臨時語音"))
            self.assertTrue(voice.enabled)
            self.assertIn("✅ 已開啟臨時語音。", self.screen(clicked))
            reconcile.assert_awaited_once()
            self.assertIs(reconcile.await_args.args[0], self.guild)

            self.view.toggled_at.clear()
            clicked = await self.click(self.button("關閉臨時語音"))
            self.assertFalse(voice.enabled)
            self.assertIn("✅ 已關閉臨時語音。", self.screen(clicked))
            reconcile.assert_awaited_once()
        self.button("開啟臨時語音")

    async def test_toggle_ai(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        self.guild.me.display_name = "Horo"
        reads = self.codex.get_runtime_status.await_count
        clicked = await self.click(self.button("關閉樂奈"))
        self.assertFalse(self.bot.ai_settings.enabled)
        self.assertIn("✅ 已關閉 Horo。", self.screen(clicked))
        self.assertEqual(self.codex.get_runtime_status.await_count, reads + 1)
        self.codex.cancel.assert_awaited_once_with(G)
        self.view.toggled_at.clear()
        clicked = await self.click(self.button("開啟 Horo"))
        self.assertTrue(self.bot.ai_settings.enabled)
        self.assertIn("✅ 已開啟 Horo。", self.screen(clicked))
        self.codex.cancel.assert_awaited_once()

    async def test_double_click_toggle_keeps_state(self) -> None:
        await self.open_panel()
        await self.go("臨時語音")
        button = self.button("開啟臨時語音")
        first, second = self.interaction(), self.interaction()
        self.assertTrue(await self.view.interaction_check(first))
        self.assertTrue(await self.view.interaction_check(second))
        await asyncio.gather(button.callback(first), button.callback(second))
        self.assertTrue(self.bot.temp_voice.enabled)

        self.view.toggled_at["temp_voice"] -= 2
        await self.click(button)
        self.assertFalse(self.bot.temp_voice.enabled)

    async def test_voice_entry_select_binds_entry(self) -> None:
        await self.open_panel()
        await self.go("臨時語音")
        entry = MagicMock(spec=discord.VoiceChannel)
        entry.id, entry.type, entry.guild = 500, discord.ChannelType.voice, self.guild
        self.channels[500] = entry
        voice = self.bot.temp_voice
        with patch.object(voice, "bind_entry", wraps=voice.bind_entry) as bind_entry:
            clicked = await self.choose(self.select("選擇臨時語音入口頻道"), resolved(entry))
            bind_entry.assert_awaited_once()
            self.assertEqual(bind_entry.await_args.args, (self.guild, entry))
            self.assertTrue(callable(bind_entry.await_args.kwargs["still_current"]))
        self.assertIn("已綁定入口；開啟臨時語音後生效。", self.screen(clicked))

    async def test_admin_revoked_while_setting_waits_for_owner_lock_cannot_commit(self) -> None:
        await self.open_panel()
        await self.go("臨時語音")
        voice = self.bot.temp_voice
        entry = MagicMock(spec=discord.VoiceChannel)
        entry.id, entry.type, entry.guild = 500, discord.ChannelType.voice, self.guild
        self.channels[500] = entry
        entered = asyncio.Event()
        original = voice.bind_entry

        async def bind(*args, **kwargs):
            entered.set()
            return await original(*args, **kwargs)

        await voice._lock.acquire()
        with patch.object(voice, "bind_entry", side_effect=bind):
            pending = asyncio.create_task(self.choose(self.select("選擇臨時語音入口頻道"), resolved(entry)))
            await entered.wait()
            self.guild.get_member.return_value.guild_permissions.administrator = False
            voice._lock.release()
            await pending
        self.assertIsNone(voice.status().entry_channel_id)

    async def test_missing_current_guild_blocks_a_previously_authorized_panel(self) -> None:
        await self.open_panel()
        self.bot.get_guild.return_value = None
        clicked = self.interaction()
        self.assertTrue(clicked.permissions.administrator)
        self.assertFalse(await self.view.interaction_check(clicked))

    async def test_steam_query_renders_result(self) -> None:
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        provider = self.bot.steam_free_games.provider
        provider.fetch_current_offers.return_value = SteamFetchResult(
            frozenset({10}), (SteamOffer(10, "Test Game", "NT$100", "", (), None),)
        )
        clicked = await self.click(self.button("重新查詢"))
        provider.fetch_current_offers.assert_awaited_once()
        self.assertIn("**Test Game**\n~~NT$100~~ → **免費**", self.screen(clicked))

    async def test_steam_toggle_and_notification_roles(self) -> None:
        self.channels[700] = text_channel(700, self.guild)
        put_setting(self.db, STEAM_CHANNEL_ID, 700)
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        steam, role = self.bot.steam_free_games, self.role(900)
        roles = self.select("選擇通知身分組（清空即取消）")
        self.assertTrue(roles.disabled)
        self.assertIn("Steam 自動通知未開啟，未修改身分組設定。", self.screen(await self.choose(roles, role)))
        self.assertEqual(steam.status().role_ids, ())

        with patch.object(steam, "start") as start:
            clicked = await self.click(self.button("開啟 Steam 自動通知"))
        start.assert_called_once_with(clicked.client)
        self.assertTrue(steam.enabled)
        self.assertIn("✅ 已開啟 Steam 自動通知。", self.screen(clicked))
        self.assertFalse(roles.disabled)

        shown = self.screen(await self.choose(roles, role))
        self.assertEqual(steam.status().role_ids, (900,))
        self.assertIn("已更新 Steam 免費遊戲通知身分組，共 1 個。", shown)
        self.assertIn("通知身分組 <@&900>", shown)
        shown = self.screen(await self.choose(roles))
        self.assertEqual(steam.status().role_ids, ())
        self.assertIn("已取消 Steam 免費遊戲通知身分組。", shown)

        self.view.toggled_at.clear()
        clicked = await self.click(self.button("關閉 Steam 自動通知"))
        self.assertFalse(steam.enabled)
        self.assertIn("✅ 已關閉 Steam 自動通知。", self.screen(clicked))
        self.assertTrue(roles.disabled)

    async def test_ai_channel_allowlist_calls_access_service(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        await self.go("權限")
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.type, channel.guild = 600, discord.ChannelType.text, self.guild
        self.channels[600] = channel
        service = self.bot.access_service
        with patch.object(service, "change", wraps=service.change) as change:
            clicked = await self.choose(
                self.select("選擇樂奈白名單文字頻道（可複選）"), resolved(channel)
            )
            change.assert_awaited_once_with(G, frozenset({600}), roles=False, still_current=ANY, operation_id=ANY)
        self.assertIn("### 白名單頻道 · 1 個", self.screen(clicked))
        self.assertIn("✅ 已更新 1 個白名單頻道。", self.screen(clicked))

    async def test_ai_channel_removal_saves_at_once_without_archiving(self) -> None:
        self.codex.cancel_revoked, self.codex.archive_scope = AsyncMock(), AsyncMock()
        self.bot.ai_settings.set_allowlist(G, frozenset({600, 601}), roles=False)
        for channel_id in (600, 601):
            self.channels[channel_id] = text_channel(channel_id, self.guild)
        await self.open_panel()
        await self.go("樂奈")
        await self.go("權限")
        clicked = await self.choose(self.select("選擇樂奈白名單文字頻道（可複選）"), resolved(self.channels[600]))
        self.assertEqual(self.bot.ai_settings.channel_ids, frozenset({600}))
        self.assertIn("✅ 已更新 1 個白名單頻道。", self.screen(clicked))
        self.codex.cancel_revoked.assert_awaited_once()
        self.codex.archive_scope.assert_not_awaited()

    async def test_allowlist_target_deleted_while_waiting_cannot_be_saved(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        await self.go("權限")
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.type, channel.guild = 600, discord.ChannelType.text, self.guild
        self.channels[600] = channel
        service, entered = self.bot.access_service, asyncio.Event()
        original = service.change

        async def change(*args, **kwargs):
            entered.set()
            return await original(*args, **kwargs)

        await service._lock.acquire()
        with patch.object(service, "change", side_effect=change):
            pending = asyncio.create_task(self.choose(self.select("選擇樂奈白名單文字頻道（可複選）"), resolved(channel)))
            await entered.wait()
            self.channels.pop(600)
            service._lock.release()
            await pending
        self.assertEqual(self.bot.ai_settings.channel_ids, frozenset())

    async def test_model_page_selects_and_saves(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        await self.go("模型")
        self.codex.get_models.assert_awaited()
        await self.choose(self.select("選擇主模型"), self.option_select("Model B")[1])
        await self.choose(self.select("選擇備援模型（可停用）"), "off")
        shown = self.screen(await self.click(self.button("重新整理")))
        self.assertIn("### 主模型\n**gpt-b** · 推理強度：高", shown)
        self.assertIn("草稿尚未儲存", shown)
        clicked = await self.click(self.button("儲存設定"))
        shown = self.screen(clicked)
        self.assertIn("### 主模型\n**gpt-b** · 推理強度：高", shown)
        self.assertIn("### 備援模型\n**停用**", shown)
        self.assertIn("已儲存全 Bot 模型設定", shown)
        self.assertNotIn("草稿尚未儲存", self.screen(await self.click(self.button("重新整理"))))

        await self.open_panel()
        await self.go("樂奈")
        shown = self.screen(await self.go("模型"))
        self.assertIn("### 主模型\n**gpt-b** · 推理強度：高", shown)
        self.assertIn("### 備援模型\n**停用**", shown)

    async def test_model_page_buttons_turn_their_own_side(self) -> None:
        self.codex.get_models.return_value = tuple(
            ModelInfo(f"gpt-{n}", f"Model {n}", "low", ("low",), ("text",)) for n in range(30)
        )
        await self.open_panel()
        await self.go("樂奈")
        await self.go("模型")
        shown = self.screen(await self.click(self.button("下一頁")))
        self.assertIn("### 主模型 · 第 2/2 頁", shown)
        self.assertIn("### 備援模型 · 第 1/2 頁", shown)
        self.assertTrue(self.button("下一頁").disabled)

    async def test_games_channel_publishes_hub_and_roles_limit_players(self) -> None:
        self.channels[800] = channel = text_channel(800, self.guild)
        channel.send = AsyncMock(return_value=MagicMock(id=801))
        await self.open_panel()
        await self.go("遊戲大廳")
        shown = self.screen(await self.choose(self.select("選擇遊戲頻道"), resolved(channel)))
        channel.send.assert_awaited_once()
        self.assertEqual((self.bot.games.channel_id, self.bot.games.hub_message), (800, [800, 801]))
        self.assertIn("✅ 已設定遊戲頻道並發布遊戲大廳。", shown)
        self.assertIn("🟢 狀態正常 · **遊戲頻道 <#800>**", shown)
        self.assertIn("**常駐訊息**　已發布", shown)

        roles = self.select("選擇可用身分組（清空即所有人可用）")
        outsider = self.role(901)
        outsider.guild = MagicMock(id=G + 1)
        self.assertIn("只能選擇目前伺服器的一般身分組。", self.screen(await self.choose(roles, outsider)))
        shown = self.screen(await self.choose(roles, self.role(900)))
        self.assertEqual(self.bot.games.role_ids, [900])
        self.assertIn("已更新遊戲大廳可用身分組，共 1 個。", shown)
        self.assertIn("**可用身分組**　<@&900>", shown)
        shown = self.screen(await self.choose(roles))
        self.assertEqual(self.bot.games.role_ids, [])
        self.assertIn("已清除可用身分組，所有人都可以使用遊戲大廳。", shown)
        self.assertIn("**可用身分組**　所有人", shown)

    async def test_effects_page_skip_and_retry_through_panel_run(self) -> None:
        operations = self.bot.effects.operations
        with transaction(self.db):
            for effect_id in ("first", "second"):
                operations.create_effect(effect_id, "action.receipt", 700, {})
        await self.open_panel()
        await self.click(self.button("待處理項目（2）"))
        picker = next(item for item in self.view.walk_children() if isinstance(item, discord.ui.Select))
        await self.choose(picker, "second")
        danger = discord.ButtonStyle.danger
        self.assertEqual((self.button("已查核未送達，重送").style, self.button("略過此次").style), (danger, danger))
        for leave in (
            lambda: self.click(self.button("取消")),
            lambda: self.choose(picker, "second"),
            lambda: self.go("返回總覽"),
        ):
            await self.click(self.button("略過此次"))
            self.assertEqual(self.button("確定要略過此次？無法復原").style, danger)
            self.assertEqual(self.button("取消").style, discord.ButtonStyle.secondary)
            with self.assertRaises(StopIteration):
                self.button("核對後標記已送達")
            await leave()
            if self.view.page != "effects":
                await self.click(self.button("待處理項目（2）"))
            self.button("略過此次")
        await self.click(self.button("略過此次"))
        self.assertEqual((operations.get_effect("first").status, operations.get_effect("second").status),
                         ("pending", "pending"))
        shown = self.screen(await self.click(self.button("確定要略過此次？無法復原")))
        self.assertEqual((operations.get_effect("first").status, operations.get_effect("second").status),
                         ("pending", "skipped"))
        self.assertIn("已處理；僅變更通知結果，不重做原本的功能操作。", shown)
        self.assertIn("**1 項待處理**", shown)

        async def retry(effect_id, *, allow_unknown, still_current):
            self.assertTrue(still_current())
            return False

        with patch.object(self.bot.actions, "retry_effect", side_effect=retry) as retried:
            await self.click(self.button("已查核未送達，重送"))
            retried.assert_not_awaited()
            shown = self.screen(await self.click(self.button("確定要重送？無法復原")))
        self.assertEqual(retried.await_args.args[0], "first")
        self.assertIn("已處理送出要求；尚未確認送達，請重新整理查看狀態。", shown)

    async def test_effects_page_shows_only_valid_actions_and_takes_a_message_link(self) -> None:
        operations = self.bot.effects.operations
        with transaction(self.db):
            for effect_id, kind in (("delete", "voice.delete"), ("open", "game_table_open"),
                                    ("failed", "action.receipt"), ("sent", "action.receipt")):
                operations.create_effect(effect_id, kind, 700, {})
            operations.update_effect("failed", "failed")
        await self.open_panel()
        await self.click(self.button("待處理項目（4）"))
        picker = next(item for item in self.view.walk_children() if isinstance(item, discord.ui.Select))
        only_skip = "這類項目不能核對或重送，請到原頻道確認並清理後按略過。"
        for effect_id, labels in (
            ("delete", ["已查核未送達，重送", "略過此次"]),
            ("open", ["略過此次"]),
            ("failed", ["已查核未送達，重送", "略過此次"]),
            ("sent", ["核對後標記已送達", "已查核未送達，重送", "略過此次"]),
        ):
            with self.subTest(effect_id=effect_id):
                shown = self.screen(await self.choose(picker, effect_id))
                bottom = self.view.children[-1].children[-2]
                self.assertEqual([button.label for button in bottom.children], [*labels, "重新整理"])
                self.assertEqual(only_skip in shown, labels == ["略過此次"])
                self.assertIn("**原頻道**　<#700>", shown)
        clicked = await self.click(self.button("核對後標記已送達"))
        modal = clicked.response.send_modal.await_args.args[0]
        modal.resource._value = f"https://discord.com/channels/{G}/700/501/"
        with patch.object(self.bot.effects, "resolve", AsyncMock(return_value=None)) as resolve:
            await modal.on_submit(self.interaction())
        self.assertEqual(resolve.await_args.args, ("sent", "confirmed"))
        self.assertEqual(resolve.await_args.kwargs["message_id"], 501)

    async def test_unusable_game_channel_needs_attention(self) -> None:
        self.bot.games.set_channel(800)
        shown = self.screen(await self.open_panel())
        self.assertIn("2 個需要處理", shown)
        self.assertIn("🟠 需要處理 · **🎲 遊戲大廳**\n找不到已綁定的遊戲頻道，請在控制台重新選擇", shown)

        shown = self.screen(await self.go("遊戲大廳"))
        self.assertIn("🟠 需要處理 · **遊戲頻道 <#800>**", shown)
        self.assertIn("**⚠️ 需要處理**\n找不到已綁定的遊戲頻道，請在控制台重新選擇。", shown)
        self.assertEqual(self.view.children[0].accent_colour, WARN_COLOUR)
        self.assertFalse(self.button("重發遊戲大廳").disabled)

    async def test_blue_marks_only_the_next_step(self) -> None:
        primary, secondary = discord.ButtonStyle.primary, discord.ButtonStyle.secondary
        self.channels[800] = text_channel(800, self.guild)
        self.bot.games.set_channel(800)
        await self.open_panel()
        await self.go("遊戲大廳")
        self.assertEqual(self.button("重發遊戲大廳").style, primary)
        put_setting(self.db, GAMES_HUB_MESSAGE, [800, 801])
        await self.click(self.button("重新整理"))
        self.assertEqual(self.button("重發遊戲大廳").style, secondary)
        self.channels.pop(800)
        await self.click(self.button("重新整理"))
        self.assertEqual(self.button("重發遊戲大廳").style, primary)

        await self.go("臨時語音")
        sync = self.button("重新同步")
        self.assertEqual((sync.style, sync.disabled), (secondary, True))
        put_setting(self.db, VOICE_ENABLED, True)
        await self.click(self.button("重新整理"))
        self.assertEqual(sync.style, primary)
        entry = MagicMock(spec=discord.VoiceChannel)
        entry.id, entry.type, entry.guild = 500, discord.ChannelType.voice, self.guild
        entry.permissions_for.return_value = discord.Permissions.all()
        self.channels[500] = entry
        put_setting(self.db, VOICE_ENTRY_CHANNEL_ID, 500)
        await self.click(self.button("重新整理"))
        self.assertEqual((sync.style, sync.disabled), (secondary, False))

    async def test_warning_note_turns_page_orange(self) -> None:
        await self.open_panel()
        self.channels[800] = text_channel(800, self.guild)
        self.bot.games.set_channel(800)
        await self.go("遊戲大廳")
        roles = self.select("選擇可用身分組（清空即所有人可用）")
        outsider = self.role(901)
        outsider.guild = MagicMock(id=G + 1)
        shown = self.screen(await self.choose(roles, outsider))
        self.assertIn("⚠️ 只能選擇目前伺服器的一般身分組。", shown)
        self.assertEqual([card.accent_colour for card in self.view.children], [WARN_COLOUR])
        await self.choose(roles, self.role(900))
        self.assertEqual([card.accent_colour for card in self.view.children], [BRAND_COLOUR])

    async def test_closed_screen_is_grey(self) -> None:
        await self.open_panel()
        self.view._render_closed()
        self.assertEqual(self.view.children[0].accent_colour, NEUTRAL_COLOUR)
        self.assertIn(f"## {ICONS['closed']} 管理控制台\n-# 已關閉", text(self.view))

    async def test_games_state_without_bot_member(self) -> None:
        self.bot.games.set_channel(800)
        self.channels[800] = text_channel(800, self.guild)
        await self.open_panel()
        self.guild.me = None
        state = games_state(self.view)
        self.assertEqual(state.label, "需要處理")
        self.assertEqual(state.summary, "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。")

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

    async def test_idle_timeout_greys_out_panel(self) -> None:
        opened = await self.open_panel()
        self.assertEqual(self.view.timeout, 600)
        await self.view.on_timeout()
        await asyncio.wait(self.view._panel_session.tasks)
        self.assertFalse(self.view.alive)
        opened.delete_original_response.assert_not_awaited()
        self.assertEqual(opened.edit_original_response.await_count, 2)
        self.assertIs(opened.edit_original_response.await_args.kwargs["view"], self.view)
        self.assertIn(f"## {ICONS['closed']} 管理控制台\n-# 閒置 10 分鐘，已自動關閉", text(self.view))
        self.assertEqual([card.accent_colour for card in self.view.children], [NEUTRAL_COLOUR])
        self.assertFalse([item for item in self.view.walk_children() if isinstance(item, (discord.ui.Button, discord.ui.Select))])

    def logged_out(self) -> asyncio.Future[bool]:
        self.codex.get_runtime_status.return_value = LOGGED_OUT
        self.codex.login_start = AsyncMock(return_value=LOGIN)
        self.codex.login_cancel = AsyncMock()
        finished = asyncio.get_running_loop().create_future()

        async def wait_login(login_id: str) -> bool:
            return await finished
        self.codex.wait_login = wait_login
        return finished

    async def test_device_login_shows_code_in_panel_then_redraws_when_done(self) -> None:
        finished = self.logged_out()
        await self.open_panel()
        await self.go("樂奈")
        self.assertIn("尚未登入。按「登入」取得裝置碼", text(self.view))
        clicked = await self.click(self.button("登入"))
        shown = self.screen(clicked)
        self.assertIn("1. 開啟 https://auth.openai.com/codex/device\n2. 輸入代碼 `ABCD-1234`", shown)
        self.button("取消登入")
        self.codex.get_runtime_status.return_value = STATUS
        finished.set_result(True)
        for _ in range(20):
            await asyncio.sleep(0)
        redrawn = clicked.edit_original_response.await_args.kwargs["view"]
        self.assertIs(redrawn, self.view)
        self.assertIn("✅ 已登入 Codex。", text(redrawn))
        self.assertIn("已登入 ChatGPT 帳號。", text(redrawn))
        self.button("登出")
        self.codex.login_cancel.assert_not_awaited()

    async def test_device_login_is_cancelled_by_button_or_closing_the_panel(self) -> None:
        self.logged_out()
        await self.open_panel()
        await self.go("樂奈")
        await self.click(self.button("登入"))
        clicked = await self.click(self.button("取消登入"))
        self.codex.login_cancel.assert_awaited_once_with("login-1")
        self.assertIn("已取消登入。", self.screen(clicked))
        self.button("登入")
        await self.click(self.button("登入"))
        await self.bot.admin_panels.close(deadline=asyncio.get_running_loop().time() + 5)
        self.assertEqual(self.codex.login_cancel.await_count, 2)

    async def test_logout_asks_for_confirmation_first(self) -> None:
        self.codex.logout = AsyncMock()
        await self.open_panel()
        await self.go("樂奈")
        shown = self.screen(await self.click(self.button("登出")))
        self.assertIn("### 確認登出 Codex", shown)
        self.codex.logout.assert_not_awaited()
        self.assertNotIn("確認登出 Codex", self.screen(await self.click(self.button("取消"))))
        await self.click(self.button("登出"))
        self.codex.get_runtime_status.return_value = LOGGED_OUT
        clicked = await self.click(self.button("確認登出"))
        self.codex.logout.assert_awaited_once()
        shown = self.screen(clicked)
        self.assertIn("✅ 已登出 Codex。", shown)
        self.assertIn("尚未登入。", shown)

    async def test_unresponsive_codex_is_named_on_ai_page_and_overview(self) -> None:
        self.codex.get_runtime_status.return_value = CodexRuntimeStatus(reason="unresponsive")
        self.bot.ai_settings.set_allowlist(G, frozenset({600}), roles=False)
        self.bot.ai_settings.set_allowlist(G, frozenset({700}), roles=True)
        self.channels[600] = text_channel(600, self.guild)
        self.role(700)
        shown = self.screen(await self.open_panel())
        self.assertIn("**🤖 樂奈**\ncodex 無回應", shown)
        shown = self.screen(await self.go("樂奈"))
        self.assertIn("codex 無回應，請手動重啟 codex 容器後再重新整理。", shown)
        with self.assertRaises(StopIteration):
            self.button("登入")

    async def test_replaced_or_shut_down_panel_is_deleted(self) -> None:
        first = await self.open_panel()
        old = self.view
        second = await self.open_panel()
        await asyncio.wait({old._retirement_task})
        first.delete_original_response.assert_awaited_once()
        first.edit_original_response.assert_awaited_once()
        await self.bot.admin_panels.close(deadline=asyncio.get_running_loop().time() + 5)
        second.delete_original_response.assert_awaited_once()
        second.edit_original_response.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
