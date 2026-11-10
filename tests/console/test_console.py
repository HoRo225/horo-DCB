import asyncio
import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from horo_dcb.console import Console
from horo_dcb.store import Store
from horo_dcb.voice import Voice
from tests.support.app_server import Reject
from tests.support.discord_fakes import Guild, http_error, member, view_text
from tests.support.waiting import until

OWNER_ONLY = "只有擁有者能使用"
NO_PLACEHOLDER = "\u200b"
MODULES = ["AI 對話", "臨時語音"]
DEVICE = {"loginId": "L1", "verificationUrl": "https://example.test/device", "userCode": "ABCD-EFGH"}
LIFETIME = timedelta(minutes=14, seconds=50)
OPENED = discord.utils.utcnow()
FOOTER = f"-# 面板在 <t:{int((OPENED + LIFETIME).timestamp())}:R>失效"
SIGNED_IN = "🟢 已登入 ChatGPT，方案：plus"
SIGNED_OUT = "🔴 尚未登入 ChatGPT"
LOGIN_STEPS = "🟡 等待登入\n1. 按下「開啟登入頁」，用機器人專用的 ChatGPT 帳號登入\n2. 輸入下面的登入代碼\n# ABCD-EFGH\n-# 完成後面板會自動更新"
GREEN, YELLOW, RED, GREY = 0x23A55A, 0xF0B232, 0xF23F43, 0x80848E


def logged_out(params: dict) -> dict:
    return {"account": None, "requiresOpenaiAuth": True}


def logged_in(params: dict) -> dict:
    return {"account": {"type": "chatgpt", "planType": "plus"}, "requiresOpenaiAuth": True}


class Interaction:
    def __init__(self, user: Mock | None = None, custom_id: str | None = None, created_at: datetime = OPENED) -> None:
        self.user = user or member()
        self.data = {"custom_id": custom_id} if custom_id is not None else {}
        self.created_at = created_at
        self.response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
        self.followup = SimpleNamespace(send=AsyncMock())
        self.deleted = False
        self.contents: list[str] = []
        self.screens: list[str] = []
        self.views: list[discord.ui.LayoutView] = []
        self.colours: list[int | None] = []
        self.edit_original_response = AsyncMock(side_effect=self.edit)
        self.delete_original_response = AsyncMock(side_effect=self.delete)

    async def edit(self, *, content: str | None = None, view: discord.ui.LayoutView | None = None) -> None:
        if content is not None:
            self.contents.append(content)
        if view is not None:
            self.views.append(view)
            self.screens.append(view_text(view))
            self.colours.append(view.to_components()[0]["accent_color"])

    async def delete(self) -> None:
        self.deleted = True

    @property
    def screen(self) -> str:
        return self.screens[-1]

    @property
    def colour(self) -> int | None:
        return self.colours[-1]


def select_payloads(view: discord.ui.LayoutView) -> list[dict]:
    def walk(payloads: list[dict]) -> list[dict]:
        found = []
        for payload in payloads:
            if payload.get("type") in (3, 6, 8):
                found.append(payload)
            found.extend(walk(payload.get("components", [])))
        return found

    return walk(view.to_components())


def selects(view: discord.ui.LayoutView) -> list[list[str]]:
    return [[option["label"] for option in payload["options"]] for payload in select_payloads(view) if payload["type"] == 3]


def chosen(view: discord.ui.LayoutView) -> list[list[str]]:
    return [[option["label"] for option in payload["options"] if option.get("default")] for payload in select_payloads(view) if payload["type"] == 3]


def placeholders(view: discord.ui.LayoutView) -> list[str | None]:
    return [payload.get("placeholder") for payload in select_payloads(view)]


def component_count(payloads: list[dict]) -> int:
    return sum(1 + component_count(payload.get("components", [])) + ("accessory" in payload) for payload in payloads)


def buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button:
    return next(item for item in buttons(view) if item.label == label)


def account_buttons(view: discord.ui.LayoutView) -> list[str]:
    return [item.label for item in buttons(view) if item.label != "編輯"]


