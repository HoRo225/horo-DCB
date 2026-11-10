import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from horo_dcb.console import Console
from horo_dcb.store import Store
from tests.support.app_server import Reject
from tests.support.discord_fakes import http_error, member, view_text
from tests.support.waiting import until

OWNER_ONLY = "只有擁有者能使用"
DEVICE = {"loginId": "L1", "verificationUrl": "https://example.test/device", "userCode": "ABCD-EFGH"}
LOGIN_BLOCK = "請用機器人專用的 ChatGPT 帳號開啟 https://example.test/device\n登入代碼：`ABCD-EFGH`"


class Interaction:
    def __init__(self, user: Mock | None = None, custom_id: str | None = None) -> None:
        self.user = user or member()
        self.data = {"custom_id": custom_id} if custom_id is not None else {}
        self.response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
        self.followup = SimpleNamespace(send=AsyncMock())
        self.expired = False
        self.is_expired = Mock(side_effect=lambda: self.expired)
        self.contents: list[str] = []
        self.screens: list[str] = []
        self.views: list[discord.ui.LayoutView] = []
        self.edit_original_response = AsyncMock(side_effect=self.edit)

    async def edit(self, *, content: str | None = None, view: discord.ui.LayoutView | None = None) -> None:
        if content is not None:
            self.contents.append(content)
        if view is not None:
            self.views.append(view)
            self.screens.append(view_text(view))

    @property
    def screen(self) -> str:
        return self.screens[-1]


def selects(view: discord.ui.LayoutView) -> list[list[str]]:
    def walk(payloads: list[dict]) -> list[list[str]]:
        found = []
        for payload in payloads:
            if payload.get("type") == 3:
                found.append([option["label"] for option in payload["options"]])
            found.extend(walk(payload.get("components", [])))
        return found

    return walk(view.to_components())


def button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


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


async def opened(console) -> Interaction:
    interaction = Interaction()
    await console.open(interaction)
    return interaction


async def test_a_non_owner_gets_no_panel(app_server, bot, console):
    bot.is_owner.return_value = False
    interaction = await opened(console)
    assert interaction.contents == [OWNER_ONLY]
    assert interaction.views == []
    assert app_server.requests("account/read") == []


async def test_the_owner_sees_account_quota_model_and_instructions(app_server, console):
    app_server.handlers["account/rateLimits/read"] = lambda params: {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 12, "resetsAt": 1800000000}}}
    interaction = await opened(console)
    assert interaction.contents == []
    lines = interaction.screen.splitlines()
    assert lines == [
        "## 控制台",
        "已登入 ChatGPT，方案：plus",
        "5 小時額度：已用 12%，重置時間 <t:1800000000:R>",
        "長期額度：無資料",
        "模型：GPT Test（預設）",
        "思考強度：中（預設）",
        "指示：未設定",
    ]
    assert selects(interaction.views[-1]) == [["GPT Test"], ["低", "中", "高"]]


async def test_saved_settings_drop_the_default_marks(store, console):
    await store.update_settings(model="gpt-test", effort="high", instructions="喵喵喵")
    interaction = await opened(console)
    assert "模型：GPT Test\n思考強度：高\n指示：已設定 3 字，只套用到新對話" in interaction.screen


async def test_an_effort_the_model_lacks_falls_back_to_its_default(store, console):
    await store.update_settings(model="gpt-test", effort="ultra")
    interaction = await opened(console)
    assert "模型：GPT Test\n思考強度：中（預設）" in interaction.screen


async def test_a_logged_out_account_says_so(app_server, console):
    app_server.handlers["account/read"] = lambda params: {"account": None, "requiresOpenaiAuth": True}
    interaction = await opened(console)
    assert "尚未登入 ChatGPT" in interaction.screen
    assert "模型：無資料" in interaction.screen
    assert app_server.requests("account/rateLimits/read") == []
    assert app_server.requests("model/list") == []


async def test_an_unreadable_account_is_reported(app_server, console):
    app_server.handlers["account/read"] = lambda params: Reject()
    interaction = await opened(console)
    assert "帳號狀態暫時無法確認" in interaction.screen
    assert interaction.screen.endswith("\n\n暫時無法讀取帳號狀態，請稍後重新整理。")


async def test_no_models_means_no_model_choice(app_server, console):
    app_server.handlers["model/list"] = lambda params: {"data": [], "nextCursor": None}
    interaction = await opened(console)
    assert "模型：無資料" in interaction.screen
    assert "思考強度" not in interaction.screen
    assert selects(interaction.views[-1]) == []


