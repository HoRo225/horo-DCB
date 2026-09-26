from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from src.calendar.discord_models import (
    event_to_input,
    event_url,
)
from src.calendar.models import (
    CALENDAR_TZ,
    CalendarCreateUncertain,
    CalendarEditUnavailable,
    CalendarUserError,
    build_calendar_event_input,
)
from src.discord_utils import truncate_discord_text

if TYPE_CHECKING:
    from src.calendar.manager import CalendarManager


DRAFT_NOTICE_CONTENT_LIMIT = 4000


def _draft_notice_text(title: str, message: str, draft: dict[str, str]) -> str:
    def safe(value: object) -> str:
        return discord.utils.escape_markdown(str(value))

    content = "\n".join(
        (
            f"## ⚠️ {safe(title)}",
            safe(message),
            "",
            "**原草稿（可複製）**",
            f"活動名稱：{safe(draft.get('name', ''))}",
            f"開始時間：{safe(draft.get('start', ''))}",
            f"活動長度（分鐘）：{safe(draft.get('duration', ''))}",
            f"地點：{safe(draft.get('location', ''))}",
            f"說明：{safe(draft.get('description', ''))}",
        )
    )
    if len(content) <= DRAFT_NOTICE_CONTENT_LIMIT:
        return content
    suffix = "\n\n-# 原草稿過長，部分內容已省略。"
    prefix_limit = DRAFT_NOTICE_CONTENT_LIMIT - len(suffix) - 1
    return truncate_discord_text(content, prefix_limit, suffix)


class _CalendarDraftNoticeView(discord.ui.LayoutView):
    def __init__(self, title: str, message: str, draft: dict[str, str]) -> None:
        super().__init__(timeout=5 * 60)
        self.add_item(discord.ui.TextDisplay(_draft_notice_text(title, message, draft)))


class _CalendarModalRetryView(discord.ui.View):
    def __init__(
        self,
        manager: CalendarManager,
        *,
        user_id: int,
        guild_id: int,
        draft: dict[str, str],
        event_id: int | None,
    ) -> None:
        super().__init__(timeout=5 * 60)
        self.manager = manager
        self.user_id = user_id
        self.guild_id = guild_id
        self.draft = draft
        self.event_id = event_id

    @discord.ui.button(label="返回修改", style=discord.ButtonStyle.primary)
    async def retry(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            CalendarEventModal(
                self.manager,
                draft=self.draft,
                event_id=self.event_id,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        return interaction.user.id == self.user_id and interaction.guild_id == self.guild_id


class CalendarEventModal(discord.ui.Modal):
    def __init__(
        self,
        manager: CalendarManager,
        event: discord.ScheduledEvent | None = None,
        *,
        draft: dict[str, str] | None = None,
        event_id: int | None = None,
    ) -> None:
        event_input = event_to_input(event) if event is not None else None
        editing = event is not None or event_id is not None
        super().__init__(
            title="編輯活動" if editing else "新增活動",
            timeout=5 * 60,
        )
        self.manager = manager
        self.event_id = event.id if event is not None else event_id

        def initial(key: str, fallback: str | None) -> str | None:
            return draft.get(key, "") if draft is not None else fallback

        self.name_input = discord.ui.TextInput(
            label="活動名稱",
            min_length=1,
            max_length=100,
            default=initial("name", event_input.name if event_input is not None else None),
        )
        self.start_input = discord.ui.TextInput(
            label="開始時間（UTC+8）",
            placeholder="例如 2026-09-20 19:30",
            min_length=16,
            max_length=16,
            default=initial(
                "start",
                (
                    event_input.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M")
                    if event_input is not None
                    else None
                ),
            ),
        )
        self.duration_input = discord.ui.TextInput(
            label="活動長度（分鐘）",
            min_length=1,
            max_length=5,
            default=initial(
                "duration", (str(event_input.duration_minutes) if event_input is not None else "60")
            ),
        )
        self.location_input = discord.ui.TextInput(
            label="地點",
            min_length=1,
            max_length=100,
            default=initial(
                "location", (event_input.location if event_input is not None else "Discord")
            ),
        )
        self.description_input = discord.ui.TextInput(
            label="說明（選填）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=initial(
                "description", (event_input.description if event_input is not None else None)
            ),
        )
        for item in (
            self.name_input,
            self.start_input,
            self.duration_input,
            self.location_input,
            self.description_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = {
            "name": str(self.name_input.value),
            "start": str(self.start_input.value),
            "duration": str(self.duration_input.value),
            "location": str(self.location_input.value),
            "description": str(self.description_input.value or ""),
        }
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("行事曆只能在伺服器中使用。", ephemeral=True)
            return
        try:
            duration = int(str(self.duration_input.value).strip())
        except ValueError:
            await self._send_error(interaction, "活動長度必須是整數分鐘。", draft)
            return
        try:
            event_input = build_calendar_event_input(
                name=str(self.name_input.value),
                start=str(self.start_input.value),
                duration_minutes=duration,
                location=str(self.location_input.value),
                description=str(self.description_input.value or ""),
            )
            if self.event_id is None:
                event = await self.manager.create_event(
                    interaction.guild,
                    event_input,
                    interaction.user,
                )
                action = "建立"
            else:
                event = await self.manager.edit_event(
                    interaction.guild,
                    self.event_id,
                    event_input,
                    interaction.user,
                )
                action = "修改"
        except CalendarCreateUncertain as exc:
            await self._send_error(
                interaction,
                str(exc),
                draft,
                retryable=False,
                title="建立結果尚未確認",
            )
            return
        except CalendarEditUnavailable as exc:
            await self._send_error(interaction, str(exc), draft, retryable=False)
            return
        except CalendarUserError as exc:
            await self._send_error(interaction, str(exc), draft)
            return
        start = event_input.start_time
        await interaction.followup.send(
            f"## ✓ 已{action}活動\n"
            f"**{discord.utils.escape_markdown(event_input.name)}**\n"
            f"{discord.utils.format_dt(start, style='F')} · "
            f"{discord.utils.escape_markdown(event_input.location)}\n"
            f"[開啟活動]({event_url(event) or getattr(event, 'url', '')})",
            ephemeral=True,
        )

    async def _send_error(
        self,
        interaction: discord.Interaction,
        message: str,
        draft: dict[str, str],
        *,
        retryable: bool = True,
        title: str = "編輯活動已失效",
    ) -> None:
        if not retryable:
            await interaction.followup.send(
                view=_CalendarDraftNoticeView(title, message, draft),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"⚠️ {message}",
            view=_CalendarModalRetryView(
                self.manager,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id or 0,
                draft=draft,
                event_id=self.event_id,
            ),
            ephemeral=True,
        )