def menu(view: discord.ui.LayoutView, kind: discord.ChannelType) -> discord.ui.ChannelSelect:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.ChannelSelect) and item.channel_types == [kind])


async def switch(interaction: Interaction, name: str) -> None:
    picker = next(item for item in interaction.views[-1].walk_children() if isinstance(item, discord.ui.Select) and [option.label for option in item.options] == MODULES)
    picker._values = [next(option.value for option in picker.options if option.label == name)]
    await picker.callback(Interaction())


@pytest.fixture
async def store(tmp_path):
    value = Store(tmp_path)
    await value.open()
    return value


@pytest.fixture
async def bot(codex, store):
    return SimpleNamespace(codex=codex, store=store, is_owner=AsyncMock(return_value=True))


@pytest.fixture
async def console(bot):
    value = Console(bot)
    value.start()
    yield value
    await value.close()


def expiring_in(seconds: float) -> datetime:
    return discord.utils.utcnow() - LIFETIME + timedelta(seconds=seconds)


async def opened(console, created_at: datetime = OPENED) -> Interaction:
    interaction = Interaction(created_at=created_at)
    await console.open(interaction)
    return interaction


async def test_a_non_owner_gets_no_panel(app_server, bot, console):
    bot.is_owner.return_value = False
    interaction = await opened(console)
    assert interaction.contents == [OWNER_ONLY]
    assert interaction.views == []
    assert app_server.requests("account/read") == []


async def test_the_owner_sees_account_quota_targets_model_and_instructions(app_server, console):
    app_server.handlers["account/rateLimits/read"] = lambda params: {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 12, "resetsAt": 1800000000}}}
    interaction = await opened(console)
    assert interaction.contents == []
    assert interaction.screen.splitlines() == [
        "## 控制台",
        SIGNED_IN,
        "5 小時額度，<t:1800000000:R>重置",
        "🟩⬛⬛⬛⬛⬛⬛⬛⬛⬛ 已用 12%",
        "-# 可使用的頻道",
        "-# 可使用的身分組",
        "-# 模型",
        "-# 思考強度",
        "-# 固定指示",
        "未設定",
        FOOTER,
    ]
    view = interaction.views[-1]
    assert selects(view) == [MODULES, ["GPT Test"], ["低", "中", "高"]]
    assert placeholders(view) == [None, NO_PLACEHOLDER, NO_PLACEHOLDER, "預設：GPT Test", "預設：中"]
    assert chosen(view) == [["AI 對話"], [], []]
    assert account_buttons(view) == ["登出", "重新整理"]
    assert interaction.colour == GREEN


async def test_quota_rows_take_two_lines_each(app_server, console):
    limits = {"primary": {"windowDurationMins": 43200, "usedPercent": 98, "resetsAt": 1800000000}, "secondary": {"windowDurationMins": None, "usedPercent": 100, "resetsAt": None}}
    app_server.handlers["account/rateLimits/read"] = lambda params: {"rateLimits": limits}
    interaction = await opened(console)
    assert f"{SIGNED_IN}\n30 天額度，<t:1800000000:R>重置\n🟥🟥🟥🟥🟥🟥🟥🟥🟥⬛ 已用 98%\n長期額度\n🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥 已用完\n-# 可使用的頻道" in interaction.screen
    assert interaction.colour == GREEN


async def test_saved_settings_show_as_chosen_options(store, console):
    await store.update_settings(model="gpt-test", effort="high", instructions="喵喵喵")
    interaction = await opened(console)
    assert chosen(interaction.views[-1]) == [["AI 對話"], ["GPT Test"], ["高"]]
    assert "-# 固定指示\n已設定 3 字，只套用到新對話\n> 喵喵喵\n" in interaction.screen


async def test_an_effort_the_model_lacks_falls_back_to_its_default(store, console):
    await store.update_settings(model="gpt-test", effort="ultra")
    interaction = await opened(console)
    view = interaction.views[-1]
    assert chosen(view) == [["AI 對話"], ["GPT Test"], []]
    assert placeholders(view)[-1] == "預設：中"


