from __future__ import annotations

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, card_title, full_rules, reported, send_notice
from src.games import access
from src.games import table_runtime
from src.games.tables import TABLES

GAME_PICKS = {
    "blackjack": ("21 點", "跟樂奈比點數 · 叫牌或停牌"),
    "slots": ("拉霸", "3×3 盤面 · 各線倍數相加"),
}
HUB_STATUS = "選一個遊戲，和樂奈玩一局"


def lobby_view(*, banner: bool = True) -> discord.ui.LayoutView:
    if banner:
        title = [
            discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://hub.png", description="遊戲大廳橫幅")),
            discord.ui.TextDisplay(f"-# {HUB_STATUS}"),
        ]
    else:
        title = [card_title(f"{ICONS['games']} 遊戲大廳", HUB_STATUS)]
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*title, accent_colour=BRAND_COLOUR))
    view.add_item(discord.ui.ActionRow(GamePickSelect()))
    return view


class GamePickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"horo:game:pick"):
    def __init__(self) -> None:
        super().__init__(discord.ui.Select(custom_id="horo:game:pick", placeholder="選擇遊戲", options=[
            discord.SelectOption(label=label, value=key, emoji=ICONS[key], description=summary)
            for key, (label, summary) in GAME_PICKS.items()
        ]))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /) -> GamePickSelect:
        return cls()

    @reported
    async def callback(self, interaction: discord.Interaction) -> None:
        if problem := interaction.client.game_operations.pick_problem(interaction):
            await send_notice(interaction, problem, items=access.hub_link(interaction.client))
            return
        await interaction.response.edit_message(view=lobby_view(banner=bool(interaction.message.attachments)))
        await table_runtime.open_table(interaction, self.item.values[0])


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
        if (table := TABLES.get(self.game)) is None:
            await send_notice(interaction, "這個遊戲已下架，請重新從遊戲大廳選擇。",
                              items=access.hub_link(interaction.client))
        else:
            view = full_rules(f"{ICONS[table.key]} {table.title} 完整規則", table.sections())
            await interaction.response.send_message(view=view, ephemeral=True)
