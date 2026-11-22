import json
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import discord
import pytest

from horo_dcb import chart
from horo_dcb import stocks as stocks_module
from horo_dcb.market import Market, parse_bars
from horo_dcb.portfolio import Bar
from horo_dcb.stocks import Stocks, TradeModal
from horo_dcb.store import Store
from tests.support.discord_fakes import member, view_text
from tests.support.market_server import FIXTURES, success

UP, DOWN, GREY = 0xF24B55, 0x2DBE7E, 0x80848E
TODAY = date(2026, 11, 22)


@dataclass
class Sent:
    content: str | None
    view: discord.ui.LayoutView | None
    files: dict[str, bytes]
    ephemeral: bool

    @property
    def text(self) -> str:
        return self.content if self.view is None else view_text(self.view)

    @property
    def colour(self) -> int | None:
        assert self.view is not None
        return self.view.to_components()[0].get("accent_color")

    def buttons(self) -> list[tuple[str, discord.ButtonStyle, str]]:
        assert self.view is not None
        return [(str(item.label), item.style, str(item.custom_id)) for item in self.view.walk_children() if isinstance(item, discord.ui.Button)]

    def select(self) -> discord.ui.Select:
        assert self.view is not None
        return next(item for item in self.view.walk_children() if isinstance(item, discord.ui.Select))


def fields(modal: discord.ui.Modal) -> list[tuple[str, str | None]]:
    return [(field["label"], field.get("value")) for row in modal.to_components() for field in row["components"]]


def files_of(files: list[discord.File] | None) -> dict[str, bytes]:
    return {file.filename: file.fp.read() for file in files or []}


class Response:
    def __init__(self) -> None:
        self.done = False
        self.deferred: list[tuple[bool, bool]] = []
        self.sent: list[Sent] = []
        self.modals: list[discord.ui.Modal] = []

    def is_done(self) -> bool:
        return self.done

    def finish(self) -> None:
        assert not self.done
        self.done = True

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.finish()
        self.deferred.append((ephemeral, thinking))

    async def send_message(self, content: str | None = None, *, view: discord.ui.LayoutView | None = None, ephemeral: bool = False) -> None:
        self.finish()
        self.sent.append(Sent(content, view, {}, ephemeral))

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.finish()
        self.modals.append(modal)


@dataclass
class Followup:
    sent: list[Sent] = field(default_factory=list)

    async def send(self, content: str | None = None, *, view: discord.ui.LayoutView | None = None, files: list[discord.File] | None = None, ephemeral: bool = False) -> None:
        self.sent.append(Sent(content, view, files_of(files), ephemeral))


class Interaction:
    def __init__(self, user_id: int = 1, custom_id: str | None = None, values: list[str] | None = None) -> None:
        self.user = member(user_id)
        self.type = discord.InteractionType.application_command if custom_id is None else discord.InteractionType.component
        self.data = {} if custom_id is None else {"custom_id": custom_id} | ({} if values is None else {"values": values})
        self.response = Response()
        self.followup = Followup()
        self.edits: list[Sent] = []

    async def edit_original_response(self, *, content: str | None = None, view: discord.ui.LayoutView | None = None, attachments: list[discord.File] | None = None) -> None:
        assert self.response.done
        self.edits.append(Sent(content, view, files_of(attachments), False))

    @property
    def replies(self) -> list[Sent]:
        return self.response.sent + self.followup.sent

    @property
    def reply(self) -> Sent:
        [only] = self.replies + self.edits
        return only


def history() -> list[Bar]:
    return parse_bars(json.loads((FIXTURES / "price_2330.json").read_text(encoding="utf-8"))["data"])


def stored(tmp_path, name: str):
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


@pytest.fixture
async def stocks(tmp_path, market_server, monkeypatch):
    monkeypatch.setattr(stocks_module, "today", lambda: TODAY)
    store = Store(tmp_path)
    await store.open()
    value = Stocks(SimpleNamespace(store=store), Market(quote_url=market_server.url("/quote"), finmind_url=market_server.url("/finmind")))
    yield value
    await value.close()