async def test_login_shows_the_device_code_and_cancel_clears_it(app_server, console):
    app_server.handlers["account/login/start"] = lambda params: DEVICE
    interaction = await opened(console)
    await console.action("login")
    assert app_server.requests("account/login/start") == [{"type": "chatgptDeviceCode"}]
    assert LOGIN_BLOCK in interaction.screen
    await console.action("cancel")
    assert app_server.requests("account/login/cancel") == [{"loginId": "L1"}]
    assert "ABCD-EFGH" not in interaction.screen
    assert "https://example.test/device" not in interaction.screen
    assert interaction.screen.endswith("\n\n已取消登入。")


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
    assert "CODE-2" in interaction.screen
    assert "CODE-1" not in interaction.screen


async def test_logout_reports_it(app_server, console):
    interaction = await opened(console)
    app_server.handlers["account/read"] = lambda params: {"account": None, "requiresOpenaiAuth": True}
    await console.action("logout")
    assert len(app_server.requests("account/logout")) == 1
    assert "尚未登入 ChatGPT" in interaction.screen
    assert interaction.screen.endswith("\n\n已登出。")


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
    assert interaction.screen.endswith("\n\n操作失敗，請稍後重新整理。")


async def test_refresh_clears_the_last_note(app_server, console):
    interaction = await opened(console)
    await console.action("logout")
    reads = len(app_server.requests("account/read"))
    await console.action("refresh")
    assert len(app_server.requests("account/read")) == reads + 1
    assert interaction.screen.endswith("指示：未設定")


@pytest.mark.parametrize(
    ("success", "account", "note"),
    [
        (True, {"type": "chatgpt", "planType": "plus"}, "登入完成。"),
        (False, {"type": "chatgpt", "planType": "plus"}, "登入未完成，請重新登入。"),
        (True, None, "登入未完成，請重新登入。"),
    ],
    ids=["completed", "failed", "still-logged-out"],
)
async def test_login_completion_updates_the_panel(app_server, console, success, account, note):
    app_server.handlers["account/login/start"] = lambda params: DEVICE
    interaction = await opened(console)
    await console.action("login")
    app_server.handlers["account/read"] = lambda params: {"account": account, "requiresOpenaiAuth": True}
    await app_server.notify("account/login/completed", {"loginId": "L1", "success": success, "error": None})
    await until(lambda: interaction.screen.endswith(f"\n\n{note}"))
    assert "登入代碼" not in interaction.screen


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
    assert LOGIN_BLOCK in interaction.screen
    assert "登入完成。" not in interaction.screen


async def test_quota_updates_reach_an_open_panel(app_server, console):
    interaction = await opened(console)
    assert "短期額度：無資料" in interaction.screen
    reads = len(app_server.requests("account/rateLimits/read"))
    app_server.handlers["account/rateLimits/read"] = lambda params: {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 50, "resetsAt": 1800000000}}}
    await app_server.notify("account/rateLimits/updated", {"rateLimits": {}})
    await until(lambda: "5 小時額度：已用 50%" in interaction.screen)
    assert len(app_server.requests("account/rateLimits/read")) == reads + 1


async def test_quota_updates_are_not_read_without_a_panel(app_server, console):
    app_server.handlers["account/read"] = lambda params: {"account": None, "requiresOpenaiAuth": True}
    await app_server.notify("account/rateLimits/updated", {})
    await app_server.notify("account/updated", {})
    await until(lambda: app_server.requests("account/read"))
    assert app_server.requests("account/rateLimits/read") == []


async def test_account_changes_refresh_the_panel(app_server, console):
    interaction = await opened(console)
    app_server.handlers["account/read"] = lambda params: {"account": None, "requiresOpenaiAuth": True}
    await app_server.notify("account/updated", {})
    await until(lambda: "尚未登入 ChatGPT" in interaction.screen)


async def test_an_expired_panel_is_dropped(console):
    stale = await opened(console)
    live = await opened(console)
    stale.expired = True
    await console.action("refresh")
    stale_edits = len(stale.screens)
    stale.expired = False
    await console.action("refresh")
    assert len(stale.screens) == stale_edits
    assert len(live.screens) == 3


async def test_a_panel_that_cannot_be_edited_is_dropped(console):
    gone = await opened(console)
    live = await opened(console)
    gone.edit_original_response.side_effect = http_error(discord.NotFound, 404)
    await console.action("refresh")
    await console.action("refresh")
    assert gone.edit_original_response.await_count == 2
    assert len(live.screens) == 3


async def test_panel_buttons_run_their_operations(app_server, console):
    app_server.handlers["account/login/start"] = lambda params: DEVICE
    interaction = await opened(console)
    panel = interaction.views[-1]
    await button(panel, "登入 ChatGPT").callback(Interaction())
    assert LOGIN_BLOCK in interaction.screen
    await button(panel, "取消登入").callback(Interaction())
    assert app_server.requests("account/login/cancel") == [{"loginId": "L1"}]
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
