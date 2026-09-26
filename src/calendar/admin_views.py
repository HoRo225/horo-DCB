from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord

from src.brand import BRAND_COLOUR, branded_title
from src.calendar.models import (
    CalendarBinding,
    CalendarUserError,
)

if TYPE_CHECKING:
    from src.calendar.manager import CalendarManager


CALENDAR_STATE_UNAVAILABLE_NOTICE = "⚠️ 行事曆狀態目前不可用，請管理員檢查儲存狀態並重新啟動 Bot。"


class _CalendarAdminChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="選擇新的行事曆頻道",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CalendarAdminView) or view._closed or interaction.guild is None:
            return
        if not view.manager.state_available:
            await view.publish_state_unavailable(interaction)
            return
        selected = self.values[0] if self.values else None
        channel = selected.resolve() if selected is not None else None
        if channel is None or getattr(channel, "type", None) != discord.ChannelType.text:
            await interaction.response.send_message(
                "只能選擇目前伺服器中的文字頻道。",
                ephemeral=True,
            )
            return

        view.pending_channel_id = channel.id
        view.pending_channel = channel
        view.notice = None
        view.unbind_target = None
        await interaction.response.defer()
        await view.publish(interaction)


class _CalendarAdminActionButton(discord.ui.Button):
    def __init__(self, action: str) -> None:
        label, style = {
            "apply": ("套用綁定", discord.ButtonStyle.primary),
            "refresh": ("重新整理", discord.ButtonStyle.secondary),
            "unbind": ("解除綁定", discord.ButtonStyle.secondary),
            "unbind_confirm": ("確認解除", discord.ButtonStyle.danger),
            "unbind_cancel": ("取消", discord.ButtonStyle.secondary),
        }[action]
        super().__init__(
            label=label,
            style=style,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CalendarAdminView) or view._closed or interaction.guild is None:
            return
        if not view.manager.state_available:
            await view.publish_state_unavailable(interaction)
            return
        if self.action == "unbind":
            view.unbind_target = view.manager.get_binding(view.guild_id)
            view.notice = None if view.unbind_target is not None else "目前沒有綁定行事曆看板。"
            await interaction.response.defer()
            await view.publish(interaction)
            return
        if self.action == "unbind_cancel":
            view.unbind_target = None
            await interaction.response.defer()
            await view.publish(interaction)
            return
        channel = view.pending_channel
        unbind_target = view.unbind_target
        binding_revision = (
            view.manager.get_binding_revision(view.guild_id)
            if self.action in {"apply", "unbind_confirm"}
            else None
        )
        if self.action in {"apply", "unbind_confirm"}:
            view.operation_version += 1
        operation_version = view.operation_version
        await interaction.response.defer()
        if self.action in {"apply", "unbind_confirm"} and (
            view._closed or operation_version != view.operation_version
        ):
            return
        if self.action in {"apply", "unbind_confirm"} and not await view.still_admin():
            view.notice = "⚠️ 你已不是伺服器管理員，操作已取消。"
            view.unbind_target = None
            await view.publish(interaction)
            return
        if self.action == "apply":
            if channel is None:
                view.notice = "⚠️ 請先選擇文字頻道。"
            else:
                try:
                    binding = await view.manager.bind(
                        interaction.guild,
                        channel,
                        actor_id=interaction.user.id,
                        is_current=lambda: (
                            not view._closed
                            and operation_version == view.operation_version
                            and binding_revision == view.manager.get_binding_revision(view.guild_id)
                        ),
                    )
                    if view._closed or operation_version != view.operation_version:
                        return
                    view.notice = f"✓ 已綁定到 <#{binding.channel_id}>。"
                    if view.pending_channel_id == channel.id:
                        view.pending_channel_id = None
                        view.pending_channel = None
                except CalendarUserError as exc:
                    if view._closed or operation_version != view.operation_version:
                        return
                    view.notice = f"⚠️ {exc}"
        elif self.action == "refresh":
            ok = await view.manager.refresh_guild(interaction.guild)
            view.notice = "✓ 行事曆看板已重新整理。" if ok else "⚠️ 行事曆看板目前無法重新整理。"
        else:
            if unbind_target is None or view.unbind_target != unbind_target:
                view.notice = "解除確認已取消或失效，請重新確認。"
            else:
                try:
                    removed = await view.manager.unbind(
                        interaction.guild,
                        actor_id=interaction.user.id,
                        expected_binding=unbind_target,
                        is_current=lambda: (
                            not view._closed
                            and operation_version == view.operation_version
                            and binding_revision == view.manager.get_binding_revision(view.guild_id)
                            and view.unbind_target == unbind_target
                        ),
                    )
                    if view._closed or operation_version != view.operation_version:
                        return
                    view.notice = "✓ 已解除行事曆看板。" if removed else "目前沒有綁定行事曆看板。"
                except CalendarUserError as exc:
                    if view._closed or operation_version != view.operation_version:
                        return
                    view.notice = f"⚠️ {exc}"
            if view.unbind_target == unbind_target:
                view.unbind_target = None
        if not view._closed and operation_version == view.operation_version:
            await view.publish(interaction)