async def command(stocks: Stocks, stock: str | None, user_id: int = 1) -> Interaction:
    interaction = Interaction(user_id)
    await stocks.command(interaction, stock)
    return interaction


async def press(stocks: Stocks, custom_id: str, user_id: int = 1, values: list[str] | None = None) -> Interaction:
    interaction = Interaction(user_id, custom_id, values)
    await stocks.route(interaction)
    return interaction


async def submit(stocks: Stocks, custom_id: str, shares: str, user_id: int = 1) -> Interaction:
    opened = await press(stocks, custom_id, user_id)
    [modal] = opened.response.modals
    assert isinstance(modal, TradeModal)
    modal.shares._value = shares
    interaction = Interaction(user_id)
    await modal.on_submit(interaction)
    return interaction


def info(*rows: tuple[str, str]) -> dict:
    return success([{"industry_category": "半導體業", "stock_id": code, "stock_name": name, "type": "twse", "date": "2026-11-22"} for code, name in rows])


async def test_choices_list_code_prefixes_before_name_matches(stocks, market_server):
    market_server.bodies["TaiwanStockInfo", None] = info(("2330", "台積電"), ("1234", "新台積電"), ("6488", "環球晶"), ("2303", "聯電"), ("9999", "台積電子"))
    assert [(choice.name, choice.value) for choice in await stocks.choices("23")] == [("2303 聯電", "2303"), ("2330 台積電", "2330")]
    assert [choice.value for choice in await stocks.choices("台積電")] == ["2330", "9999", "1234"]
    assert [choice.value for choice in await stocks.choices("00400a")] == []


async def test_choices_stop_at_twenty_five(stocks, market_server):
    market_server.bodies["TaiwanStockInfo", None] = info(*((f"{1100 + number}", f"股{number}") for number in range(30)), ("2330", "台積11號"))
    choices = await stocks.choices("11")
    assert [choice.value for choice in choices] == [f"{1100 + number}" for number in range(25)]


async def test_choices_are_empty_without_input_or_listings(stocks, market_server):
    assert await stocks.choices("  ") == []
    assert market_server.finmind_requests == []
    market_server.statuses["TaiwanStockInfo"] = 500
    assert await stocks.choices("2330") == []


@pytest.mark.parametrize("stock", ["2330", "台積電", " 2330 "])
async def test_command_posts_a_public_quote_card(stocks, stock):
    interaction = await command(stocks, stock)
    assert interaction.response.deferred == [(False, True)]
    card = interaction.reply
    assert not card.ephemeral
    assert card.text == "## 2330 台積電\n**2,000** ▼ 100（-4.76%）\n-# 開 2,035 · 高 2,060 · 低 1,970 · 量 75,207 張\n-# 報價來源 證交所 · 11/20 收盤"
    assert card.colour == DOWN
    assert list(card.files) == ["chart.png"]
    assert card.view is not None and card.view.to_components()[0]["components"][1]["items"][0]["media"]["url"] == "attachment://chart.png"
    assert card.buttons() == [
        ("近 1 個月", discord.ButtonStyle.secondary, "horo:stock:span:2330:1m"),
        ("近 3 個月", discord.ButtonStyle.primary, "horo:stock:span:2330:3m"),
        ("近 1 年", discord.ButtonStyle.secondary, "horo:stock:span:2330:1y"),
        ("買進", discord.ButtonStyle.success, "horo:stock:buy:2330"),
        ("賣出", discord.ButtonStyle.danger, "horo:stock:sell:2330"),
        ("我的持股", discord.ButtonStyle.secondary, "horo:stock:mine"),
    ]


@pytest.mark.parametrize("stock", ["台積", "2330 台積電", "2888"])
async def test_unknown_stock_is_told_only_to_the_asker(stocks, stock):
    interaction = await command(stocks, stock)
    assert interaction.response.deferred == []
    assert (interaction.reply.content, interaction.reply.ephemeral) == (f"找不到股票 {stock}", True)


