import time
import unittest
from unittest.mock import MagicMock, patch

import discord

from src import testing
from src.admin.adapters.discord.codex_login import CodexLogin
from src.admin.adapters.discord.pages.ai import AiReadings, render_ai, render_ai_access
from src.admin.adapters.discord.pages.ai_models import ModelEditor
from src.admin.adapters.discord.pages.effects import render_effects
from src.admin.adapters.discord.pages.games import render_games
from src.admin.adapters.discord.pages.steam import SteamQuery, render_steam
from src.admin.adapters.discord.pages.voice import render_voice
from src.admin.adapters.discord.panel import PAGES
from src.ai import DeviceLogin, ModelChoice, ModelInfo, ModelSettings, RateLimits, RateWindow, RuntimeStatus
from src.ai.adapters.discord.help import help_view
from src.core.adapters.discord.receipts import receipt_view
from src.core.adapters.discord.brand import ICONS, WHITE, full_rules, notice_view
from src.games import GAMES, LiveTable
from src.games.adapters.discord.format import slot_symbol
from src.games.adapters.discord.tables import seat_view
from src.games.adapters.discord.views import lobby_view
from src.games.domain import slots
from src.games.domain.blackjack import RANKS, SUITS, Card, Round
from src.games.domain.table import Blackjack, Slots
from src.core.adapters.sqlite.db import transaction
from src.testing import put_setting
from src.steam import SteamFetchResult, SteamOffer
from src.steam.adapters.discord.views import build_offer_view
from src.games.adapters.sqlite.store import GAMES_HUB_MESSAGE
from src.ai.adapters.sqlite.store import AI_ENABLED
from src.steam.adapters.sqlite.store import STEAM_ENABLED, STEAM_ROLE_IDS
from src.voice.adapters.sqlite.store import VOICE_ENABLED
from tests.support.admin import Admin

G, OWNER, MEMBER = 100, 200, 300


class LayoutBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_worst_case_layouts_fit_discord_limits(self) -> None:
        await self.check_layouts("unicode icons")

    async def test_layouts_fit_with_app_emoji_icons(self) -> None:
        icons = {key: f"<:lg{key}:1234567890123456789>" for key in ICONS} | {
            "dots": "<a:lgdots:1234567890123456789>",
            "warn": "<:lowarn:1234567890123456789>",
            "error": "<:lrerror:1234567890123456789>",
        }
        names = [f"bar{colour}{part}" for colour in ("gold", "empty") for part in "lmr"] + ["rana"]
        with (
            patch.dict("src.core.adapters.discord.brand.ICONS", icons),
            patch.dict("src.core.adapters.discord.brand.WHITE", {icon: icon.replace(":lg", ":lw") for icon in icons.values() if ":lg" in icon}),
            patch.dict("src.core.adapters.discord.brand.APP_EMOJIS", {name: f"<:{name}:1234567890123456789>" for name in names}),
        ):
            await self.check_layouts("app emoji icons")

    async def check_layouts(self, label: str) -> None:
        def fits(name: str, view: discord.ui.LayoutView) -> None:
            with self.subTest(f"{label}: {name}"):
                self.assertLessEqual(view.total_children_count, 40)
                self.assertLessEqual(view.content_length(), 4000)
                view.to_components()
                items = [i.item if isinstance(i, discord.ui.DynamicItem) else i for i in view.walk_children()]
                for button in (i for i in items if isinstance(i, discord.ui.Button)):
                    self.assertNotEqual(button.style, discord.ButtonStyle.success, button.label)
                    self.assertIsNotNone(button.emoji, button.label)
                    if button.style in (discord.ButtonStyle.primary, discord.ButtonStyle.danger):
                        self.assertFalse(str(button.emoji) in WHITE, button.label)
                for row in (i for i in items if isinstance(i, discord.ui.ActionRow)):
                    styles = [getattr(getattr(i, "item", i), "style", None) for i in row.children]
                    self.assertLessEqual(styles.count(discord.ButtonStyle.primary), 1)
                for container in (i for i in items if isinstance(i, discord.ui.Container)):
                    texts = [i.content for i in container.walk_children() if isinstance(i, discord.ui.TextDisplay)]
                    small = [line for text in texts for line in text.split("\n") if line.startswith("-# ")]
                    self.assertLessEqual(len(small), 1, small)
                    for child in container.children[:-1]:
                        said = child.walk_children() if isinstance(child, discord.ui.Section) else [child]
                        self.assertFalse(any("**樂奈**　" in getattr(i, "content", "") for i in said))

        card = "<:bj_10H:1234567890123456789>"
        emojis = dict.fromkeys([f"bj_{r}{s}" for r in RANKS for s in SUITS] + ["bj_back"], card)
        emojis |= dict.fromkeys([f"reel{n}" for n in (*slots.PAYS, "spin", "unknown")], "<a:reelspin:1234567890123456789>")
        image = "https://cdn.example.com/header.jpg"
        name = "名" * 200
        with (
            patch.dict("src.core.adapters.discord.brand._brand_urls", card=image),
            patch.dict("src.core.adapters.discord.brand.APP_EMOJIS", emojis),
        ):
            db = testing.memory_db(self)
            for switch in (AI_ENABLED, VOICE_ENABLED, STEAM_ENABLED):
                put_setting(db, switch, True)
            put_setting(db, STEAM_ROLE_IDS, list(range(1, 26)))
            admin = Admin(db)
            interaction = testing.interaction()
            interaction.user.id = OWNER
            interaction.guild.id = G
            interaction.guild.unavailable = False
            interaction.guild.me.display_name = "樂奈"
            interaction.guild.get_channel.return_value = None
            interaction.guild.get_role.return_value = None
            admin.client.get_guild.side_effect = lambda guild_id: interaction.guild
            view = admin.panel(interaction)
            view.state(AiReadings).codex_status = RuntimeStatus(
                reason="auth_required", plan="plus", runtime_version="1.0", thread_count=3, last_error="auth_required",
            )
            view.state(AiReadings).rate_limits = RateLimits(
                int(time.time()), (RateWindow("primary", 20, 300, None),), None
            )
            view.state(SteamQuery).result = SteamFetchResult(
                frozenset(),
                tuple(SteamOffer(n, name, "NT$1,000,000", "", ("dev",), image) for n in range(10)),
                failed_app_count=10,
            )
            view.state(SteamQuery).fetched_at = int(time.time())
            view.state(ModelEditor).catalog = tuple(
                ModelInfo(f"gpt-{n}", f"Model {n}", "low", ("low",), ("text",)) for n in range(30)
            )
            view.state(ModelEditor).draft = ModelSettings(ModelChoice("gpt-0"), ModelChoice("gpt-1"))
            view.state(ModelEditor).notice = ("warn", "註" * 60)
            admin.games.set_roles(range(1, 26))

            for key, page in PAGES.items():
                page.render(view)
                fits(key, view)
                self.assertEqual(view.children[-1].children[-1].children[0].placeholder, "前往其他頁面…")
            for render in (render_ai, render_ai_access, render_voice, render_steam, render_games):
                render(view, ("warn", "註" * 60))
                fits(f"{render.__name__} with note", view)
                if render is not render_ai_access:
                    self.assertEqual(len(view.children), 2, render.__name__)
            readings, login = view.state(AiReadings), view.state(CodexLogin)
            login.login = DeviceLogin("login-1", "https://auth.openai.com/codex/device", "ABCD-1234")
            render_ai(view, ("warn", "註" * 60))
            fits("render_ai with device code", view)
            readings.codex_status = RuntimeStatus(reason="ready", authenticated=True, plan="plus", last_error="timeout")
            login.confirm_logout = True
            render_ai(view, ("warn", "註" * 60))
            fits("render_ai confirming logout", view)
            put_setting(db, GAMES_HUB_MESSAGE, [10**18, 10**18])
            render_games(view, ("warn", "註" * 60))
            fits("render_games with hub", view)
            self.assertIn(f"https://discord.com/channels/{G}/{10**18}/{10**18}", [getattr(i, "url", None) for i in view.walk_children()])
            interaction.guild.get_channel_or_thread.return_value = None
            with transaction(db):
                for n in range(30):
                    admin.operations.create_effect(f"e{n}", "steam.announce", 10**18 + n, {})
            render_effects(view)
            fits("effects pending", view)
            render_effects(view, ("warn", "註" * 60))
            fits("effects pending with note", view)
            view._render_closed()
            fits("panel closed", view)
            fits("lobby", lobby_view())
            fits("lobby without banner", lobby_view(banner=False))
            for game in GAMES.values():
                fits(f"{game.key} full rules", full_rules(f"{ICONS[game.key]} {game.title} 完整規則", game.rules(slot_symbol)))

            hub = [discord.ui.Button(label="前往遊戲大廳", emoji=ICONS["games"], url="https://discord.com/channels/1/2/3")]
            table = LiveTable(OWNER, Blackjack(), "t" * 32, avatar_url=image)
            fits("blackjack open", seat_view(table))
            table.state = Blackjack(Round([Card("K", "S"), Card("9", "S"), Card("A", "S"), Card("9", "H")]))
            fits("blackjack dealer blackjack", seat_view(table))
            game = Round([Card("2", "S")] * 60)
            table.state = Blackjack(game)
            fits("blackjack dealt", seat_view(table))
            while not game.done:
                game.apply("hit")
                fits("blackjack hit", seat_view(table))
            self.assertEqual(len(game.player), 11)
            table.taunt = "嘴" * 60
            shown = seat_view(table)
            fits("blackjack settled with line", shown)
            self.assertTrue(any(f"**樂奈**　{table.taunt}" in i.content for i in shown.walk_children()
                                if isinstance(i, discord.ui.TextDisplay)))
            table.farewell = "註" * 60
            fits("blackjack closed idle", seat_view(table, hub))

            spin = LiveTable(OWNER, Slots(), "t" * 32, avatar_url=image)
            fits("slots open", seat_view(spin))
            spin.state = Slots(slots.evaluate((0, 0, 0)))
            fits("slots spun", seat_view(spin))
            spin.taunt = "嘴" * 60
            shown = seat_view(spin)
            fits("slots spun with line", shown)
            self.assertTrue(any(f"**樂奈**　{spin.taunt}" in i.content for i in shown.walk_children()
                                if isinstance(i, discord.ui.TextDisplay)))
            spin.farewell = "註" * 60
            fits("slots closed idle", seat_view(spin, hub))

            roles = [MagicMock(mention=f"<@&{10**18 + n}>") for n in range(25)]
            offer = SteamOffer(1, name, "NT$1,000,000", "說" * 5000, ("開發商",) * 10, image, ends_at=2**31)
            fits("steam offer", build_offer_view(offer, roles))
            fits("ai help", help_view(10**18))
            receipt = [{"ok": n % 2 == 0, "text": "果" * 200, "private_ref": f"ref{n}"} for n in range(3)] + [
                {"ok": True, "text": "果" * 200}]
            for said in ("", "說" * 1500):
                fits(f"ai receipt explained={bool(said)}", receipt_view(receipt, said, buttons=admin.receipts.private_results.make_buttons(
                    item["private_ref"] for item in receipt if "private_ref" in item)))
            fits("notice", notice_view("註" * 2000, title="標" * 100))


if __name__ == "__main__":
    unittest.main()
