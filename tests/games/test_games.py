import asyncio
import json
import re
from itertools import product
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from horo_dcb import games as games_module
from horo_dcb import slots
from horo_dcb.blackjack import Card
from horo_dcb.games import ACTION_TEMPLATE, BetModal, Games, Lobby
from horo_dcb.store import Store
from tests.support.discord_fakes import http_error, member, view_text
from tests.support.waiting import until

PURPLE, GREEN, RED, GREY = 0x7F77DD, 0x23A55A, 0xF23F43, 0x80848E
SPACE = "　"


@dataclass
class Shot:
    view: discord.ui.LayoutView
    files: list[str]

    @property
    def text(self) -> str:
        return view_text(self.view)

    @property
    def colour(self) -> int:
        return self.view.to_components()[0]["accent_color"]

    def buttons(self) -> list[discord.ui.Button]:
        return [item for item in self.view.walk_children() if isinstance(item, discord.ui.Button)]

    def labels(self) -> list[str]:
        return [str(item.label) for item in self.buttons()]

    def button(self, label: str) -> discord.ui.Button:
        return next(item for item in self.buttons() if item.label == label)


@dataclass
class Message:
    id: int
    screens: list[Shot] = field(default_factory=list)
    deleted: bool = False

    async def edit(self, *, view: discord.ui.LayoutView, attachments: list[discord.File]) -> None:
        if self.deleted:
            raise http_error(discord.NotFound, 404)
        self.screens.append(Shot(view, [file.filename for file in attachments]))

    async def delete(self) -> None:
        if self.deleted:
            raise http_error(discord.NotFound, 404)
        self.deleted = True

    @property
    def screen(self) -> Shot:
        return self.screens[-1]


class Response:
    def __init__(self, message: Message | None) -> None:
        self.message = message
        self.done = False
        self.deferred = 0
        self.sent: list[tuple[str | None, discord.ui.LayoutView | None, bool]] = []
        self.modals: list[discord.ui.Modal] = []

    def is_done(self) -> bool:
        return self.done

    def finish(self) -> None:
        assert not self.done
        self.done = True

    async def defer(self) -> None:
        self.finish()
        self.deferred += 1

    async def edit_message(self, *, view: discord.ui.LayoutView, attachments: list[discord.File]) -> None:
        self.finish()
        assert self.message is not None
        await self.message.edit(view=view, attachments=attachments)

    async def send_message(self, content: str | None = None, *, view: discord.ui.LayoutView | None = None, ephemeral: bool = False) -> None:
        self.finish()
        self.sent.append((content, view, ephemeral))

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.finish()
        self.modals.append(modal)


class Interaction:
    def __init__(self, user: Mock, message: Message | None = None) -> None:
        self.user = user
        self.message = message
        self.guild = SimpleNamespace(me=SimpleNamespace(display_name="樂奈"))
        self.client = SimpleNamespace(user=None)
        self.response = Response(message)
        self.delete_original_response = AsyncMock()


def player(user_id: int = 1) -> Mock:
    value = member(user_id)
    value.mention = f"<@{user_id}>"
    value.display_avatar = SimpleNamespace(url=f"https://cdn.test/{user_id}.png")
    return value


def dealt(*draws: str) -> list[Card]:
    return [Card(draw[:-1], draw[-1]) for draw in reversed(draws)]


def chips(tmp_path) -> dict:
    return json.loads((tmp_path / "chips.json").read_text(encoding="utf-8"))


@pytest.fixture
async def games(tmp_path, monkeypatch):
    monkeypatch.setattr(games_module, "SPIN_HOLD", 0)
    monkeypatch.setattr(games_module, "today", lambda: "2026-11-15")
    store = Store(tmp_path)
    await store.open()
    value = Games(SimpleNamespace(store=store, add_dynamic_items=Mock()))
    yield value
    await value.close()


def rig_shoe(monkeypatch, *draws: str) -> None:
    monkeypatch.setattr(games_module, "shoe", lambda: dealt(*draws))


def rig_spin(monkeypatch, stops: tuple[int, int, int]) -> None:
    monkeypatch.setattr(slots, "spin", lambda: slots.evaluate(stops))


async def lobby(games: Games, user: Mock) -> Lobby:
    interaction = Interaction(user)
    await games.lobby(interaction)
    [(_, view, ephemeral)] = interaction.response.sent
    assert not ephemeral and isinstance(view, Lobby)
    return view


async def pick(view: Lobby, user: Mock, message: Message, game: str) -> Interaction:
    interaction = Interaction(user, message)
    view.select._values = [game]
    if await view.interaction_check(interaction):  # ty: ignore[invalid-argument-type]
        await view.select.callback(interaction)  # ty: ignore[invalid-argument-type]
    return interaction