@pytest.mark.parametrize(
    ("code", "changes", "line", "colour"),
    [
        ("00400A", {}, "**9.8** ▼ 0.76（-7.20%）", DOWN),
        ("6488", {}, "**441.5** ▼ 48.5（-9.90%）", DOWN),
        ("2330", {"y": "1990.0000"}, "**2,000** ▲ 10（+0.50%）", UP),
        ("2330", {"y": "2000.0000"}, "**2,000** ±0（0.00%）", GREY),
    ],
)
async def test_price_line_formats_and_colours_the_change(stocks, market_server, code, changes, line, colour):
    board = "otc" if code == "6488" else "tse"
    market_server.rows[f"{board}_{code}"] |= changes
    card = (await command(stocks, code)).reply
    assert card.text.split("\n")[1] == line
    assert card.colour == colour


async def test_open_market_shows_the_quote_time(stocks, market_server, monkeypatch):
    monkeypatch.setattr(stocks_module, "today", lambda: date(2026, 11, 20))
    market_server.rows["tse_2330"] |= {"t": "10:15:03"}
    assert (await command(stocks, "2330")).reply.text.endswith("-# 報價來源 證交所 · 10:15:03")


async def test_closing_time_counts_as_closed(stocks, market_server, monkeypatch):
    monkeypatch.setattr(stocks_module, "today", lambda: date(2026, 11, 20))
    assert (await command(stocks, "2330")).reply.text.endswith("-# 報價來源 證交所 · 11/20 收盤")


async def test_missing_open_high_low_are_left_out(stocks, market_server):
    market_server.rows["tse_2330"] |= {"o": "-", "h": "-", "l": "-"}
    assert (await command(stocks, "2330")).reply.text.split("\n")[2] == "-# 量 75,207 張"


async def test_stock_without_any_bars_has_no_chart(stocks, market_server):
    market_server.rows["otc_6488"] |= {"o": "-", "h": "-", "l": "-"}
    card = (await command(stocks, "6488")).reply
    assert "-# 這段期間沒有交易資料" in card.text
    assert card.files == {}


async def test_a_newer_quote_adds_todays_bar(stocks, market_server, monkeypatch):
    monkeypatch.setattr(stocks_module, "today", lambda: date(2026, 11, 23))
    market_server.rows["tse_2330"] |= {"d": "20261123", "t": "10:00:00"}
    card = (await command(stocks, "2330")).reply
    assert card.files["chart.png"] == chart.render([*history(), Bar(date(2026, 11, 23), 2035.0, 2060.0, 1970.0, 2000.0, 75_207_000)], "3m")


async def test_a_quote_from_the_last_bar_day_adds_nothing(stocks):
    card = (await command(stocks, "2330")).reply
    assert card.files["chart.png"] == chart.render(history(), "3m")


async def test_quote_failure_after_deferring_replaces_the_thinking_message(stocks, market_server):
    market_server.statuses["quote"] = 503
    interaction = await command(stocks, "2330")
    assert interaction.followup.sent == []
    assert interaction.reply.content == "暫時抓不到 2330 台積電 的報價，請稍後再試"


async def test_listing_failure_is_told_only_to_the_asker(stocks, market_server):
    market_server.statuses["TaiwanStockInfo"] = 500
    reply = (await command(stocks, "2330")).reply
    assert (reply.content, reply.ephemeral) == ("暫時抓不到股票清單，請稍後再試", True)


async def test_span_buttons_redraw_the_same_message(stocks):
    interaction = await press(stocks, "horo:stock:span:2330:1y", user_id=5)
    assert interaction.response.deferred == [(False, False)]
    [edit] = interaction.edits
    assert [(label, style) for label, style, _ in edit.buttons()[:3]] == [("近 1 個月", discord.ButtonStyle.secondary), ("近 3 個月", discord.ButtonStyle.secondary), ("近 1 年", discord.ButtonStyle.primary)]
    assert edit.files["chart.png"] == chart.render(history(), "1y")
    assert interaction.followup.sent == []


