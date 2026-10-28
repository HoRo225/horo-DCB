import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.live import LiveGuild
from src.games.access import hub_link, play_problem, role_problem


class PlayPermissionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = SimpleNamespace(id=1, roles=[SimpleNamespace(id=7)])
        self.guild = MagicMock(spec=discord.Guild, id=5, unavailable=False)
        self.guild.get_member.return_value = self.actor
        self.bot = SimpleNamespace(
            guild_id=5,
            games=SimpleNamespace(channel_id=10, role_ids={7}, hub_message=None),
            get_guild=lambda guild_id: self.guild,
        )
        self.live = LiveGuild(self.bot, 5, lambda: False)
        self.user = SimpleNamespace(id=1, roles=[SimpleNamespace(id=7)])

    def play_problem(self, member):
        return play_problem(self.live, self.bot.games, 5, 10, member)

    def clicked_problem(self):
        return self.play_problem(self.live.member(self.user.id))

    def test_click_uses_current_roles_instead_of_its_member_snapshot(self):
        self.actor.roles = []
        self.assertEqual(self.clicked_problem(), "你沒有使用這個功能的身分組。")
        self.user.roles = []
        self.actor.roles = [SimpleNamespace(id=7)]
        self.assertIsNone(self.clicked_problem())

    def test_hub_link_jumps_to_the_published_hub(self):
        self.assertEqual(hub_link(self.bot), [])
        self.bot.games.hub_message = [10, 50]
        [link] = hub_link(self.bot)
        self.assertEqual((link.label, link.url, str(link.emoji)),
                         ("前往遊戲大廳", "https://discord.com/channels/5/10/50", ICONS["games"]))

    def test_role_problem_passes_matching_or_unconfigured_roles(self):
        self.assertIsNone(role_problem(self.bot.games, self.actor))
        self.assertEqual(role_problem(self.bot.games, SimpleNamespace(roles=[])), "你沒有使用這個功能的身分組。")
        self.bot.games.role_ids = set()
        self.assertIsNone(role_problem(self.bot.games, SimpleNamespace(roles=[])))

    def test_missing_member_or_unavailable_guild_fails_closed(self):
        identity = "目前無法確認你的伺服器身分，請稍後再試。"
        self.guild.get_member.return_value = None
        self.assertEqual(self.clicked_problem(), identity)
        self.guild.get_member.return_value = self.actor
        self.guild.unavailable = True
        self.assertEqual(self.clicked_problem(), identity)
        self.guild = None
        self.assertEqual(self.clicked_problem(), identity)

    def test_domain_context_keeps_its_freshly_fetched_member(self):
        self.actor.roles = []
        self.assertIsNone(self.play_problem(self.user))

    def test_wrong_channel_or_closed_lobby_is_named_before_identity(self):
        self.guild = None
        self.assertEqual(play_problem(self.live, self.bot.games, 5, 11, None), "請到 <#10> 使用遊戲大廳。")
        self.bot.games.channel_id = None
        self.assertEqual(play_problem(self.live, self.bot.games, 5, 10, None),
                         "遊戲大廳尚未開放，請管理員先在 /控制台 設定遊戲頻道。")


if __name__ == "__main__":
    unittest.main()