async def open_table(games: Games, user: Mock, game: str, message_id: int = 500) -> Message:
    message = Message(message_id)
    await pick(await lobby(games, user), user, message, game)
    return message


async def press(games: Games, user: Mock, message: Message, label: str, shot: Shot | None = None) -> Interaction:
    button = (shot or message.screen).button(label)
    interaction = Interaction(user, message)
    if button.custom_id and button.custom_id.startswith("horo:game:rules:"):
        await games.explain(interaction, button.custom_id.rsplit(":", 1)[1])  # ty: ignore[invalid-argument-type]
        return interaction
    match = re.fullmatch(ACTION_TEMPLATE, button.custom_id or "")
    assert match is not None
    await games.press(interaction, match["token"], match["action"])  # ty: ignore[invalid-argument-type]
    return interaction


async def bet_custom(games: Games, user: Mock, message: Message, amount: str) -> Interaction:
    opened = await press(games, user, message, "自訂")
    [modal] = opened.response.modals
    assert isinstance(modal, BetModal)
    submitted = Interaction(user, message)
    await games.custom(submitted, modal.table, modal.token, amount)  # ty: ignore[invalid-argument-type]
    return submitted


def test_idle_slots_board_shows_no_win():
    assert slots.evaluate(games_module.IDLE_STOPS).lines == ()


async def test_lobby_shows_the_players_chips_and_both_games(games):
    view = await lobby(games, player())
    assert view_text(view) == "## 遊戲大廳\n-# 選一個遊戲，和樂奈玩一局\n-# <@1> 的籌碼 1,000"
    assert [option.label for option in view.select.options] == ["21 點", "拉霸"]


async def test_only_the_caller_can_pick_from_the_lobby(games):
    view = await lobby(games, player(1))
    message = Message(500)
    stranger = await pick(view, player(2), message, "blackjack")
    assert stranger.response.deferred == 1 and stranger.response.sent == []
    assert message.screens == []


async def test_blackjack_table_starts_with_two_backs_and_bet_buttons(games):
    message = await open_table(games, player(), "blackjack")
    assert message.screen.text == "## 21 點\n-# <@1> 的牌桌\n-# 籌碼 1,000"
    assert message.screen.files == ["table.png"]
    assert message.screen.labels() == ["下注 10", "下注 50", "下注 100", "下注 500", "自訂", "完整規則"]
    assert all(button.style == discord.ButtonStyle.secondary for button in message.screen.buttons())
    assert message.screen.colour == PURPLE


async def test_blackjack_round_deals_hides_the_hole_card_and_pays_a_win(games, tmp_path, monkeypatch):
    rig_shoe(monkeypatch, "10S", "6H", "9D", "QC", "AS")
    message = await open_table(games, player(), "blackjack")
    await press(games, player(), message, "下注 100")
    assert chips(tmp_path)["1"] == {"chips": 900, "stake": 100, "refilled": "2026-11-15"}
    assert message.screen.text == "## 21 點\n-# <@1> 的牌桌\n-# 籌碼 900 · 下注 100\n-# 樂奈 · 明牌 6 點\n-# 你 · 19 點"
    assert message.screen.files == ["dealer.png", "player.png"]
    assert message.screen.labels() == ["叫牌", "停牌"]
    await press(games, player(), message, "停牌")
    assert chips(tmp_path)["1"] == {"chips": 1100, "stake": None, "refilled": "2026-11-15"}
    assert message.screen.text == f"## 21 點\n-# <@1> 的牌桌\n-# 籌碼 1,100\n-# 樂奈 · 17 點\n-# 你 · 19 點\n🏆 - **你贏了**{SPACE}`+100`"
    assert message.screen.colour == GREEN
    assert message.screen.button("下注 100").style == discord.ButtonStyle.primary
    assert message.screen.button("下注 50").style == discord.ButtonStyle.secondary


async def test_bust_loses_the_stake_in_red(games, tmp_path, monkeypatch):
    rig_shoe(monkeypatch, "10S", "6H", "6D", "QC", "KS")
    message = await open_table(games, player(), "blackjack")
    await press(games, player(), message, "下注 50")
    await press(games, player(), message, "叫牌")
    assert chips(tmp_path)["1"]["chips"] == 950
    assert f"-# 你 · 爆牌 26 點\n💔 - **你輸了**{SPACE}`-50`" in message.screen.text
    assert message.screen.colour == RED