async def test_span_failure_leaves_the_card_and_tells_the_presser(stocks, market_server):
    market_server.statuses["TaiwanStockPrice"] = 500
    interaction = await press(stocks, "horo:stock:span:2330:1m")
    assert interaction.edits == []
    [error] = interaction.followup.sent
    assert (error.content, error.ephemeral) == ("暫時抓不到 2330 台積電 的報價，請稍後再試", True)


async def test_buying_asks_for_shares_and_charges_the_presser(stocks, tmp_path):
    opened = await press(stocks, "horo:stock:buy:00400A", user_id=5)
    [modal] = opened.response.modals
    assert modal.title == "買進 00400A 主動國泰動能高息"
    assert fields(modal) == [("股數", None)]
    interaction = await submit(stocks, "horo:stock:buy:00400A", "3", user_id=5)
    assert interaction.response.deferred == [(True, True)]
    assert (interaction.reply.content, interaction.reply.ephemeral) == ("買進 00400A 主動國泰動能高息 3 股，成交價 9.8，花費 30，餘額 970", True)
    assert stored(tmp_path, "stocks.json") == {"5": {"00400A": {"shares": 3, "cost": 30, "settled": "2026-11-22"}}}
    assert stored(tmp_path, "chips.json")["5"]["chips"] == 970


async def test_buying_more_than_the_chips_cover_says_how_many_fit(stocks, tmp_path):
    interaction = await submit(stocks, "horo:stock:buy:00400A", "200")
    assert interaction.reply.content == "籌碼不足，最多可以買 102 股"
    assert stored(tmp_path, "stocks.json") == {}


@pytest.mark.parametrize("shares", ["0", "-1", "abc", "1.5", "３"])
async def test_shares_must_be_a_positive_whole_number(stocks, shares, tmp_path):
    interaction = await submit(stocks, "horo:stock:buy:00400A", shares)
    assert interaction.response.deferred == []
    assert (interaction.reply.content, interaction.reply.ephemeral) == ("股數要填正整數", True)
    assert stored(tmp_path, "stocks.json") == {}


async def test_trade_without_a_quote_does_nothing(stocks, market_server, tmp_path):
    market_server.statuses["quote"] = 503
    interaction = await submit(stocks, "horo:stock:buy:00400A", "3")
    assert interaction.reply.content == "暫時抓不到 00400A 主動國泰動能高息 的報價，沒有成交"
    assert stored(tmp_path, "stocks.json") == {}


async def test_selling_without_shares_skips_the_form(stocks):
    interaction = await press(stocks, "horo:stock:sell:2330")
    assert interaction.response.modals == []
    assert (interaction.reply.content, interaction.reply.ephemeral) == ("你沒有持有 2330 台積電", True)


async def test_selling_prefills_every_share_and_pays_back(stocks, tmp_path):
    await submit(stocks, "horo:stock:buy:00400A", "3")
    opened = await press(stocks, "horo:stock:sell:00400A")
    [modal] = opened.response.modals
    assert modal.title == "賣出 00400A 主動國泰動能高息"
    assert fields(modal) == [("股數", "3")]
    over = await submit(stocks, "horo:stock:sell:00400A", "4")
    assert over.reply.content == "超過持股，你目前有 3 股"
    interaction = await submit(stocks, "horo:stock:sell:00400A", "3")
    assert interaction.reply.content == "賣出 00400A 主動國泰動能高息 3 股，成交價 9.8，拿回 29，餘額 999"
    assert stored(tmp_path, "stocks.json") == {}
    assert stored(tmp_path, "chips.json")["1"]["chips"] == 999


async def test_trading_settles_dividends_first(stocks, tmp_path):
    (tmp_path / "stocks.json").write_text(json.dumps({"1": {"00878": {"shares": 1000, "cost": 30000, "settled": "2026-08-01"}}}), encoding="utf-8")
    await stocks.bot.store.open()
    interaction = await submit(stocks, "horo:stock:sell:00878", "1000")
    assert interaction.reply.content == "暫時抓不到 00878 國泰永續高股息 的報價，沒有成交\n除權息與分割入帳 +2,260"
    assert stored(tmp_path, "stocks.json") == {"1": {"00878": {"shares": 1000, "cost": 30000, "settled": "2026-11-21"}}}
    assert stored(tmp_path, "chips.json")["1"]["chips"] == 3260


