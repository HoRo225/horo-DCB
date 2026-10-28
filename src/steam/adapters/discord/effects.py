from __future__ import annotations

import json

import discord

from src.core.app.effects import EffectKind, MessageProof, StillCurrent, delivered
from src.core.domain.records import DiscordEffect
from src.steam.app.ports import SteamStore
from src.steam.app.steam import Steam
from src.steam.domain.offers import ANNOUNCE


def announces_offer(effect: DiscordEffect, message: discord.Message) -> str | None:
    body = message.content + json.dumps([component.to_dict() for component in message.components], ensure_ascii=False)
    app_id = effect.payload.get("offer", {}).get("app_id")
    if type(app_id) is not int or app_id <= 0 or f"https://store.steampowered.com/app/{app_id}/" not in body:
        return "訊息內容不是這一項 Steam 活動。"
    return None


def steam_effect_kinds(steam: Steam, store: SteamStore) -> tuple[EffectKind, ...]:
    async def retry(effect_id: str, still_current: StillCurrent) -> str | None:
        return delivered(await steam.retry(effect_id, still_current=still_current))

    return (
        EffectKind(ANNOUNCE, "Steam 免費遊戲通知", proof=MessageProof(check=announces_offer),
                   retry=retry, settle=store.settle),
    )
