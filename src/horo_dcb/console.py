import asyncio
import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING

import discord
from discord import ui

from . import log_event
from .codex import choose_model
from .store import Settings

if TYPE_CHECKING:
    from .bot import HoroBot


EFFORT_NAMES = {"none": "不思考", "minimal": "最低", "low": "低", "medium": "中", "high": "高", "xhigh": "超高", "max": "最高", "ultra": "極限"}
FILLED_CELL = "▰"
EMPTY_CELL = "▱"
NO_PLACEHOLDER = "\u200b"
PANEL_LIFETIME = timedelta(minutes=14, seconds=50)
RECONNECTING = "-# 正在自動重連，連上後面板會自動更新。這段期間收到的提問會在連上後回答。"


class Level(Enum):
    OK = 0x23A55A
    WARN = 0xF0B232
    BAD = 0xF23F43
    UNKNOWN = 0x80848E


@dataclass(frozen=True)
class QuotaRow:
    name: str
    used: float
    resets_at: int | None


def window_title(minutes: int | None, fallback: str) -> str:
    if minutes is None:
        return fallback
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天額度"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小時額度"
    return f"{minutes} 分鐘額度"


def quota_rows(limits: dict) -> list[QuotaRow]:
    buckets = limits.get("rateLimitsByLimitId") or {"codex": limits.get("rateLimits", {})}
    rows = []
    for bucket_id, snapshot in buckets.items():
        prefix = f"{snapshot.get('limitName') or bucket_id} · " if len(buckets) > 1 else ""
        for key, fallback in (("primary", "短期額度"), ("secondary", "長期額度")):
            window = snapshot.get(key) or {}
            used = window.get("usedPercent")
            if used is not None:
                rows.append(QuotaRow(prefix + window_title(window.get("windowDurationMins"), fallback), used, window.get("resetsAt")))
    return rows


def remaining_percent(used: float) -> int:
    if used >= 100:
        return 0
    if used <= 0:
        return 100
    return min(99, max(1, math.floor(100 - used + 0.5)))


def bar(remaining: int) -> str:
    filled = 0 if remaining == 0 else 10 if remaining == 100 else min(9, max(1, math.floor(remaining / 10 + 0.5)))
    return FILLED_CELL * filled + EMPTY_CELL * (10 - filled)


def quota_line(row: QuotaRow) -> str:
    remaining = remaining_percent(row.used)
    reset = "" if row.resets_at is None else f"，<t:{row.resets_at}:R>重置"
    return f"{bar(remaining)} {remaining}%\u3000{row.name}{reset}"


@dataclass(frozen=True)
class LoginAttempt:
    id: str
    url: str
    code: str


@dataclass(frozen=True)
class Note:
    text: str
    failed: bool = False

    def render(self) -> str:
        return self.text if self.failed else f"-# {self.text}"


@dataclass(frozen=True)
class AccountButton:
    label: str
    operation: str | None = None
    style: discord.ButtonStyle = discord.ButtonStyle.secondary


REFRESH = AccountButton("重新整理", "refresh")


class AccountState(Enum):
    DISCONNECTED = (Level.BAD, "Codex 未連線", (REFRESH,))
    SIGNED_OUT = (Level.BAD, "尚未登入 ChatGPT", (AccountButton("登入 ChatGPT", "login", discord.ButtonStyle.primary), REFRESH))
    PENDING = (Level.WARN, "等待登入", (AccountButton("開啟登入頁"), AccountButton("取消登入", "cancel"), REFRESH))
    SIGNED_IN = (Level.OK, "已登入 ChatGPT，方案：{plan}", (AccountButton("登出", "logout"), REFRESH))
    UNKNOWN = (Level.UNKNOWN, "帳號狀態無法確認", (REFRESH,))

    def __init__(self, level: Level, status: str, buttons: tuple[AccountButton, ...]) -> None:
        self.level = level
        self.status = status
        self.buttons = buttons


def account_state(console: Console) -> AccountState:
    if not console.bot.codex.ready.is_set():
        return AccountState.DISCONNECTED
    if console.login_attempt is not None:
        return AccountState.PENDING
    if console.account is None:
        return AccountState.UNKNOWN
    return AccountState.SIGNED_IN if console.account.get("account") else AccountState.SIGNED_OUT


