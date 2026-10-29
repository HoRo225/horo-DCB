from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, card_title, full_rules, reported, send_notice
from src.core.adapters.discord.live import name_text
from src.games.adapters.discord.format import slot_symbol
from src.games.app.lobby import Lobby
from src.games.app.play import Move, Play
from src.games.domain.catalog import GAMES, Game

if TYPE_CHECKING:
    from src.games.adapters.discord.access import DiscordPlayers
    from src.games.adapters.discord.presenter import DiscordTables


class LobbyView(discord.ui.LayoutView):
    def is_dispatchable(self) -> bool:
        return False


def pick_select(host: str) -> discord.ui.Select:
    return discord.ui.Select(custom_id="horo:game:pick", placeholder="選擇遊戲", options=[
        discord.SelectOption(label=game.title, value=key, emoji=ICONS[key], description=name_text(game.summary, host))
        for key, game in GAMES.items()
    ])


def hub_status(host: str) -> str:
    return name_text("選一個遊戲，和{name}玩一局", discord.utils.escape_markdown(host))


def rules_view(game: Game, host: str) -> discord.ui.LayoutView:
    name = discord.utils.escape_markdown(host)
    return full_rules(f"{ICONS[game.key]} {game.title} 完整規則",
                      [(heading, name_text(text, name)) for heading, text in game.rules(slot_symbol)])


def lobby_view(host: str, *, banner: bool = True) -> discord.ui.LayoutView:
    status = hub_status(host)
    if banner:
        title = [
            discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://hub.png", description="遊戲大廳橫幅")),
            discord.ui.TextDisplay(f"-# {status}"),
        ]
    else:
        title = [card_title(f"{ICONS['games']} 遊戲大廳", status)]
    view = LobbyView(timeout=None)
    view.add_item(discord.ui.Container(*title, accent_colour=BRAND_COLOUR))
    view.add_item(discord.ui.ActionRow(pick_select(host)))
    return view


def game_pick_item(
    play: Play, lobby: Lobby, tables: DiscordTables, players: DiscordPlayers,
) -> type[discord.ui.DynamicItem[discord.ui.Select]]:
    class GamePickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"horo:game:pick"):
        def __init__(self) -> None:
            super().__init__(pick_select(tables.host()))

        @classmethod
        async def from_custom_id(cls, interaction, item, match, /) -> GamePickSelect:
            return cls()

        @reported
        async def callback(self, interaction: discord.Interaction) -> None:
            user_id = interaction.user.id
            if problem := lobby.problem(interaction.guild_id, interaction.channel_id, players.role_ids(user_id)):
                await send_notice(interaction, problem, items=tables.hub_link())
                return
            await interaction.response.defer()
            move = Move("open_table", user_id, interaction.guild_id, interaction.channel_id, interaction.id,
                        play.snapshot(user_id)["state_token"], game=self.item.values[0])
            await play.execute(move, tables.reply(interaction))
            try:
                await interaction.edit_original_response(**await tables.lobby_message(interaction))
            except Exception:
                logging.warning("Game lobby menu could not be reset after a pick.", exc_info=True)

    return GamePickSelect


def rules_item(tables: DiscordTables) -> type[discord.ui.DynamicItem[discord.ui.Button]]:
    class RulesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"horo:game:rules:(?P<game>blackjack|slots)"):
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
            await interaction.response.send_message(view=rules_view(GAMES[self.game], tables.host()), ephemeral=True)

    return RulesButton
