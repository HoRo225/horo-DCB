from __future__ import annotations

import logging
from collections.abc import Sequence

import aiohttp
import discord

from src.core.adapters.discord.channels import post_problem, text_channel_problem
from src.core.adapters.discord.live import LiveGuild
from src.steam.adapters.discord.views import build_ended_view, build_offer_view
from src.steam.app.ports import DeliveryError, EndResult, RoleState
from src.steam.domain.offers import PostedCard, SteamOffer


class DiscordAnnouncer:
    def __init__(self, client: discord.Client, live: LiveGuild) -> None:
        self._client = client
        self._live = live

    async def ready(self) -> None:
        await self._client.wait_until_ready()

    def guild(self) -> GuildAnnouncer | None:
        guild = self._live.guild()
        return GuildAnnouncer(guild) if guild is not None else None


class GuildAnnouncer:
    def __init__(self, guild: discord.Guild) -> None:
        self._guild = guild

    def channel_problem(self, channel_id: int | None) -> str | None:
        return post_problem(self._guild, self._guild.get_channel(channel_id), "通知頻道")

    def bind_problem(self, channel_id: int) -> str | None:
        channel = self._guild.get_channel(channel_id)
        if channel is None:
            return "指定通知頻道已不存在，未綁定頻道。"
        return text_channel_problem(channel, self._guild) or self.channel_problem(channel_id)

    def role(self, role_id: int, channel_id: int | None) -> RoleState:
        role = self._guild.get_role(role_id)
        if role is None:
            return "missing"
        if role.is_default():
            return "default"
        channel, me = self._guild.get_channel(channel_id), self._guild.me
        if role.mentionable or (me is not None and channel is not None and channel.permissions_for(me).mention_everyone):
            return "usable"
        return "muted"

    async def announce(
        self, channel_id: int, offer: SteamOffer, role_ids: Sequence[int], nonce: str | None,
    ) -> int | None:
        roles = tuple(self._guild.get_role(role_id) for role_id in role_ids)
        try:
            message = await self._guild.get_channel(channel_id).send(
                view=build_offer_view(offer, roles),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=list(roles) if roles else False, replied_user=False,
                ),
                nonce=nonce,
            )
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError) as error:
            rejected = isinstance(error, discord.HTTPException) and 400 <= error.status < 500 and error.status != 429
            raise DeliveryError(type(error).__name__, rejected=rejected) from error
        logging.info("Sent Steam free games notification: %s (%s)", offer.name, offer.app_id)
        message_id = getattr(message, "id", None)
        return message_id if type(message_id) is int else None

    async def end(self, card: PostedCard) -> EndResult:
        channel = self._guild.get_channel(card.channel_id)
        if channel is None:
            return "gone"
        try:
            await channel.get_partial_message(card.message_id).edit(view=build_ended_view(card.offer))
        except discord.NotFound:
            return "gone"
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError):
            logging.exception("Ended Steam free games notification could not be updated: %s", card.offer.app_id)
            return "failed"
        return "ended"