def account_status(console: Console) -> str:
    account = (console.account or {}).get("account") or {}
    return account_state(console).status.format(plan=account.get("planType") or "無資料")


def voice_status(settings: Settings) -> tuple[Level, str]:
    if settings.voice_hub_ids:
        return Level.OK, f"已啟用，{len(settings.voice_hub_ids)} 個入口"
    return Level.UNKNOWN, "未設定入口，功能停用中"


def instructions_text(instructions: str | None) -> str:
    if not instructions:
        return "-# 固定指示\n未設定"
    lines = [line.strip() for line in instructions.splitlines() if line.strip()][:3]
    preview = [discord.utils.escape_mentions(discord.utils.escape_markdown(line if len(line) <= 40 else line[:40] + "…")) for line in lines]
    return "\n".join(["-# 固定指示", f"已設定 {len(instructions)} 字，只套用到新對話", *(f"> {line}" for line in preview)])


@dataclass(frozen=True)
class Page:
    level: Level
    items: list[ui.Item]


@dataclass(frozen=True)
class Module:
    key: str
    name: str
    summary: Callable[[Console, Settings], str]
    page: Callable[[ConsolePanel, Settings], Page]


class InstructionsModal(ui.Modal):
    def __init__(self, console: Console, current: str | None) -> None:
        super().__init__(title="AI 指示", timeout=900)
        self.console = console
        self.text = ui.TextInput(label="給 AI 的固定指示，只套用到新對話", style=discord.TextStyle.paragraph, required=False, max_length=4000, default=current or "")
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not await self.console.bot.is_owner(interaction.user):  # ty: ignore[invalid-argument-type]
            await interaction.followup.send("只有擁有者能使用", ephemeral=True)
            return
        await self.console.bot.store.update_settings(instructions=self.text.value.strip() or None)
        await self.console.refresh_panels()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_event("console_interaction_error", error)


def account_section(console: Console) -> list[ui.Item]:
    state = account_state(console)
    attempt = console.login_attempt
    lines = [account_status(console)]
    if console.note is not None:
        lines.append(console.note.render())
    if state is AccountState.DISCONNECTED:
        lines.append(RECONNECTING)
    if state is AccountState.PENDING:
        lines += ["1. 按下「開啟登入頁」，用機器人專用的 ChatGPT 帳號登入", "2. 輸入下面的登入代碼", f"# {attempt.code}", "-# 完成後面板會自動更新"]
    if state is AccountState.SIGNED_IN:
        rows = quota_rows(console.limits)
        lines += [quota_line(row) for row in rows] if rows else ["-# 額度資料暫時無法取得"]
    actions = ui.ActionRow()
    for spec in state.buttons:
        if spec.operation is None:
            actions.add_item(ui.Button(label=spec.label, url=attempt.url if attempt is not None else None))
            continue
        button = ui.Button(label=spec.label, style=spec.style)

        async def callback(interaction: discord.Interaction, operation: str = spec.operation) -> None:
            await console.action(operation)

        button.callback = callback
        actions.add_item(button)
    return [ui.TextDisplay("\n".join(lines)), actions]


def targets_section(console: Console, settings: Settings) -> list[ui.Item]:
    channels = ui.ChannelSelect(channel_types=[discord.ChannelType.text], placeholder=NO_PLACEHOLDER, min_values=0, max_values=25, default_values=[discord.Object(value) for value in settings.ai_channel_ids])

    async def channel_callback(interaction: discord.Interaction) -> None:
        await console.bot.store.update_settings(ai_channel_ids=[value.id for value in channels.values])
        await console.refresh_panels()

    channels.callback = channel_callback
    roles = ui.RoleSelect(placeholder=NO_PLACEHOLDER, min_values=0, max_values=25, default_values=[discord.Object(value) for value in settings.allowed_role_ids])

    async def role_callback(interaction: discord.Interaction) -> None:
        await console.bot.store.update_settings(allowed_role_ids=[value.id for value in roles.values])
        await console.refresh_panels()

    roles.callback = role_callback
    return [ui.TextDisplay("-# 可使用的頻道"), ui.ActionRow(channels), ui.TextDisplay("-# 可使用的身分組"), ui.ActionRow(roles)]


