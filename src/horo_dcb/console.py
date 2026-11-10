import asyncio
import contextlib
from dataclasses import dataclass

import discord
from discord import ui

from . import log_event
from .codex import choose_model


EFFORT_NAMES = {"none": "不思考", "minimal": "最低", "low": "低", "medium": "中", "high": "高", "xhigh": "超高", "max": "最高", "ultra": "極限"}


@dataclass(frozen=True)
class LoginAttempt:
    id: str
    url: str
    code: str


def quota_text(limits: dict) -> str:
    buckets = limits.get("rateLimitsByLimitId") or {"codex": limits.get("rateLimits", {})}
    lines = []
    for bucket_id, snapshot in buckets.items():
        prefix = f"{snapshot.get('limitName') or bucket_id} · " if len(buckets) > 1 else ""
        for key, fallback in (("primary", "短期額度"), ("secondary", "長期額度")):
            window = snapshot.get(key)
            if not window:
                lines.append(f"{prefix}{fallback}：無資料")
                continue
            minutes = window.get("windowDurationMins")
            if minutes is None:
                title = fallback
            elif minutes % 1440 == 0:
                title = f"{minutes // 1440} 天額度"
            elif minutes % 60 == 0:
                title = f"{minutes // 60} 小時額度"
            else:
                title = f"{minutes} 分鐘額度"
            used = window.get("usedPercent")
            usage = f"已用 {used}%" if used is not None else "無資料"
            reset = window.get("resetsAt")
            when = f"<t:{reset}:R>" if reset is not None else "無資料"
            lines.append(f"{prefix}{title}：{usage}，重置時間 {when}")
    return "\n".join(lines) or "額度：無資料"


class InstructionsModal(ui.Modal):
    def __init__(self, console, current: str | None) -> None:
        super().__init__(title="AI 指示", timeout=900)
        self.console = console
        self.text = ui.TextInput(label="給 AI 的固定指示，只套用到新對話", style=discord.TextStyle.paragraph, required=False, max_length=4000, default=current or "")
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not await self.console.bot.is_owner(interaction.user):
            await interaction.followup.send("只有擁有者能使用", ephemeral=True)
            return
        await self.console.bot.store.update_settings(instructions=self.text.value.strip() or None)
        await self.console.refresh_panels()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_event("console_interaction_error", error)


