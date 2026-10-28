from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, card_title, full_rules, reported, send_notice
from src.games.adapters.discord.format import slot_symbol
from src.games.app.lobby import Lobby
from src.games.app.play import Move, Play
from src.games.domain.catalog import GAMES

if TYPE_CHECKING:
    from src.games.adapters.discord.access import DiscordPlayers
    from src.games.adapters.discord.presenter import DiscordTables

HUB_STATUS = "選一個遊戲，和樂奈玩一局"
RETIRED = "這個遊戲已下架，請重新從遊戲大廳選擇。"


class LobbyView(discord.ui.LayoutView):
    def is_dispatchable(self) -> bool:
        return False


def pick_select() -> discord.ui.Select:
    return discord.ui.Select(custom_id="horo:game:pick", placeholder="選擇遊戲", options=[
        discord.SelectOption(label=game.title, value=key, emoji=ICONS[key], description=game.summary)
        for key, game in GAMES.items()
    ])


def lobby_view(*, banner: bool = True) -> discord.ui.LayoutView:
    if banner:
        title = [
            discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://hub.png", description="遊戲大廳橫幅")),
            discord.ui.TextDisplay(f"-# {HUB_STATUS}"),
        ]
    else:
        title = [card_title(f"{ICONS['games']} 遊戲大廳", HUB_STATUS)]
    view = LobbyView(timeout=None)
    view.add_item(discord.ui.Container(*title, accent_colour=BRAND_COLOUR))
    view.add_item(discord.ui.ActionRow(pick_select()))
    return view


def game_pick_item(
    play: Play, lobby: Lobby, tables: DiscordTables, players: DiscordPlayers,
) -> type[discord.ui.DynamicItem[discord.ui.Select]]:
    class GamePickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"horo:game:pick"):
        def __init__(self) -> None:
            super().__init__(pick_select())

        @classmethod
        async def from_custom_id(cls, interaction, item, match, /) -> GamePickSelect:
            return cls()

        @reported
        async def callback(self, interaction: discord.Interaction) -> None:
            user_id = interaction.user.id
            if problem := lobby.problem(interaction.guild_id, interaction.channel_id, players.role_ids(user_id)):
                await send_notice(interaction, problem, items=tables.hub_link())
                return
            await interaction.response.edit_message(**await tables.lobby_message())
            move = Move("open_table", user_id, interaction.guild_id, interaction.channel_id, interaction.id,
                        play.snapshot(user_id)["state_token"], game=self.item.values[0])
            await play.execute(move, tables.reply(interaction))

    return GamePickSelect


def rules_item(tables: DiscordTables) -> type[discord.ui.DynamicItem[discord.ui.Button]]:
    class RulesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"horo:game:rules:(?P<game>[a-z_]+)"):
        def __init__(self, game: str) -> None:
            super().__init__(
                discord.ui.Button(label="完整規則", emoji=ICONS["rules"], style=discord.ButtonStyle.secondary,
                                  custom_id=f"horo:game:rules:{game}")
            )
            self.game = game

        @classmethod
        async def from_custom_id(cls, interaction, item, match, /) -> RulesButton:
            return cls(match["game"])

        @reported
        async def callback(self, interaction: discord.Interaction) -> None:
            if (game := GAMES.get(self.game)) is None:
                await send_notice(interaction, RETIRED, items=tables.hub_link())
                return
            view = full_rules(f"{ICONS[game.key]} {game.title} 完整規則", game.rules(slot_symbol))
            await interaction.response.send_message(view=view, ephemeral=True)

    return RulesButton