async def test_trading_a_held_stock_waits_until_dividends_can_be_settled(stocks, tmp_path, market_server):
    (tmp_path / "stocks.json").write_text(json.dumps({"1": {"00400A": {"shares": 3, "cost": 30, "settled": "2026-08-01"}}}), encoding="utf-8")
    await stocks.bot.store.open()
    market_server.statuses["TaiwanStockDividendResult"] = 500
    interaction = await submit(stocks, "horo:stock:sell:00400A", "3")
    assert (interaction.reply.content, interaction.reply.ephemeral) == ("暫時無法結算 00400A 主動國泰動能高息 的除權息，沒有成交，請稍後再試", True)
    assert stored(tmp_path, "stocks.json") == {"1": {"00400A": {"shares": 3, "cost": 30, "settled": "2026-08-01"}}}
    bought = await submit(stocks, "horo:stock:buy:2330", "1")
    assert bought.reply.content.startswith("籌碼不足")


async def test_holdings_page_is_private_and_lists_each_stock(stocks, tmp_path):
    await submit(stocks, "horo:stock:buy:00400A", "3")
    (tmp_path / "stocks.json").write_text(json.dumps({"1": {"00400A": {"shares": 3, "cost": 30, "settled": "2026-11-22"}, "00878": {"shares": 1000, "cost": 30000, "settled": "2026-08-01"}, "2888": {"shares": 5, "cost": 100, "settled": "2026-11-22"}}}), encoding="utf-8")
    await stocks.bot.store.open()
    interaction = await command(stocks, None)
    assert interaction.response.deferred == [(True, True)]
    page = interaction.reply
    assert page.ephemeral
    assert page.text == (
        "## 我的持股\n"
        "**00400A 主動國泰動能高息**\n-# 3 股 · 均價 10 · 現價 9.8 · 損益 -1（-3.33%）\n"
        "**00878 國泰永續高股息**\n-# 1,000 股 · 均價 30 · 查無報價\n"
        "**2888**\n-# 5 股 · 均價 20 · 查無報價\n"
        "持股市值 29 · 籌碼 3,230 · 合計 3,259\n"
        "除權息與分割入帳 +2,260"
    )
    assert page.colour == DOWN
    select = page.select()
    assert select.custom_id == "horo:stock:open"
    assert [(option.label, option.value) for option in select.options] == [("00400A 主動國泰動能高息", "00400A"), ("00878 國泰永續高股息", "00878")]


async def test_empty_holdings_page_explains_how_to_start(stocks):
    page = (await command(stocks, None)).reply
    assert page.text == "## 我的持股\n還沒有持股，用 /股市 加上股票代號查價後買進"
    assert not [item for item in page.view.walk_children() if isinstance(item, discord.ui.Select)]


async def test_my_holdings_button_shows_the_presser_their_own(stocks):
    await submit(stocks, "horo:stock:buy:00400A", "3", user_id=1)
    interaction = await press(stocks, "horo:stock:mine", user_id=2)
    assert interaction.reply.ephemeral
    assert "還沒有持股" in interaction.reply.text


async def test_holdings_menu_posts_a_public_card(stocks):
    interaction = await press(stocks, "horo:stock:open", values=["2330"])
    assert interaction.response.deferred == [(False, True)]
    assert not interaction.reply.ephemeral
    assert interaction.reply.text.startswith("## 2330 台積電")


@pytest.mark.parametrize("custom_id", ["horo:game:rules:slots", "horo:stock:span:2330:5y", "horo:stock:buy:2330A", "horo:stock:other"])
async def test_other_components_are_ignored(stocks, custom_id):
    interaction = await press(stocks, custom_id)
    assert not interaction.response.done
    assert interaction.followup.sent == []