def model_section(console: Console, settings: Settings) -> list[ui.Item]:
    models = console.models
    model, _ = choose_model(models, settings.model, settings.effort)
    default_model, _ = choose_model(models, None, None)
    if model is None or default_model is None:
        return []
    default_effort = model.get("defaultReasoningEffort")
    model_select = ui.Select(placeholder=f"預設：{default_model['displayName']}", min_values=0, max_values=1, options=[discord.SelectOption(label=value["displayName"], value=value["id"], description=(value.get("description") or "")[:100] or None, default=value["id"] == settings.model) for value in models[:25]])

    async def model_callback(interaction: discord.Interaction) -> None:
        selected = model_select.values[0] if model_select.values else None
        current = await console.bot.store.settings()
        _, effort = choose_model(console.models, selected, current.effort)
        await console.bot.store.update_settings(model=selected, effort=current.effort if effort == current.effort else None)
        await console.refresh_panels()

    model_select.callback = model_callback
    effort_select = ui.Select(placeholder=f"預設：{EFFORT_NAMES.get(default_effort, default_effort)}", min_values=0, max_values=1, options=[discord.SelectOption(label=EFFORT_NAMES.get(value["reasoningEffort"], value["reasoningEffort"]), value=value["reasoningEffort"], description=(value.get("description") or "")[:100] or None, default=value["reasoningEffort"] == settings.effort) for value in model.get("supportedReasoningEfforts", [])[:25]])

    async def effort_callback(interaction: discord.Interaction) -> None:
        await console.bot.store.update_settings(effort=effort_select.values[0] if effort_select.values else None)
        await console.refresh_panels()

    effort_select.callback = effort_callback
    return [ui.Separator(), ui.TextDisplay("-# 模型"), ui.ActionRow(model_select), ui.TextDisplay("-# 思考強度"), ui.ActionRow(effort_select)]


def instructions_section(console: Console, settings: Settings) -> list[ui.Item]:
    edit = ui.Button(label="編輯", style=discord.ButtonStyle.secondary, custom_id="instructions")

    async def edit_callback(interaction: discord.Interaction) -> None:
        current = await console.bot.store.settings()
        await interaction.response.send_modal(InstructionsModal(console, current.instructions))

    edit.callback = edit_callback
    return [ui.Section(ui.TextDisplay(instructions_text(settings.instructions)), accessory=edit)]


def ai_page(panel: ConsolePanel, settings: Settings) -> Page:
    console = panel.console
    items = [*account_section(console), ui.Separator(), *targets_section(console, settings), *model_section(console, settings), ui.Separator(), *instructions_section(console, settings)]
    return Page(account_state(console).level, items)


def voice_page(panel: ConsolePanel, settings: Settings) -> Page:
    console = panel.console
    level, status = voice_status(settings)
    hubs = ui.ChannelSelect(channel_types=[discord.ChannelType.voice], placeholder=NO_PLACEHOLDER, min_values=0, max_values=25, default_values=[discord.Object(value) for value in settings.voice_hub_ids])

    async def hub_callback(interaction: discord.Interaction) -> None:
        await console.bot.store.update_settings(voice_hub_ids=[value.id for value in hubs.values])
        await console.refresh_panels()
        if interaction.guild is not None:
            await console.bot.voice.settle(interaction.guild)

    hubs.callback = hub_callback
    return Page(level, [ui.TextDisplay(status), ui.TextDisplay("-# 入口頻道"), ui.ActionRow(hubs)])


MODULES = {
    module.key: module
    for module in (
        Module("ai", "AI 對話", lambda console, settings: account_status(console), ai_page),
        Module("voice", "臨時語音", lambda console, settings: voice_status(settings)[1], voice_page),
    )
}