async def test_the_instructions_preview_is_trimmed_and_escaped(store, console):
    text = "\n  *粗體* @everyone  \n\n" + "長" * 45 + "\n第三行\n第四行"
    await store.update_settings(instructions=text)
    interaction = await opened(console)
    preview = f"-# 固定指示\n已設定 {len(text)} 字，只套用到新對話\n> \\*粗體\\* @​everyone\n> {'長' * 40}…\n> 第三行\n{FOOTER}"
    assert interaction.screen.endswith(preview)
    assert button(interaction.views[-1], "編輯").custom_id == "instructions"


async def test_a_logged_out_account_offers_login_and_hides_the_model(app_server, console):
    app_server.handlers["account/read"] = logged_out
    interaction = await opened(console)
    view = interaction.views[-1]
    assert interaction.screen.splitlines()[1:4] == [SIGNED_OUT, "-# 可使用的頻道", "-# 可使用的身分組"]
    assert "-# 模型" not in interaction.screen
    assert selects(view) == [MODULES]
    assert account_buttons(view) == ["登入 ChatGPT", "重新整理"]
    assert button(view, "登入 ChatGPT").style is discord.ButtonStyle.primary
    assert button(view, "重新整理").style is discord.ButtonStyle.secondary
    assert interaction.colour == RED
    assert app_server.requests("account/rateLimits/read") == []
    assert app_server.requests("model/list") == []


async def test_an_unreadable_account_is_reported(app_server, console):
    app_server.handlers["account/read"] = lambda params: Reject()
    interaction = await opened(console)
    assert interaction.screen.splitlines()[1:3] == ["⚪ 帳號狀態無法確認", "🔴 暫時無法讀取帳號狀態，請稍後重新整理。"]
    assert account_buttons(interaction.views[-1]) == ["重新整理"]
    assert interaction.colour == GREY


async def test_signed_in_without_quota_data_says_so(console):
    interaction = await opened(console)
    assert f"{SIGNED_IN}\n-# 額度資料暫時無法取得\n" in interaction.screen


async def test_no_models_means_no_model_choice(app_server, console):
    app_server.handlers["model/list"] = lambda params: {"data": [], "nextCursor": None}
    interaction = await opened(console)
    assert "-# 模型" not in interaction.screen
    assert "思考強度" not in interaction.screen
    assert selects(interaction.views[-1]) == [MODULES]


async def test_login_shows_the_device_code_and_cancel_clears_it(app_server, console):
    app_server.handlers["account/login/start"] = lambda params: DEVICE
    interaction = await opened(console)
    await console.action("login")
    assert app_server.requests("account/login/start") == [{"type": "chatgptDeviceCode"}]
    view = interaction.views[-1]
    assert f"## 控制台\n{LOGIN_STEPS}\n" in interaction.screen
    assert "https://example.test/device" not in interaction.screen
    assert account_buttons(view) == ["開啟登入頁", "取消登入", "重新整理"]
    assert button(view, "開啟登入頁").url == "https://example.test/device"
    assert button(view, "開啟登入頁").style is discord.ButtonStyle.link
    assert interaction.colour == YELLOW
    await console.action("cancel")
    assert app_server.requests("account/login/cancel") == [{"loginId": "L1"}]
    assert "ABCD-EFGH" not in interaction.screen
    assert f"{SIGNED_IN}\n-# 已取消登入。\n" in interaction.screen
    assert interaction.colour == GREEN


async def test_a_new_login_cancels_the_pending_one_first(app_server, console):
    numbers = itertools.count(1)

    def start(params: dict) -> dict:
        number = next(numbers)
        return {"loginId": f"L{number}", "verificationUrl": f"https://example.test/{number}", "userCode": f"CODE-{number}"}

    app_server.handlers["account/login/start"] = start
    interaction = await opened(console)
    await console.action("login")
    await console.action("login")
    assert app_server.requests("account/login/cancel") == [{"loginId": "L1"}]
    wire = [message["method"] for message in app_server.received if message.get("method", "").startswith("account/login/")]
    assert wire == ["account/login/start", "account/login/cancel", "account/login/start"]
    assert "# CODE-2" in interaction.screen
    assert "CODE-1" not in interaction.screen
    assert button(interaction.views[-1], "開啟登入頁").url == "https://example.test/2"


