import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ERROR_COLOUR, GOLD_COLOUR, LOSS_COLOUR, NEUTRAL_COLOUR
from src.games.adapters.discord import tables
from src.games.adapters.discord.tables import SlotsView
from src.games.app.tables import LiveTable, Posted
from src.games.domain.blackjack import Round
from src.games.domain import slots
from src.games.domain.catalog import GAMES
from src.games.domain.slots import evaluate
from src.games.domain.table import Blackjack, Slots
from tests.support import testing
from tests.support.games import deck

AVATAR = "https://cdn.example.com/a.png"
HOST = "睦_頭"
BLACKJACK_BRIEF = "玩法　叫牌或停牌，比睦\\_頭更接近 21 點就贏"


def table(state, **fields) -> LiveTable:
    return LiveTable(1, state, "a" * 32, Posted(10, 20), **fields)


def seat_view(seated: LiveTable) -> tables.SeatView:
    return tables.seat_view(seated, HOST)


def labels(view: discord.ui.LayoutView) -> list[str]:
    return [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def thumbnails(view: discord.ui.LayoutView) -> list[str]:
    return [item.media.url for item in view.walk_children() if isinstance(item, discord.ui.Thumbnail)]


def colour(view: discord.ui.LayoutView) -> discord.Colour:
    return view.children[0].accent_colour


def layout(view: discord.ui.LayoutView) -> list[type]:
    return [type(item) for item in view.children[0].children[1:]]


def texts(view: discord.ui.LayoutView) -> list[str]:
    return [item.content for item in view.children[0].children[1:] if isinstance(item, discord.ui.TextDisplay)]


SETTLED = [discord.ui.Separator, discord.ui.TextDisplay, discord.ui.Separator, discord.ui.TextDisplay]


class BlackjackViewTests(unittest.TestCase):
    def test_new_table_shows_the_brief_and_a_start_button(self) -> None:
        view = seat_view(table(Blackjack()))
        self.assertEqual(colour(view), BRAND_COLOUR)
        self.assertIn(BLACKJACK_BRIEF, testing.text(view))
        self.assertNotIn("{name}", testing.text(view))
        self.assertIn("比 HoRo 更接近", testing.text(tables.seat_view(table(Blackjack()), "HoRo")))
        self.assertIn("## 🃏 21 點\n-# <@1>", testing.text(view))
        self.assertEqual(labels(view), ["開局", "完整規則", "離桌"])

    def test_round_offers_only_hit_and_stand_and_hides_the_hole_card(self) -> None:
        view = seat_view(table(Blackjack(Round(deck("10", "9", "8", "8")))))
        self.assertEqual(labels(view), ["叫牌", "停牌"])
        self.assertEqual(texts(view)[0], "**睦\\_頭** · 明牌 **9** 點\n## `9♠` 🂠\n\n**你** · **18** 點\n## `10♠` `8♠`")
        self.assertNotIn(BLACKJACK_BRIEF, testing.text(view))
        self.assertEqual(layout(view), [discord.ui.Separator, discord.ui.TextDisplay])

    def test_seats_name_the_host_and_bold_soft_bust_and_blackjack_points(self) -> None:
        soft = seat_view(table(Blackjack(Round(deck("A", "9", "6", "8")))))
        self.assertIn("**你** · 軟 **17** 點", testing.text(soft))
        busted = Blackjack(Round(deck("10", "7", "6", "10", "K"))).play("hit", None)
        self.assertIn("**你** · **26** 點 · 爆牌", testing.text(seat_view(table(busted))))
        dealt = seat_view(table(Blackjack(Round(deck("9", "K", "9", "A")))))
        self.assertIn("**睦\\_頭** · **Blackjack**\n## `K♠` `A♠`", testing.text(dealt))
        self.assertIn("**HoRo** · 明牌 **9** 點", testing.text(tables.seat_view(table(Blackjack(Round(deck("10", "9", "8", "8")))), "HoRo")))

    def test_missing_avatars_leave_plain_text(self) -> None:
        self.assertEqual(thumbnails(seat_view(table(Blackjack(Round(deck("10", "10", "8", "8")))))), [])

    def test_finished_rounds_settle_with_a_result_and_its_reason(self) -> None:
        stand = lambda *ranks: Blackjack(Round(deck(*ranks))).play("stand", None)
        for state, settlement, tone in (
            (Blackjack(Round(deck("A", "9", "K", "7"))), "### ✨ Blackjack！\n起手 21 點", GOLD_COLOUR),
            (Blackjack(Round(deck("A", "A", "K", "K"))), "### 🤝 平手\n雙方都是 Blackjack", NEUTRAL_COLOUR),
            (Blackjack(Round(deck("9", "K", "9", "A"))), "### 💔 你輸了\n睦\\_頭起手 21 點", LOSS_COLOUR),
            (Blackjack(Round(deck("10", "7", "6", "10", "K"))).play("hit", None), "### 💔 你輸了\n你爆牌", LOSS_COLOUR),
            (stand("10", "10", "8", "6", "K"), "### 🏆 你贏了\n睦\\_頭爆牌", GOLD_COLOUR),
            (stand("10", "10", "10", "8"), "### 🏆 你贏了\n20 點對 18 點", GOLD_COLOUR),
            (stand("10", "10", "8", "8"), "### 🤝 平手\n雙方都是 18 點", NEUTRAL_COLOUR),
            (stand("10", "9", "8", "7", "5"), "### 💔 你輸了\n18 點對 21 點", LOSS_COLOUR),
        ):
            with self.subTest(settlement):
                view = seat_view(table(state))
                self.assertEqual(texts(view)[-1], settlement)
                self.assertEqual(layout(view), SETTLED)
                self.assertEqual(colour(view), tone)
                self.assertNotEqual(colour(view), ERROR_COLOUR)
                self.assertEqual(labels(view), ["再來一局", "完整規則", "離桌"])

    def test_dealer_bust_reason_spaces_a_latin_host_name(self) -> None:
        state = Blackjack(Round(deck("10", "10", "8", "6", "K"))).play("stand", None)
        self.assertEqual(texts(tables.seat_view(table(state), "HoRo"))[-1], "### 🏆 你贏了\nHoRo 爆牌")


class SlotsViewTests(unittest.TestCase):
    def test_new_table_shows_the_brief_over_an_unknown_board(self) -> None:
        view = seat_view(table(Slots()))
        self.assertEqual(texts(view), [f"{GAMES['slots'].brief}\n## ❔ ❔ ❔\n## ❔ ❔ ❔\n## ❔ ❔ ❔"])
        self.assertEqual(layout(view), [discord.ui.Separator, discord.ui.TextDisplay])
        self.assertEqual((labels(view), colour(view)), (["開局", "完整規則", "離桌"], BRAND_COLOUR))

    def test_winning_spin_settles_with_its_multiplier_and_lines(self) -> None:
        view = seat_view(table(Slots(evaluate((3, 4, 7)))))
        self.assertEqual(texts(view)[-1], "### 🎉 中獎 ×4\n中 2 條線：上橫線 🍒🍒 ×2 · 左直線 🍒🍒 ×2")
        self.assertEqual(layout(view), SETTLED)
        self.assertNotIn(GAMES["slots"].brief, testing.text(view))
        self.assertEqual((labels(view)[0], colour(view)), ("再來一局", GOLD_COLOUR))

    def test_losing_spin_says_no_win_and_why(self) -> None:
        view = seat_view(table(Slots(evaluate((10, 20, 30)))))
        self.assertEqual(texts(view)[-1], "### 💨 沒有中獎\n8 條線都沒有連線")
        self.assertEqual(layout(view), SETTLED)
        self.assertEqual(colour(view), LOSS_COLOUR)
        self.assertNotIn("×0", testing.text(view))

    def test_header_shows_the_player_avatar(self) -> None:
        self.assertEqual(thumbnails(seat_view(table(Slots()))), [])
        self.assertEqual(thumbnails(seat_view(table(Slots(), avatar_url=AVATAR))), [AVATAR])


class HeaderTests(unittest.TestCase):
    def test_both_games_put_only_the_player_avatar_in_the_title_row(self) -> None:
        for state in (Blackjack(Round(deck("8", "9", "8", "7"))), Slots(evaluate((3, 4, 7)))):
            with self.subTest(state.key):
                view = seat_view(table(state, avatar_url=AVATAR))
                title = view.children[0].children[0]
                self.assertIsInstance(title, discord.ui.Section)
                self.assertTrue(title.children[0].content.startswith("## "))
                self.assertEqual(title.accessory.media.url, AVATAR)
                self.assertEqual(thumbnails(view), [AVATAR])


class ControlTests(unittest.TestCase):
    def test_buttons_carry_the_state_token_and_rules_keep_their_id(self) -> None:
        view = seat_view(table(Slots()))
        ids = [item.custom_id for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual(ids, [f"{'a' * 32}:again", "horo:game:rules:slots", f"{'a' * 32}:leave"])
        self.assertTrue(all(len(custom_id) <= 100 for custom_id in ids))
        self.assertEqual(view.actions, {f"{'a' * 32}:again": "again", f"{'a' * 32}:leave": "leave"})
        [rules] = [item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == "完整規則"]
        self.assertEqual(rules.style, discord.ButtonStyle.secondary)

    def test_closed_idle_table_folds_into_one_line_without_controls(self) -> None:
        settled = Blackjack(Round(deck("10", "9", "8", "7", "5"))).settle()
        view = seat_view(table(settled, farewell="已閒置結束"))
        self.assertEqual(testing.text(view), "🪑 **已閒置結束**　21 點・<@1>・你輸了")
        self.assertEqual(colour(view), NEUTRAL_COLOUR)
        self.assertEqual([item for item in view.walk_children()
                          if isinstance(item, (discord.ui.Button, discord.ui.Select, discord.ui.ActionRow))], [])
        self.assertEqual(view.actions, {})

    def test_closed_idle_slots_table_keeps_its_result_in_the_line(self) -> None:
        view = seat_view(table(Slots(evaluate((10, 20, 30))), farewell="已閒置結束"))
        self.assertEqual(testing.text(view), "🪑 **已閒置結束**　拉霸・<@1>・沒有中獎")

    def test_finished_round_without_farewell_keeps_its_full_card(self) -> None:
        settled = Blackjack(Round(deck("10", "9", "8", "7", "5"))).settle()
        view = seat_view(table(settled))
        self.assertIn("### 💔 你輸了", testing.text(view))
        self.assertEqual(labels(view), ["再來一局", "完整規則", "離桌"])

    def test_table_views_are_never_stored_for_dispatch(self) -> None:
        for key in GAMES:
            with self.subTest(key):
                self.assertFalse(seat_view(table(GAMES[key].start())).is_dispatchable())


class RevealTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, shown: list[tuple[list[str], str]]) -> MagicMock:
        clicked = testing.interaction()
        record = lambda **kwargs: shown.append((labels(kwargs["view"]), testing.text(kwargs["view"])))
        clicked.response.defer.side_effect = lambda: clicked.response.is_done.configure_mock(return_value=True)
        clicked.edit_original_response = AsyncMock(side_effect=record)
        return clicked

    def test_spinning_frame_shares_the_settled_layout_in_brand_colour(self) -> None:
        state = Slots(evaluate((3, 4, 7)))
        view = SlotsView(table(state), HOST, frame=slots.stopping(state.spin.grid)[0])
        self.assertEqual((layout(view), texts(view)[-1], colour(view)), (SETTLED, "### 🎰 轉動中…", BRAND_COLOUR))
        self.assertEqual(labels(view), [])

    async def test_spin_reveals_the_reels_then_the_result(self) -> None:
        shown = []
        clicked = self.interaction(shown)
        await clicked.response.defer()
        view = SlotsView(table(Slots(evaluate((3, 4, 7)))), HOST)
        with patch.object(tables, "SPIN_DELAYS", (0, 0, 0)):
            await view.present(clicked)
        self.assertEqual(len(shown), 4)
        self.assertEqual([buttons for buttons, _ in shown], [[], [], [], ["再來一局", "完整規則", "離桌"]])
        self.assertTrue(all(text.endswith("\n### 🎰 轉動中…") for _, text in shown[:3]))
        self.assertIn("## ❔ ❔ ❔", shown[0][1])
        self.assertNotIn("轉動中", shown[3][1])
        self.assertIn("### 🎉 中獎 ×4", shown[3][1])

    async def test_failed_frame_still_attempts_the_result(self) -> None:
        clicked = testing.interaction()
        clicked.response.edit_message.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertRaises(discord.HTTPException):
            await SlotsView(table(Slots(evaluate((3, 4, 7)))), HOST).present(clicked)
        self.assertEqual(clicked.response.edit_message.await_count, 2)
        self.assertEqual(labels(clicked.response.edit_message.await_args.kwargs["view"])[0], "再來一局")

    async def test_blackjack_answers_with_one_edit(self) -> None:
        clicked = testing.interaction()
        view = seat_view(table(Blackjack(Round(deck("10", "9", "8", "8")))))
        await view.present(clicked)
        clicked.response.edit_message.assert_awaited_once_with(view=view)
        await view.present(clicked)
        clicked.edit_original_response.assert_awaited_once_with(view=view)


if __name__ == "__main__":
    unittest.main()
