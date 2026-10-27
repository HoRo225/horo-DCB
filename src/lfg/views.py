from __future__ import annotations

import discord

from src.ai.background import ask_once, clean_reply, persona_prompt
from src.brand import BRAND_COLOUR, ICONS, NEUTRAL_COLOUR, bar, card_title, rana_says, reported, tint
from src.lfg.store import LfgCard

DEPARTED = ("go", "已出發")
CANCELLED = ("cancel", "已取消")
ENDED = ("closed", "已結束")

RANA_SECONDS = 45
RANA_TASK = ("下面是一個還沒滿的揪團。用繁體中文寫 1 句 40 字內的嘴炮，幫忙揪人加入；"
             "「再喊一次」為 true 時是開團 30 分鐘還沒滿的第二次喊人。不要 @ 任何人、不要標題。")

LFG_BUTTONS = {
    "join": ("加入", "join", discord.ButtonStyle.primary),
    "leave": ("退出", "leave", discord.ButtonStyle.secondary),
    "go": ("出發", "go", discord.ButtonStyle.secondary),
    "cancel": ("取消揪團", "cancel", discord.ButtonStyle.secondary),
}


def card_view(card: LfgCard, ended: tuple[str, str] | None = None, avatar: str | None = None) -> discord.ui.LayoutView:
    status = f"{ICONS[ended[0]]} {ended[1]}" if ended else f"<t:{card.expires_at}:R> 自動結束"
    items: list[discord.ui.Item] = [card_title(f"{ICONS['lfg']} {discord.utils.escape_markdown(card.game)}",
                                               f"發起人 <@{card.host_id}> · {status}", thumbnail=avatar)]
    count = len(card.members)
    lines = ["> " + discord.utils.escape_markdown(card.note).replace("\n", "\n> ")] if card.note else []
    lines += [
        f"**成員**　{bar(count / card.size)} {count} / {card.size} 人",
        " · ".join(f"<@{user_id}>" for user_id in card.members),
    ]
    if ended is None and count < card.size:
        lines.append(f"還差 {card.size - count} 人")
    items.append(discord.ui.TextDisplay("\n".join(lines)))
    if card.cheer and ended is None:
        items.append(discord.ui.TextDisplay(rana_says(card.cheer)))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_colour=NEUTRAL_COLOUR if ended else BRAND_COLOUR))
    if ended is None:
        view.add_item(discord.ui.ActionRow(*(LfgButton(action) for action in LFG_BUTTONS)))
    return view


def departure_sub(card: LfgCard) -> str:
    return f"{len(card.members)} 人"


def departure_view(card: LfgCard, sub: str, room_url: str | None = None, image: bool = True) -> discord.ui.LayoutView:
    title = f"{card.game} 出發！"
    items: list[discord.ui.Item] = [
        discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://depart.png", description=f"{title} {sub}"))
        if image else card_title(f"{ICONS['go']} {discord.utils.escape_markdown(title)}", sub),
        discord.ui.TextDisplay(" ".join(f"<@{user_id}>" for user_id in card.members)),
    ]
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_colour=BRAND_COLOUR))
    if room_url:
        view.add_item(discord.ui.ActionRow(discord.ui.Button(label="進入語音房", emoji=ICONS["voice"], url=room_url)))
    return view


async def rana_line(bot, card: LfgCard, *, again: bool) -> str | None:
    guild = bot.get_guild(bot.guild_id)
    host = guild.get_member(card.host_id) if guild is not None else None
    data = {"遊戲": card.game, "目前人數": len(card.members), "上限": card.size, "備註": card.note,
            "發起人暱稱": host.display_name if host is not None else "某位成員", "再喊一次": again}
    reply = await ask_once(bot, card.channel_id, persona_prompt(RANA_TASK, data), timeout=RANA_SECONDS, effort="low", kind="lfg")
    return clean_reply(next((line for line in (reply or "").splitlines() if line.strip()), None), 60)


def list_view(cards: list[LfgCard], guild_id: int) -> discord.ui.LayoutView:
    lines = [f"[{discord.utils.escape_markdown(card.game)}](https://discord.com/channels/{guild_id}/{card.channel_id}/{card.message_id})"
             f" · {len(card.members)} / {card.size} 人 · <t:{card.expires_at}:R> 結束" for card in cards[:15]]
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(card_title(f"{ICONS['lfg']} 揪團"),
                                       discord.ui.TextDisplay("\n".join(lines) or "目前沒有可見的揪團。"),
                                       accent_colour=BRAND_COLOUR))
    return view


class LfgButton(discord.ui.DynamicItem[discord.ui.Button], template=r"horo:lfg:(?P<action>join|leave|go|cancel)"):
    def __init__(self, action: str) -> None:
        label, icon, style = LFG_BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, emoji=tint(ICONS[icon], style), style=style, custom_id=f"horo:lfg:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /) -> LfgButton:
        return cls(match["action"])

    @reported
    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.lfg_operations.execute(interaction, self.action, interaction.message.id)