async def test_logout_reports_it(app_server, console):
    interaction = await opened(console)
    app_server.handlers["account/read"] = logged_out
    await console.action("logout")
    assert len(app_server.requests("account/logout")) == 1
    assert f"{SIGNED_OUT}\n-# 已登出。\n" in interaction.screen


async def test_logout_cancels_a_pending_login(app_server, console):
    app_server.handlers["account/login/start"] = lambda params: DEVICE
    interaction = await opened(console)
    await console.action("login")
    await console.action("logout")
    assert app_server.requests("account/login/cancel") == [{"loginId": "L1"}]
    assert "ABCD-EFGH" not in interaction.screen


async def test_a_failed_operation_is_reported(app_server, console):
    app_server.handlers["account/login/start"] = lambda params: Reject()
    interaction = await opened(console)
    await console.action("login")
    assert "登入代碼" not in interaction.screen
    assert f"{SIGNED_IN}\n🔴 操作失敗，請稍後重新整理。\n" in interaction.screen


async def test_refresh_clears_the_last_note(app_server, console):
    interaction = await opened(console)
    await console.action("logout")
    reads = len(app_server.requests("account/read"))
    await console.action("refresh")
    assert len(app_server.requests("account/read")) == reads + 1
    assert "已登出。" not in interaction.screen
    assert f"{SIGNED_IN}\n-# 額度資料暫時無法取得\n" in interaction.screen


@pytest.mark.parametrize(
    ("success", "account", "status", "note"),
    [
        (True, {"type": "chatgpt", "planType": "plus"}, SIGNED_IN, "-# 登入完成。"),
        (False, {"type": "chatgpt", "planType": "plus"}, SIGNED_IN, "🔴 登入未完成，請重新登入。"),
        (True, None, SIGNED_OUT, "🔴 登入未完成，請重新登入。"),
    ],
    ids=["completed", "failed", "still-logged-out"],
)
async def test_login_completion_updates_the_panel(app_server, console, success, account, status, note):
    app_server.handlers["account/login/start"] = lambda params: DEVICE
    interaction = await opened(console)
    await console.action("login")
    app_server.handlers["account/read"] = lambda params: {"account": account, "requiresOpenaiAuth": True}
    await app_server.notify("account/login/completed", {"loginId": "L1", "success": success, "error": None})
    await until(lambda: f"## 控制台\n{status}\n{note}\n" in interaction.screen)
    assert "ABCD-EFGH" not in interaction.screen


async def test_completion_of_another_login_is_ignored(app_server, console):
    app_server.handlers["account/login/start"] = lambda params: DEVICE
    interaction = await opened(console)
    await console.action("login")
    reads = len(app_server.requests("account/read"))
    quota_reads = len(app_server.requests("account/rateLimits/read"))
    edits = len(interaction.screens)
    await app_server.notify("account/login/completed", {"loginId": "other", "success": True, "error": None})
    await app_server.notify("account/rateLimits/updated", {})
    await until(lambda: len(app_server.requests("account/rateLimits/read")) == quota_reads + 1 and len(interaction.screens) == edits + 1)
    assert len(app_server.requests("account/read")) == reads
    assert LOGIN_STEPS in interaction.screen
    assert "登入完成。" not in interaction.screen


async def test_quota_updates_reach_an_open_panel(app_server, console):
    interaction = await opened(console)
    assert "-# 額度資料暫時無法取得" in interaction.screen
    reads = len(app_server.requests("account/rateLimits/read"))
    app_server.handlers["account/rateLimits/read"] = lambda params: {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 50, "resetsAt": 1800000000}}}
    await app_server.notify("account/rateLimits/updated", {"rateLimits": {}})
    await until(lambda: "5 小時額度，<t:1800000000:R>重置\n🟩🟩🟩🟩🟩⬛⬛⬛⬛⬛ 已用 50%" in interaction.screen)
    assert len(app_server.requests("account/rateLimits/read")) == reads + 1


