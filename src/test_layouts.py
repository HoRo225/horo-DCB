import time
import unittest
from unittest.mock import MagicMock, patch

import discord

from src import testing
from src.admin.pages.ai import AiReadings, render_ai, render_ai_access
from src.admin.pages.ai_models import ModelEditor
from src.admin.pages.effects import render_effects
from src.admin.pages.games import render_games
from src.admin.pages.steam import SteamQuery, render_steam
from src.admin.pages.voice import render_voice
from src.admin.panel import PAGES, AdminPanelView
from src.codex.model_settings import ModelChoice, ModelInfo, ModelSettings
from src.codex.protocol import CodexRuntimeStatus
from src.ai.query_views import help_view
from src.codex.rate_limits import CodexRateLimits, CodexRateWindow
from src.codex.service import DeviceLogin
from src.bot import HoroBot
from src.core.adapters.discord.brand import ICONS, WHITE, full_rules, notice_view
from src.games import slots
from src.games.blackjack import RANKS, SUITS, Card, Round
from src.games.tables import TABLES, BlackjackTableView, SlotTableView
from src.games.views import lobby_view
from src.core.adapters.sqlite.db import transaction
from src.testing import put_setting
from src.steam.models import SteamFetchResult, SteamOffer
from src.steam.views import build_offer_view
from src.games.store import GAMES_HUB_MESSAGE
from src.ai.access import AI_ENABLED
from src.steam.notifier import STEAM_ENABLED, STEAM_ROLE_IDS
from src.voice.manager import VOICE_ENABLED

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
            codex = MagicMock()
            codex.request_counts.return_value = (0, 0)
            bot = HoroBot(codex, guild_id=G, db=db)
            interaction = testing.interaction()
            interaction.user.id = OWNER
            interaction.guild.id = G
            interaction.guild.unavailable = False
            interaction.guild.me.display_name = "樂奈"
            interaction.guild.get_channel.return_value = None
            interaction.guild.get_role.return_value = None
            bot.get_guild = MagicMock(return_value=interaction.guild)
            view = AdminPanelView(bot, bot.live, bot.admin_panels.begin((G, OWNER)), interaction)
            view.state(AiReadings).codex_status = CodexRuntimeStatus(
                reason="auth_required", plan="plus", runtime_version="1.0", thread_count=3, last_error="auth_required",
            )
            view.state(AiReadings).rate_limits = CodexRateLimits(
                int(time.time()), (CodexRateWindow("primary", 20, 300, None),), None
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
            view.games.set_roles(range(1, 26))

            for key, page in PAGES.items():
                page.render(view)
                fits(key, view)
                self.assertEqual(view.children[-1].children[-1].children[0].placeholder, "前往其他頁面…")
            for render in (render_ai, render_ai_access, render_voice, render_steam, render_games):
                render(view, ("warn", "註" * 60))
                fits(f"{render.__name__} with note", view)
                if render is not render_ai_access:
                    self.assertEqual(len(view.children), 2, render.__name__)
            readings = view.state(AiReadings)
            readings.login = DeviceLogin("login-1", "https://auth.openai.com/codex/device", "ABCD-1234")
            render_ai(view, ("warn", "註" * 60))
            fits("render_ai with device code", view)
            readings.codex_status = CodexRuntimeStatus(reason="ready", authenticated=True, plan="plus", last_error="timeout")
            readings.confirm_logout = True
            render_ai(view, ("warn", "註" * 60))
            fits("render_ai confirming logout", view)
            put_setting(db, GAMES_HUB_MESSAGE, [10**18, 10**18])
            render_games(view, ("warn", "註" * 60))
            fits("render_games with hub", view)
            self.assertIn(f"https://discord.com/channels/{G}/{10**18}/{10**18}", [getattr(i, "url", None) for i in view.walk_children()])
            interaction.guild.get_channel_or_thread.return_value = None
            with transaction(db):
                for n in range(30):
                    view.effects.operations.create_effect(f"e{n}", "steam.announce", 10**18 + n, {})
            render_effects(view)
            fits("effects pending", view)
            render_effects(view, ("warn", "註" * 60))
            fits("effects pending with note", view)
            view._render_closed()
            fits("panel closed", view)
            fits("lobby", lobby_view())
            fits("lobby without banner", lobby_view(banner=False))
            for game, table in TABLES.items():
                fits(f"{game} full rules", full_rules(f"{ICONS[game]} {table.title} 完整規則", table.sections()))

            store = view.games
            table = BlackjackTableView(store, OWNER, notice="註", avatar=image)
            fits("blackjack open", table)
            table.game = Round([Card("K", "S"), Card("9", "S"), Card("A", "S"), Card("9", "H")])
            table.render()
            fits("blackjack dealer blackjack", table)
            table.game = game = Round([Card("2", "S")] * 60)
            table.render()
            fits("blackjack dealt", table)
            while not game.done:
                game.apply("hit")
                table.render()
                fits("blackjack hit", table)
            self.assertEqual(len(game.player), 11)
            table.taunt = "嘴" * 60
            table.render()
            fits("blackjack settled with line", table)
            self.assertTrue(any(f"**樂奈**　{table.taunt}" in i.content for i in table.walk_children()
                                if isinstance(i, discord.ui.TextDisplay)))

            spin = SlotTableView(store, OWNER, notice="註", avatar=image)
            fits("slots open", spin)
            with patch("src.games.slots.spin", lambda: slots.evaluate((0, 0, 0))):
                spin.multiplier, spin.result, spin.summary = spin.play()
            spin.render()
            fits("slots spun", spin)
            spin.taunt = "嘴" * 60
            spin.render()
            fits("slots spun with line", spin)
            self.assertTrue(any(f"**樂奈**　{spin.taunt}" in i.content for i in spin.walk_children()
                                if isinstance(i, discord.ui.TextDisplay)))

            roles = [MagicMock(mention=f"<@&{10**18 + n}>") for n in range(25)]
            offer = SteamOffer(1, name, "NT$1,000,000", "說" * 5000, ("開發商",) * 10, image, ends_at=2**31)
            fits("steam offer", build_offer_view(offer, roles))
            fits("ai help", help_view(10**18))
            receipt = [{"ok": n % 2 == 0, "text": "果" * 200, "private_ref": f"ref{n}"} for n in range(3)] + [
                {"ok": True, "text": "果" * 200}]
            for said in ("", "說" * 1500):
                fits(f"ai receipt explained={bool(said)}", bot.actions._receipt_view(receipt, said))
            fits("notice", notice_view("註" * 2000, title="標" * 100))


if __name__ == "__main__":
    unittest.main()