class CalendarAdminView(discord.ui.LayoutView):
    def __init__(self, manager: CalendarManager, *, user_id: int, guild: discord.Guild) -> None:
        # Close before the latest 15-minute interaction token expires.
        super().__init__(timeout=14 * 60)
        self.last_interaction: discord.Interaction | None = None
        self.manager = manager
        self.user_id = user_id
        self.guild = guild
        self.guild_id = guild.id
        self.pending_channel_id: int | None = None
        self.pending_channel: discord.TextChannel | None = None
        self.notice: str | None = None
        self.unbind_target: CalendarBinding | None = None
        self.operation_version = 0
        self._closed = False
        self._publish_lock = asyncio.Lock()
        self._channel_select = _CalendarAdminChannelSelect()
        self._action_buttons = {
            action: _CalendarAdminActionButton(action)
            for action in (
                "apply",
                "refresh",
                "unbind",
                "unbind_confirm",
                "unbind_cancel",
            )
        }
        self.render()

    async def publish_state_unavailable(self, interaction: discord.Interaction) -> None:
        self.notice = CALENDAR_STATE_UNAVAILABLE_NOTICE
        self.unbind_target = None
        await interaction.response.defer()
        await self.publish(interaction)

    def _button(self, action: str, *, disabled: bool) -> _CalendarAdminActionButton:
        button = self._action_buttons[action]
        button.disabled = disabled
        return button

    async def publish(self, interaction: discord.Interaction) -> None:
        async with self._publish_lock:
            if self._closed:
                return
            old_children = tuple(self.children)
            try:
                self.render()
                await interaction.edit_original_response(
                    view=self,
                )
            except Exception, asyncio.CancelledError:
                self.clear_items()
                for child in old_children:
                    self.add_item(child)
                raise

    def render(self) -> None:
        self.clear_items()
        state_available = self.manager.state_available
        if state_available:
            if self.notice == CALENDAR_STATE_UNAVAILABLE_NOTICE:
                self.notice = None
            binding = self.manager.get_binding(self.guild_id)
            bound = binding is not None
            binding_valid = self.manager.binding_channel_is_valid(self.guild)
            status = (
                f"**狀態正常**　<#{binding.channel_id}>\n-# 看板訊息會隨活動與日期自動更新。"
                if binding_valid and binding is not None
                else (
                    f"**需要處理**　<#{binding.channel_id}> 不是一般文字頻道或已不存在。"
                    "\n-# 請選擇文字頻道重新綁定，或解除綁定。"
                    if binding is not None
                    else "**尚未綁定**\n-# 選擇文字頻道後按「套用綁定」。"
                )
            )
        else:
            self.notice = CALENDAR_STATE_UNAVAILABLE_NOTICE
            self.unbind_target = None
            binding = None
            bound = False
            binding_valid = False
            status = "**行事曆狀態目前不可用**\n-# 請管理員檢查儲存狀態並重新啟動 Bot。"
        self._channel_select.disabled = not state_available
        if self.pending_channel_id is not None:
            status += f"\n\n**待套用**　<#{self.pending_channel_id}>"
        if self.notice and self.notice != CALENDAR_STATE_UNAVAILABLE_NOTICE:
            status += f"\n\n{discord.utils.escape_markdown(self.notice)}"
        children: list[discord.ui.Item] = [
            branded_title("📅 行事曆管理", "設定公開看板的位置與更新狀態"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            discord.ui.TextDisplay(status),
            discord.ui.ActionRow(self._channel_select),
        ]
        if self.unbind_target is not None:
            children.extend(
                (
                    discord.ui.TextDisplay(
                        "### 解除綁定？\n-# 看板訊息將被移除；既有 Discord 活動不會刪除。"
                    ),
                    discord.ui.ActionRow(
                        self._button("unbind_confirm", disabled=False),
                        self._button("unbind_cancel", disabled=False),
                    ),
                )
            )
        else:
            buttons: list[discord.ui.Button] = [
                self._button(
                    "apply",
                    disabled=not state_available or self.pending_channel_id is None,
                ),
                self._button("refresh", disabled=not state_available or not binding_valid),
            ]
            if binding_valid and binding is not None:
                buttons.append(
                    discord.ui.Button(
                        label="開啟看板",
                        style=discord.ButtonStyle.link,
                        url=(
                            f"https://discord.com/channels/{self.guild_id}/"
                            f"{binding.channel_id}/{binding.message_id}"
                        ),
                    )
                )
            buttons.append(self._button("unbind", disabled=not state_available or not bound))
            children.append(discord.ui.ActionRow(*buttons))
        self.add_item(discord.ui.Container(*children, accent_colour=BRAND_COLOUR))

    async def still_admin(self) -> bool:
        member = self.guild.get_member(self.user_id)
        if member is None:
            try:
                async with asyncio.timeout(3):
                    member = await self.guild.fetch_member(self.user_id)
            except TimeoutError, discord.HTTPException:
                return False
        return member.guild_permissions.administrator

    async def on_timeout(self) -> None:
        self._closed = True
        self.operation_version += 1
        self.unbind_target = None
        async with self._publish_lock:
            self.clear_items()
            self.add_item(
                discord.ui.Container(
                    branded_title("📅 行事曆管理", ""),
                    discord.ui.TextDisplay(
                        "-# 管理面板已關閉；重新輸入 /行事曆 可再開啟。公開看板不受影響。"
                    ),
                    discord.ui.ActionRow(discord.ui.Button(label="已關閉", disabled=True)),
                    accent_colour=BRAND_COLOUR,
                )
            )
            interaction = self.last_interaction
            if interaction is None:
                return
            try:
                await interaction.edit_original_response(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if (
            not self._closed
            and interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
            and getattr(interaction.permissions, "administrator", False)
        ):
            self.last_interaction = interaction
            return True
        await interaction.response.send_message(
            "只有開啟面板的伺服器管理員可以操作這個行事曆面板。",
            ephemeral=True,
        )
        return False