async def test_quota_updates_are_not_read_without_a_panel(app_server, console):
    app_server.handlers["account/read"] = logged_out
    await app_server.notify("account/rateLimits/updated", {})
    await app_server.notify("account/updated", {})
    await until(lambda: app_server.requests("account/read"))
    assert app_server.requests("account/rateLimits/read") == []


async def test_account_changes_refresh_the_panel(app_server, console):
    interaction = await opened(console)
    app_server.handlers["account/read"] = logged_out
    await app_server.notify("account/updated", {})
    await until(lambda: SIGNED_OUT in interaction.screen)


async def test_a_panel_deletes_itself_when_it_expires(console):
    interaction = await opened(console, expiring_in(0.3))
    await until(lambda: interaction.deleted)


async def test_an_expired_panel_is_no_longer_edited(app_server, store, console):
    stale = await opened(console, expiring_in(0))
    await until(lambda: stale.deleted)
    edits = len(stale.screens)
    await store.update_settings(effort="high")
    await console.refresh_panels()
    await console.action("refresh")
    reads = len(app_server.requests("account/read"))
    await app_server.notify("account/updated", {})
    await until(lambda: len(app_server.requests("account/read")) == reads + 1)
    await switch(stale, "臨時語音")
    assert len(stale.screens) == edits


async def test_a_panel_that_has_not_expired_stays_and_refreshes(console):
    stale = await opened(console, expiring_in(0))
    live = await opened(console)
    await until(lambda: stale.deleted)
    await console.action("refresh")
    assert not live.deleted
    assert len(live.screens) == 2


async def test_shutdown_leaves_open_panels_on_screen(bot):
    console = Console(bot)
    console.start()
    interaction = await opened(console, expiring_in(0.3))
    await console.close()
    await asyncio.sleep(0.5)
    assert not interaction.deleted


async def test_a_panel_that_cannot_be_edited_is_dropped(console):
    gone = await opened(console)
    live = await opened(console)
    gone.edit_original_response.side_effect = http_error(discord.NotFound, 404)
    await console.action("refresh")
    await console.action("refresh")
    assert gone.edit_original_response.await_count == 2
    assert len(live.screens) == 3


async def test_panel_buttons_run_their_operations(app_server, console):
    app_server.handlers["account/read"] = logged_out
    app_server.handlers["account/login/start"] = lambda params: DEVICE
    interaction = await opened(console)
    panel = interaction.views[-1]
    await button(panel, "登入 ChatGPT").callback(Interaction())
    assert LOGIN_STEPS in interaction.screen
    await button(panel, "取消登入").callback(Interaction())
    assert app_server.requests("account/login/cancel") == [{"loginId": "L1"}]
    app_server.handlers["account/read"] = logged_in
    await button(panel, "重新整理").callback(Interaction())
    assert SIGNED_IN in interaction.screen
    await button(panel, "登出").callback(Interaction())
    assert len(app_server.requests("account/logout")) == 1


@pytest.mark.parametrize("custom_id", [None, "instructions"], ids=["button", "instructions"])
async def test_owners_pass_the_panel_check(console, custom_id):
    panel = (await opened(console)).views[-1]
    presser = Interaction(custom_id=custom_id)
    assert await panel.interaction_check(presser)
    presser.followup.send.assert_not_awaited()
    presser.response.send_message.assert_not_awaited()


async def test_a_non_owner_pressing_a_button_is_refused(bot, console):
    panel = (await opened(console)).views[-1]
    bot.is_owner.return_value = False
    presser = Interaction()
    assert not await panel.interaction_check(presser)
    presser.followup.send.assert_awaited_once_with(OWNER_ONLY, ephemeral=True)


async def test_a_non_owner_opening_the_instructions_is_refused(bot, console):
    panel = (await opened(console)).views[-1]
    bot.is_owner.return_value = False
    presser = Interaction(custom_id="instructions")
    assert not await panel.interaction_check(presser)
    presser.response.send_message.assert_awaited_once_with(OWNER_ONLY, ephemeral=True)