class ConsolePanel(ui.LayoutView):
    def __init__(self, console: Console, interaction: discord.Interaction) -> None:
        super().__init__(timeout=None)
        self.console = console
        self.interaction = interaction
        self.module = "ai"
        self.deadline = interaction.created_at + PANEL_LIFETIME
        self.expires_at = int(self.deadline.timestamp())
        self.expiry: asyncio.Task | None = None
        self.render_lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (interaction.data or {}).get("custom_id") == "instructions":
            if not await self.console.bot.is_owner(interaction.user):  # ty: ignore[invalid-argument-type]
                await interaction.response.send_message("只有擁有者能使用", ephemeral=True)
                return False
            return True
        await interaction.response.defer()
        if not await self.console.bot.is_owner(interaction.user):  # ty: ignore[invalid-argument-type]
            await interaction.followup.send("只有擁有者能使用", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item) -> None:
        log_event("console_interaction_error", error)

    async def expire(self) -> None:
        await discord.utils.sleep_until(self.deadline)
        self.stop()
        self.console.panels.discard(self)
        try:
            async with self.render_lock:
                await self.interaction.delete_original_response()
        except discord.HTTPException as error:
            log_event("console_panel_error", error)

    async def rebuild(self) -> None:
        settings = await self.console.bot.store.settings()
        self.clear_items()
        page = MODULES[self.module].page(self, settings)
        switch = ui.Select(options=[discord.SelectOption(label=module.name, value=module.key, description=module.summary(self.console, settings)[:100], default=module.key == self.module) for module in MODULES.values()])

        async def switch_callback(interaction: discord.Interaction) -> None:
            self.module = switch.values[0]
            await self.refresh()

        switch.callback = switch_callback
        self.add_item(ui.Container(ui.TextDisplay("## 控制台"), ui.ActionRow(switch), *page.items, ui.Separator(), ui.TextDisplay(f"-# 面板在 <t:{self.expires_at}:R>失效"), accent_colour=page.level.value))

    async def refresh(self) -> None:
        try:
            async with self.render_lock:
                if self.is_finished():
                    return
                await self.rebuild()
                await self.interaction.edit_original_response(view=self)
        except discord.HTTPException as error:
            log_event("console_panel_error", error)
            self.console.panels.discard(self)


class Console:
    def __init__(self, bot: HoroBot) -> None:
        self.bot = bot
        self.panels: set[ConsolePanel] = set()
        self.login_attempt: LoginAttempt | None = None
        self.account: dict | None = None
        self.limits: dict = {}
        self.models: list[dict] = []
        self.note: Note | None = None
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
            if panel.expiry is not None:
                panel.expiry.cancel()
        self.panels.clear()

    async def open(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self.bot.is_owner(interaction.user):  # ty: ignore[invalid-argument-type]
            await interaction.edit_original_response(content="只有擁有者能使用")
            return
        async with self.lock:
            try:
                await self._read_account()
            except Exception as error:
                log_event("console_account_error", error)
                self.note = Note("暫時無法讀取帳號狀態，請稍後重新整理。", failed=True)
        panel = ConsolePanel(self, interaction)
        await panel.rebuild()
        await interaction.edit_original_response(view=panel)
        panel.expiry = asyncio.create_task(panel.expire())
        self.panels.add(panel)

    async def _read_account(self) -> None:
        self.account = None
        self.limits = {}
        self.models = []
        if not self.bot.codex.ready.is_set():
            return
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
                    self.note = None
                elif operation == "logout":
                    await self.bot.codex.logout()
                    self.note = Note("已登出。")
                elif operation == "cancel":
                    self.note = Note("已取消登入。")
                else:
                    self.note = None
                await self._read_account()
            except Exception as error:
                log_event("console_action_error", error)
                self.note = Note("操作失敗，請稍後重新整理。", failed=True)
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
                        self.note = Note("登入完成。") if params.get("success") and self.account.get("account") is not None else Note("登入未完成，請重新登入。", failed=True)
                    await self.refresh_panels()
                elif method == "account/rateLimits/updated" and self.panels:
                    async with self.lock:
                        self.limits = await self.bot.codex.rate_limits()
                    await self.refresh_panels()
                elif method in ("client/connected", "client/disconnected", "account/updated"):
                    async with self.lock:
                        await self._read_account()
                    await self.refresh_panels()
            except Exception as error:
                log_event("console_update_error", error)
