from __future__ import annotations
from src.core.adapters.discord.channels import interaction_operation_id

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from src.admin.pages.common import footer_note, spacer
from src.ai import (
    DEFAULT_MODEL_SETTINGS,
    ModelChoice,
    ModelInfo,
    ModelSettings,
    find_model,
)
from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS

if TYPE_CHECKING:
    from src.admin.panel import AdminPanelView

EFFORT_LABELS = {
    "none": "無",
    "minimal": "極低",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "極高",
    "max": "最高",
    "ultra": "極致",
}
MODEL_PAGE_SIZE = 24
UNSAVED_NOTE = ("info", "草稿尚未儲存；按「儲存設定」後套用於後續請求。")


@dataclass(slots=True)
class ModelEditor:
    catalog: tuple[ModelInfo, ...] = ()
    draft: ModelSettings | None = None
    notice: tuple[str, str] | None = None
    pages: dict[str, int] = field(default_factory=lambda: {"primary": 0, "fallback": 0})


def last_page(catalog: tuple[object, ...]) -> int:
    return max(0, (len(catalog) - 1) // MODEL_PAGE_SIZE)

async def load_models(view: AdminPanelView, operation: int) -> bool:
    editor = view.state(ModelEditor)
    if not view.is_current(operation):
        return False
    try:
        catalog = await view.codex_client.get_models()
    except Exception:
        logging.exception("Admin panel failed to load model catalog.")
        catalog = ()
    if not view.is_current(operation):
        return False
    editor.catalog = catalog
    try:
        saved = view.ai_settings.models
    except sqlite3.Error, ValueError:
        saved = DEFAULT_MODEL_SETTINGS
    if editor.draft is None:
        editor.draft = saved
    editor.notice = (
        ("warn", "目前無法取得模型清單；未變更已儲存設定。")
        if not catalog
        else UNSAVED_NOTE
        if editor.draft != saved
        else None
    )
    for side in editor.pages:
        editor.pages[side] = min(editor.pages[side], last_page(catalog))
    return True


def model_setting_select(view: AdminPanelView, side: str, kind: str) -> discord.ui.Select:
    editor = view.state(ModelEditor)
    choice = getattr(editor.draft, side)
    if kind == "model":
        start = editor.pages[side] * MODEL_PAGE_SIZE
        options = [
            discord.SelectOption(
                label=info.display_name[:100],
                description=info.model[:100],
                value=str(index),
                default=choice is not None and choice.model == info.model,
            )
            for index, info in enumerate(editor.catalog[start : start + MODEL_PAGE_SIZE], start)
        ]
        if side == "fallback":
            options.insert(
                0, discord.SelectOption(label="停用備援", value="off", default=choice is None)
            )
        placeholder = "選擇主模型" if side == "primary" else "選擇備援模型（可停用）"
        disabled = not editor.catalog
    else:
        info = find_model(editor.catalog, choice.model if choice else None)
        if info is not None and info.default_effort in info.supported_efforts:
            options = [
                discord.SelectOption(
                    label=EFFORT_LABELS.get(effort, effort)[:100],
                    description=effort[:100],
                    value="default" if effort == info.default_effort else str(index),
                    default=(choice.effort or info.default_effort) == effort,
                )
                for index, effort in enumerate(info.supported_efforts)
            ]
        else:
            options = [discord.SelectOption(label="模型預設", value="default", default=True)]
        placeholder = "選擇主模型推理強度" if side == "primary" else "選擇備援模型推理強度"
        disabled = not editor.catalog or info is None
    if not options:
        options = [discord.SelectOption(label="無可用模型", value="unavailable")]
    item = view.control(
        ("model_setting", side, kind),
        discord.ui.Select,
        lambda item, interaction: handle_model_select(view, interaction, side, kind, item.values),
    )
    item.options, item.placeholder, item.disabled = options, placeholder, disabled
    return item


async def handle_model_select(
    view: AdminPanelView,
    interaction: discord.Interaction,
    side: str,
    kind: str,
    values: list[str],
) -> None:
    if not values:
        return
    value = values[0]
    editor = view.state(ModelEditor)

    async def work(operation: int) -> Callable[[], None] | None:
        if not editor.catalog or not _model_ready(view, operation):
            return None
        choice = getattr(editor.draft, side)
        if kind == "model":
            if side == "fallback" and value == "off":
                selected = None
            else:
                try:
                    index = int(value)
                except ValueError:
                    return None
                start = editor.pages[side] * MODEL_PAGE_SIZE
                if not start <= index < min(start + MODEL_PAGE_SIZE, len(editor.catalog)):
                    return None
                info = editor.catalog[index]
                effort = (
                    choice.effort if choice and choice.effort in info.supported_efforts else None
                )
                selected = ModelChoice(info.model, effort)
        else:
            info = find_model(editor.catalog, choice.model if choice else None)
            if info is None:
                return None
            if value == "default":
                effort = None
            else:
                try:
                    index = int(value)
                except ValueError:
                    return None
                if not 0 <= index < len(info.supported_efforts):
                    return None
                effort = info.supported_efforts[index]
            selected = ModelChoice(choice.model, effort)
        editor.draft = ModelSettings(
            selected if side == "primary" else editor.draft.primary,
            selected if side == "fallback" else editor.draft.fallback,
        )
        editor.notice = UNSAVED_NOTE
        return lambda: render_ai_models(view)

    await view.run(interaction, work)


def _model_ready(view: AdminPanelView, operation: int) -> bool:
    return (
        view.page == "ai_models"
        and view.state(ModelEditor).draft is not None
        and view.is_current(operation)
    )


async def save_models(view: AdminPanelView, interaction: discord.Interaction) -> None:
    editor = view.state(ModelEditor)

    async def work(operation: int) -> Callable[[], None] | None:
        if not _model_ready(view, operation) or not await load_models(view, operation):
            return None
        valid = bool(editor.catalog)
        for choice in (editor.draft.primary, editor.draft.fallback):
            if choice is None:
                continue
            info = find_model(editor.catalog, choice.model)
            valid = (
                valid
                and info is not None
                and (choice.effort is None or choice.effort in info.supported_efforts)
            )
        if not valid:
            editor.notice = ("warn", "模型或推理強度已失效，請重新選擇；已儲存設定未變更。")
        else:
            try:
                view.ai_settings.save_models(editor.draft, operation_id=interaction_operation_id(interaction))
            except sqlite3.Error, ValueError:
                editor.notice = ("warn", "設定無法儲存；原設定未變更。")
            else:
                editor.notice = ("ok", "已儲存全 Bot 模型設定；只影響後續接納的請求。")
        return lambda: render_ai_models(view)

    await view.run(interaction, work)


async def turn_model_page(
    view: AdminPanelView, interaction: discord.Interaction, side: str, step: int
) -> None:
    editor = view.state(ModelEditor)

    async def work(operation: int) -> Callable[[], None] | None:
        if not _model_ready(view, operation):
            return None
        editor.pages[side] = min(
            last_page(editor.catalog), max(0, editor.pages[side] + step)
        )
        return lambda: render_ai_models(view)

    await view.run(interaction, work)


def render_ai_models(view: AdminPanelView) -> None:
    editor = view.state(ModelEditor)
    children = view.header(view.bot_text(after="模型設定"), "ai_models")
    children.append(discord.ui.TextDisplay("全 Bot 共用；儲存後只影響後續接納的請求。"))
    pages = last_page(editor.catalog)
    for side, label in (("primary", "主模型"), ("fallback", "備援模型")):
        if side == "fallback":
            children.append(spacer())
        choice = getattr(editor.draft, side) if editor.draft is not None else None
        info = find_model(editor.catalog, choice.model if choice else None)
        if choice is None:
            status = "**停用**"
        else:
            effort = (
                choice.effort
                if choice.effort
                else info.default_effort
                if info is not None
                else "模型預設"
            )
            status = (
                f"**{discord.utils.escape_markdown(choice.model)}**"
                f" · 推理強度：{EFFORT_LABELS.get(effort, effort)}"
            )
            if info is None and editor.catalog:
                status += f" · {ICONS['warn']} 模型已失效"
        heading = f"{label} · 第 {editor.pages[side] + 1}/{pages + 1} 頁" if pages else label
        children.extend(
            (
                discord.ui.TextDisplay(f"### {heading}\n{status}"),
                discord.ui.ActionRow(model_setting_select(view, side, "model")),
                discord.ui.ActionRow(model_setting_select(view, side, "effort")),
            )
        )
        if pages:
            page = editor.pages[side]
            children.append(
                discord.ui.ActionRow(
                    view.button(
                        f"model_{side}_prev",
                        "上一頁",
                        on_click=lambda i, side=side: turn_model_page(view, i, side, -1),
                        disabled=page == 0,
                        emoji=ICONS["prev"],
                    ),
                    view.button(
                        f"model_{side}_next",
                        "下一頁",
                        on_click=lambda i, side=side: turn_model_page(view, i, side, 1),
                        disabled=page >= pages,
                        emoji=ICONS["next"],
                    ),
                )
            )
    children.extend((discord.ui.Separator(), *footer_note(editor.notice)))
    children.append(
        view.actions(
            view.button(
                "model_save",
                "儲存設定",
                on_click=lambda i: save_models(view, i),
                style=discord.ButtonStyle.primary,
                disabled=not editor.catalog,
                emoji=ICONS["save"],
            )
        )
    )
    view.set_container(*children, colour=BRAND_COLOUR, note=editor.notice)