class ConsolePanel(ui.LayoutView):
    def __init__(self, console, interaction: discord.Interaction) -> None:
        super().__init__(timeout=900)
        self.console = console
        self.interaction = interaction
        self.expired = False
        self.render_lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (interaction.data or {}).get("custom_id") == "instructions":
            if not await self.console.bot.is_owner(interaction.user):
                await interaction.response.send_message("只有擁有者能使用", ephemeral=True)
                return False
            return True
        await interaction.response.defer()
        if not await self.console.bot.is_owner(interaction.user):
            await interaction.followup.send("只有擁有者能使用", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item) -> None:
        log_event("console_interaction_error", error)

    async def on_timeout(self) -> None:
        self.expired = True
        self.console.panels.discard(self)

    async def rebuild(self) -> None:
        settings = await self.console.bot.store.settings()
        self.clear_items()
        account_state = self.console.account
        account = account_state.get("account") if account_state is not None else None
        if account_state is None:
            status = "帳號狀態暫時無法確認"
        else:
            status = f"已登入 ChatGPT，方案：{account.get('planType') or '無資料'}" if account else "尚未登入 ChatGPT"
        models = self.console.models
        model, effort = choose_model(models, settings.model, settings.effort)
        if model is None:
            choice = "模型：無資料"
        else:
            model_note = "" if settings.model == model["id"] else "（預設）"
            effort_note = "" if settings.effort == effort else "（預設）"
            choice = f"模型：{model['displayName']}{model_note}\n思考強度：{EFFORT_NAMES.get(effort, effort)}{effort_note}"
        instructions = f"指示：已設定 {len(settings.instructions)} 字，只套用到新對話" if settings.instructions else "指示：未設定"
        text = f"## 控制台\n{status}\n{quota_text(self.console.limits)}\n{choice}\n{instructions}"
        if self.console.note:
            text += f"\n\n{self.console.note}"
        attempt = self.console.login_attempt
        if attempt is not None:
            text += f"\n\n請用機器人專用的 ChatGPT 帳號開啟 {attempt.url}\n登入代碼：`{attempt.code}`\n等待登入完成，面板會自動更新。"
        self.add_item(ui.Container(ui.TextDisplay(text)))
        actions = ui.ActionRow()
        for label, operation in (("登入 ChatGPT", "login"), ("取消登入", "cancel"), ("登出", "logout"), ("重新整理", "refresh")):
            button = ui.Button(label=label, style=discord.ButtonStyle.secondary)

            async def callback(interaction: discord.Interaction, operation=operation) -> None:
                await self.console.action(operation)

            button.callback = callback
            actions.add_item(button)
        edit = ui.Button(label="編輯指示", style=discord.ButtonStyle.secondary, custom_id="instructions")

        async def edit_callback(interaction: discord.Interaction) -> None:
            current = await self.console.bot.store.settings()
            await interaction.response.send_modal(InstructionsModal(self.console, current.instructions))

        edit.callback = edit_callback
        actions.add_item(edit)
        self.add_item(actions)
        channels = ui.ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="選擇 AI 頻道，清空即停用", min_values=0, max_values=25, default_values=[discord.Object(value) for value in settings.ai_channel_ids])

        async def channel_callback(interaction: discord.Interaction) -> None:
            await self.console.bot.store.update_settings(ai_channel_ids=[value.id for value in channels.values])
            await self.console.refresh_panels()

        channels.callback = channel_callback
        self.add_item(ui.ActionRow(channels))
        roles = ui.RoleSelect(placeholder="選擇可使用 AI 的身分組，清空即停用", min_values=0, max_values=25, default_values=[discord.Object(value) for value in settings.allowed_role_ids])

        async def role_callback(interaction: discord.Interaction) -> None:
            await self.console.bot.store.update_settings(allowed_role_ids=[value.id for value in roles.values])
            await self.console.refresh_panels()

        roles.callback = role_callback
        self.add_item(ui.ActionRow(roles))
        hubs = ui.ChannelSelect(channel_types=[discord.ChannelType.voice], placeholder="選擇臨時語音入口，清空即停用", min_values=0, max_values=25, default_values=[discord.Object(value) for value in settings.voice_hub_ids])

        async def hub_callback(interaction: discord.Interaction) -> None:
            await self.console.bot.store.update_settings(voice_hub_ids=[value.id for value in hubs.values])
            await self.console.refresh_panels()
            if interaction.guild is not None:
                await self.console.bot.voice.settle(interaction.guild)

        hubs.callback = hub_callback
        self.add_item(ui.ActionRow(hubs))
        if model is None:
            return
        model_select = ui.Select(placeholder="選擇模型，清空即用預設", min_values=0, max_values=1, options=[discord.SelectOption(label=value["displayName"], value=value["id"], description=(value.get("description") or "")[:100] or None, default=value["id"] == settings.model) for value in models[:25]])

        async def model_callback(interaction: discord.Interaction) -> None:
            selected = model_select.values[0] if model_select.values else None
            current = await self.console.bot.store.settings()
            _, effort = choose_model(self.console.models, selected, current.effort)
            await self.console.bot.store.update_settings(model=selected, effort=current.effort if effort == current.effort else None)
            await self.console.refresh_panels()

        model_select.callback = model_callback
        self.add_item(ui.ActionRow(model_select))
        effort_select = ui.Select(placeholder="選擇思考強度，清空即用預設", min_values=0, max_values=1, options=[discord.SelectOption(label=EFFORT_NAMES.get(value["reasoningEffort"], value["reasoningEffort"]), value=value["reasoningEffort"], description=(value.get("description") or "")[:100] or None, default=value["reasoningEffort"] == settings.effort) for value in model.get("supportedReasoningEfforts", [])[:25]])

        async def effort_callback(interaction: discord.Interaction) -> None:
            await self.console.bot.store.update_settings(effort=effort_select.values[0] if effort_select.values else None)
            await self.console.refresh_panels()

        effort_select.callback = effort_callback
        self.add_item(ui.ActionRow(effort_select))

    async def refresh(self) -> None:
        if self.expired or self.interaction.is_expired():
            self.console.panels.discard(self)
            return
        try:
            async with self.render_lock:
                await self.rebuild()
                await self.interaction.edit_original_response(view=self)
        except discord.HTTPException as error:
            log_event("console_panel_error", error)
            self.console.panels.discard(self)