async def test_push_returns_the_stake_in_grey(games, tmp_path, monkeypatch):
    rig_shoe(monkeypatch, "10S", "10H", "8D", "8C")
    message = await open_table(games, player(), "blackjack")
    await press(games, player(), message, "下注 10")
    await press(games, player(), message, "停牌")
    assert chips(tmp_path)["1"]["chips"] == 1000
    assert f"🤝 - **平手**{SPACE}`±0`" in message.screen.text
    assert message.screen.colour == GREY


async def test_custom_odd_blackjack_rounds_the_bonus_down(games, tmp_path, monkeypatch):
    rig_shoe(monkeypatch, "AS", "9H", "KD", "7C")
    message = await open_table(games, player(), "blackjack")
    await bet_custom(games, player(), message, "25")
    assert chips(tmp_path)["1"] == {"chips": 1037, "stake": None, "refilled": "2026-11-15"}
    assert message.screen.text.endswith(f"-# 樂奈 · 16 點\n-# 你 · Blackjack\n✨ - **Blackjack**{SPACE}`+37`")
    assert message.screen.button("自訂").style == discord.ButtonStyle.primary


async def test_custom_prefills_the_last_bet(games, monkeypatch):
    rig_shoe(monkeypatch, "10S", "10H", "8D", "8C")
    message = await open_table(games, player(), "blackjack")
    first = await press(games, player(), message, "自訂")
    assert first.response.modals[0].amount.default is None  # ty: ignore[unresolved-attribute]
    await press(games, player(), message, "下注 50")
    await press(games, player(), message, "停牌")
    again = await press(games, player(), message, "自訂")
    assert again.response.modals[0].amount.default == "50"  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize("amount", ["9", "1001", "abc", "", "１０"])
async def test_invalid_custom_amount_only_tells_the_player(games, tmp_path, amount):
    message = await open_table(games, player(), "blackjack")
    before = list(message.screens)
    submitted = await bet_custom(games, player(), message, amount)
    assert submitted.response.sent == [("請輸入 10 到 1,000 之間的整數", None, True)]
    assert message.screens == before
    assert chips(tmp_path)["1"]["chips"] == 1000


@pytest.mark.parametrize("amount", ["10", "1000"])
async def test_custom_amount_accepts_the_bounds(games, tmp_path, monkeypatch, amount):
    rig_shoe(monkeypatch, "10S", "10H", "6D", "9C", "KS")
    message = await open_table(games, player(), "blackjack")
    await bet_custom(games, player(), message, amount)
    assert chips(tmp_path)["1"] == {"chips": 1000 - int(amount), "stake": int(amount), "refilled": "2026-11-15"}


async def test_broke_player_sees_disabled_bets_and_the_refill_note(games, monkeypatch):
    rig_shoe(monkeypatch, "10S", "10H", "6D", "9C", "KS")
    message = await open_table(games, player(), "blackjack")
    await bet_custom(games, player(), message, "995")
    await press(games, player(), message, "叫牌")
    assert "-# 籌碼 5\n" in message.screen.text
    assert message.screen.text.endswith("-# 籌碼不足，明天補回 1,000")
    assert [button.disabled for button in message.screen.buttons()] == [True, True, True, True, True, False]


async def test_someone_else_pressing_changes_nothing(games, tmp_path):
    message = await open_table(games, player(1), "blackjack")
    before = list(message.screens)
    stranger = await press(games, player(2), message, "下注 100")
    assert stranger.response.deferred == 1 and stranger.response.sent == []
    assert message.screens == before
    assert chips(tmp_path).get("2") is None
    assert chips(tmp_path)["1"]["chips"] == 1000


async def test_stale_button_only_redraws_the_current_state(games, tmp_path, monkeypatch):
    rig_shoe(monkeypatch, "10S", "6H", "9D", "QC", "AS")
    message = await open_table(games, player(), "blackjack")
    idle = message.screen
    await press(games, player(), message, "下注 100")
    current = message.screen.text
    await press(games, player(), message, "下注 100", shot=idle)
    assert message.screen.text == current
    assert chips(tmp_path)["1"] == {"chips": 900, "stake": 100, "refilled": "2026-11-15"}


async def test_unknown_table_gets_no_response(games):
    message = Message(999)
    interaction = Interaction(player(), message)
    await games.press(interaction, "0" * 32, "bet100")
    assert not interaction.response.is_done()
    assert message.screens == []


async def test_new_table_replaces_the_old_and_forfeits_its_hand(games, tmp_path, monkeypatch):
    rig_shoe(monkeypatch, "10S", "6H", "9D", "QC", "AS")
    old = await open_table(games, player(), "blackjack", 500)
    await press(games, player(), old, "下注 100")
    new = await open_table(games, player(), "slots", 501)
    assert old.deleted and not new.deleted
    assert chips(tmp_path)["1"] == {"chips": 900, "stake": None, "refilled": "2026-11-15"}
    await games.press(Interaction(player(), old), "0" * 32, "hit")
    assert chips(tmp_path)["1"]["chips"] == 900


