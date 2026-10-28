import unittest

from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.live import LiveGuild
from src.games.adapters.discord.access import (
    CANNOT_PLAY,
    NO_CHANNEL,
    NO_GRANT,
    NO_GUILD,
    NO_MEMBER,
    DiscordPlayers,
    hub_link,
)
from src.games.app.ports import Player, Refused
from tests.support.discord import Client, Guild, channel, member

G, CHANNEL = 5, 10


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.guild = Guild(G).add(member(1, roles=(7,), name="玩家"), channel(CHANNEL, view_channel=True, send_messages=True))
        self.client = Client(self.guild)
        self.players = DiscordPlayers(LiveGuild(self.client, G, lambda: False))

    async def refusal(self, *, fresh: bool, channel_id: int = CHANNEL, user_id: int = 1) -> str:
        with self.assertRaises(Refused) as raised:
            await self.players.player(channel_id, user_id, fresh=fresh)
        return raised.exception.text

    async def test_cached_member_who_can_post_is_a_player(self) -> None:
        self.assertEqual(await self.players.player(CHANNEL, 1, fresh=False),
                         Player(1, "玩家", "https://cdn.example.com/1.png", frozenset({7})))
        self.assertEqual(self.players.role_ids(1), frozenset({7}))
        self.assertIsNone(self.players.role_ids(2))

    async def test_requests_use_the_freshly_fetched_member(self) -> None:
        self.guild.add(member(1, roles=(8,)), fetched=True)
        self.assertEqual((await self.players.player(CHANNEL, 1, fresh=True)).role_ids, frozenset({8}))
        self.assertEqual(await self.refusal(fresh=True, user_id=2), NO_GRANT)

    async def test_unconfirmed_guild_member_or_channel_is_refused(self) -> None:
        self.assertEqual(await self.refusal(fresh=False, user_id=2), NO_MEMBER)
        self.assertEqual(await self.refusal(fresh=False, channel_id=CHANNEL + 1), NO_CHANNEL)
        self.guild.add(channel(CHANNEL + 2, view_channel=True))
        self.assertEqual(await self.refusal(fresh=False, channel_id=CHANNEL + 2), CANNOT_PLAY)
        self.guild.unavailable = True
        self.assertEqual(await self.refusal(fresh=False), NO_GUILD)
        self.assertEqual(await self.refusal(fresh=True), "目前無法確認最新的伺服器狀態，請稍後再試。")


class HubLinkTests(unittest.TestCase):
    def test_link_jumps_to_the_published_hub(self) -> None:
        self.assertEqual(hub_link(None, G), [])
        [link] = hub_link([CHANNEL, 50], G)
        self.assertEqual((link.label, link.url, str(link.emoji)),
                         ("前往遊戲大廳", f"https://discord.com/channels/{G}/{CHANNEL}/50", ICONS["games"]))


if __name__ == "__main__":
    unittest.main()