async def test_the_switch_menu_summarises_each_module(store, console):
    interaction = await opened(console)
    options = next(payload["options"] for payload in select_payloads(interaction.views[-1]) if payload["type"] == 3)
    assert [(option["label"], option.get("description")) for option in options] == [("AI 對話", "已登入 ChatGPT，方案：plus"), ("臨時語音", "未設定入口，功能停用中")]
    assert not any("emoji" in option for option in options)


async def test_switching_modules_changes_only_that_panel(console):
    first = await opened(console)
    second = await opened(console)
    untouched = len(second.screens)
    await switch(first, "臨時語音")
    assert first.screen.splitlines() == ["## 控制台", "⚪ 未設定入口，功能停用中", "-# 入口頻道", FOOTER]
    assert first.colour == GREY
    assert chosen(first.views[-1]) == [["臨時語音"]]
    assert placeholders(first.views[-1]) == [None, NO_PLACEHOLDER]
    assert len(second.screens) == untouched
    await console.action("refresh")
    assert "⚪ 未設定入口，功能停用中" in first.screen
    assert SIGNED_IN in second.screen
    await switch(first, "AI 對話")
    assert SIGNED_IN in first.screen
    assert first.colour == GREEN


async def test_a_new_console_opens_on_ai_chat(console):
    earlier = await opened(console)
    await switch(earlier, "臨時語音")
    fresh = await opened(console)
    assert chosen(fresh.views[-1])[0] == ["AI 對話"]
    assert SIGNED_IN in fresh.screen


async def test_the_voice_page_counts_its_hubs(store, console):
    await store.update_settings(voice_hub_ids=[300, 301])
    interaction = await opened(console)
    await switch(interaction, "臨時語音")
    assert interaction.screen.splitlines()[1] == "🟢 已啟用，2 個入口"
    assert interaction.screen.endswith(FOOTER)
    assert interaction.colour == GREEN


async def test_the_largest_page_fits_discord_component_limit(app_server, store, console):
    app_server.handlers["account/rateLimits/read"] = lambda params: {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 12, "resetsAt": 1}, "secondary": {"windowDurationMins": 10080, "usedPercent": 80, "resetsAt": 2}}}
    await store.update_settings(model="gpt-test", effort="high", instructions="一\n二\n三\n四")
    interaction = await opened(console)
    assert "-# 思考強度" in interaction.screen
    assert component_count(interaction.views[-1].to_components()) <= 40


async def test_the_voice_hub_menu_saves_hubs_and_seats_whoever_waits(bot, store, console):
    guild = Guild()
    hub = guild.voice_channel(300)
    guild.join(member(1, "愛麗絲"), hub)
    bot.get_channel = guild.mock.get_channel
    bot.voice = Voice(bot)
    interaction = await opened(console)
    await switch(interaction, "臨時語音")
    hubs = menu(interaction.views[-1], discord.ChannelType.voice)
    assert hubs.to_component_dict()["channel_types"] == [discord.ChannelType.voice.value]
    hubs._values = [discord.Object(300)]
    presser = Interaction()
    presser.guild = guild.mock
    await hubs.callback(presser)
    assert (await store.settings()).voice_hub_ids == {300}
    assert [int(value["id"]) for value in menu(interaction.views[-1], discord.ChannelType.voice).to_component_dict()["default_values"]] == [300]
    assert "🟢 已啟用，1 個入口" in interaction.screen
    assert [room.name for room in guild.created] == ["▍愛麗絲的房間"]
    assert guild.moves == [(1, guild.created[0].id)]


async def test_clearing_the_voice_hub_menu_stops_opening_rooms(bot, store, console):
    await store.update_settings(voice_hub_ids=[300])
    guild = Guild()
    guild.join(member(1, "愛麗絲"), guild.voice_channel(300))
    bot.get_channel = guild.mock.get_channel
    bot.voice = Voice(bot)
    interaction = await opened(console)
    await switch(interaction, "臨時語音")
    hubs = menu(interaction.views[-1], discord.ChannelType.voice)
    hubs._values = []
    presser = Interaction()
    presser.guild = guild.mock
    await hubs.callback(presser)
    assert (await store.settings()).voice_hub_ids == frozenset()
    assert "⚪ 未設定入口，功能停用中" in interaction.screen
    assert guild.created == []