async def test_other_players_tables_survive(games):
    first = await open_table(games, player(1), "blackjack", 500)
    await open_table(games, player(2), "blackjack", 501)
    assert not first.deleted


async def test_slots_spin_animates_then_shows_the_win(games, tmp_path, monkeypatch):
    rig_spin(monkeypatch, (17, 17, 17))
    message = await open_table(games, player(), "slots")
    assert message.screen.files == ["board.png"]
    pressed = await press(games, player(), message, "下注 100")
    assert pressed.response.deferred == 1
    spinning, settled = message.screens[-2:]
    assert spinning.files == ["spin.gif"]
    assert spinning.text == "## 拉霸\n-# <@1> 的牌桌\n-# 籌碼 900 · 下注 100\n🎰 - **轉動中**"
    assert spinning.labels() == []
    assert settled.files == ["board.png"]
    assert settled.text == f"## 拉霸\n-# <@1> 的牌桌\n-# 籌碼 9,900\n🎉 - **中獎 ×90**{SPACE}`+8,900`"
    assert settled.colour == GREEN
    assert chips(tmp_path)["1"] == {"chips": 9900, "stake": None, "refilled": "2026-11-15"}


async def test_slots_miss_and_break_even(games, tmp_path, monkeypatch):
    rig_spin(monkeypatch, games_module.IDLE_STOPS)
    message = await open_table(games, player(), "slots")
    await press(games, player(), message, "下注 10")
    assert message.screen.text.endswith(f"💨 - **沒有中獎**{SPACE}`-10`")
    assert message.screen.colour == RED
    even = next(stops for stops in product(range(len(slots.STRIP)), repeat=3) if slots.evaluate(stops).multiplier == 1)
    rig_spin(monkeypatch, even)
    await press(games, player(), message, "下注 10")
    assert message.screen.text.endswith(f"🎉 - **中獎 ×1**{SPACE}`±0`")
    assert message.screen.colour == GREY
    assert chips(tmp_path)["1"]["chips"] == 990


async def test_double_click_while_spinning_bets_once(games, tmp_path, monkeypatch):
    monkeypatch.setattr(games_module, "SPIN_HOLD", 0.2)
    rig_spin(monkeypatch, games_module.IDLE_STOPS)
    message = await open_table(games, player(), "slots")
    idle = message.screen
    first = asyncio.create_task(press(games, player(), message, "下注 100", shot=idle))
    await until(lambda: any(shot.files == ["spin.gif"] for shot in message.screens))
    second = await press(games, player(), message, "下注 100", shot=idle)
    await first
    assert second.response.deferred == 1
    assert chips(tmp_path)["1"]["chips"] == 900
    assert sum(shot.files == ["spin.gif"] for shot in message.screens) == 1


async def test_rules_card_is_private_and_lists_odds(games):
    message = await open_table(games, player(), "blackjack")
    shown = await press(games, player(), message, "完整規則")
    [(content, view, ephemeral)] = shown.response.sent
    assert ephemeral and content is None and view is not None
    assert "起手 Blackjack 拿回下注再加一倍半" in view_text(view)
    assert "低於 1,000 會補回 1,000" in view_text(view)
    slots_table = await open_table(games, player(), "slots", 501)
    shown = await press(games, player(), slots_table, "完整規則")
    assert "- 7️⃣ 七　×50" in view_text(shown.response.sent[0][1])  # ty: ignore[invalid-argument-type]


async def test_idle_tables_are_swept_and_forfeit_their_hand(games, tmp_path, monkeypatch):
    monkeypatch.setattr(games_module, "SWEEP_SECONDS", 0.01)
    monkeypatch.setattr(games_module, "IDLE_SECONDS", 0)
    rig_shoe(monkeypatch, "10S", "6H", "9D", "QC", "AS")
    message = await open_table(games, player(), "blackjack")
    await press(games, player(), message, "下注 100")
    games.start()
    await until(lambda: message.deleted)
    assert chips(tmp_path)["1"] == {"chips": 900, "stake": None, "refilled": "2026-11-15"}


async def test_lobby_refills_a_broke_player_on_a_new_day(games, tmp_path, monkeypatch):
    (tmp_path / "chips.json").write_text(json.dumps({"1": {"chips": 0, "stake": None, "refilled": "2026-11-14"}}), encoding="utf-8")
    store = Store(tmp_path)
    await store.open()
    games.bot.store = store
    view = await lobby(games, player())
    assert view_text(view).endswith("<@1> 的籌碼 1,000")