class Console:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.panels: set[ConsolePanel] = set()
        self.login_attempt: LoginAttempt | None = None
        self.account: dict | None = None
        self.limits: dict = {}
        self.models: list[dict] = []
        self.note = ""
        self.lock = asyncio.Lock()
        self.queue = bot.codex.subscribe()
        self.monitor: asyncio.Task | None = None

    def start(self) -> None:
        self.monitor = asyncio.create_task(self._monitor())

    async def close(self) -> None:
        self.bot.codex.unsubscribe(self.queue)
        if self.monitor is not None:
            self.monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor
        for panel in tuple(self.panels):
            panel.stop()
        self.panels.clear()

    async def open(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self.bot.is_owner(interaction.user):
            await interaction.edit_original_response(content="只有擁有者能使用")
            return
        async with self.lock:
            try:
                await self._read_account()
            except Exception as error:
                log_event("console_account_error", error)
                self.note = "暫時無法讀取帳號狀態，請稍後重新整理。"
        panel = ConsolePanel(self, interaction)
        await panel.rebuild()
        await interaction.edit_original_response(view=panel)
        self.panels.add(panel)

    async def _read_account(self) -> None:
        self.account = None
        self.limits = {}
        self.models = []
        self.account = await self.bot.codex.account()
        if self.account.get("account") is not None:
            try:
                self.limits = await self.bot.codex.rate_limits()
            except Exception as error:
                log_event("console_quota_error", error)
            try:
                self.models = await self.bot.codex.models()
            except Exception as error:
                log_event("console_models_error", error)

    async def action(self, operation: str) -> None:
        async with self.lock:
            try:
                if operation in ("login", "cancel", "logout") and self.login_attempt is not None:
                    await self.bot.codex.cancel_login(self.login_attempt.id)
                    self.login_attempt = None
                if operation == "login":
                    result = await self.bot.codex.login()
                    self.login_attempt = LoginAttempt(result["loginId"], result["verificationUrl"], result["userCode"])
                    self.note = ""
                elif operation == "logout":
                    await self.bot.codex.logout()
                    self.note = "已登出。"
                elif operation == "cancel":
                    self.note = "已取消登入。"
                else:
                    self.note = ""
                await self._read_account()
            except Exception as error:
                log_event("console_action_error", error)
                self.note = "操作失敗，請稍後重新整理。"
        await self.refresh_panels()

    async def refresh_panels(self) -> None:
        await asyncio.gather(*(panel.refresh() for panel in tuple(self.panels)))

    async def _monitor(self) -> None:
        while True:
            method, params = await self.queue.get()
            try:
                if method == "account/login/completed":
                    async with self.lock:
                        if self.login_attempt is None or params.get("loginId") != self.login_attempt.id:
                            continue
                        self.login_attempt = None
                        await self._read_account()
                        self.note = "登入完成。" if params.get("success") and self.account.get("account") is not None else "登入未完成，請重新登入。"
                    await self.refresh_panels()
                elif method == "account/rateLimits/updated" and self.panels:
                    async with self.lock:
                        self.limits = await self.bot.codex.rate_limits()
                    await self.refresh_panels()
                elif method in ("client/connected", "account/updated"):
                    async with self.lock:
                        await self._read_account()
                    await self.refresh_panels()
            except Exception as error:
                log_event("console_update_error", error)
