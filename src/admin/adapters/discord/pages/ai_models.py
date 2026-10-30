from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from src.admin.adapters.discord.pages.common import PendingConfirm, Proceed, footer_note, spacer
from src.ai import DEFAULT_MODEL_SETTINGS, MODEL_CATALOG, ModelChoice, ModelSettings, StoreError, find_model
from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS
from src.core.adapters.discord.channels import interaction_operation_id

if TYPE_CHECKING:
    from src.admin.adapters.discord.panel import AdminPanelView

UNSAVED_MARK = "● 尚有未儲存的變更"


@dataclass(slots=True)
class ModelEditor:
    draft: ModelSettings | None = None
    notice: tuple[str, str] | None = None


def saved_models(view: AdminPanelView) -> ModelSettings:
    try:
        return view.services.ai.models
    except StoreError:
        return DEFAULT_MODEL_SETTINGS


def unsaved(view: AdminPanelView) -> bool:
    draft = view.state(ModelEditor).draft
    return draft is not None and draft != saved_models(view)


def leave_models(view: AdminPanelView, proceed: Proceed) -> PendingConfirm | None:
    if not unsaved(view):
        return None

    async def discard(interaction: discord.Interaction) -> None:
        view.state(ModelEditor).draft = None
        await proceed(interaction)

    return PendingConfirm(
        "捨棄未儲存的變更？",
        "這次選擇的模型不會套用，後續請求仍使用已儲存的設定。",
        "捨棄",
        discard,
        "繼續編輯",
    )


async def load_models(view: AdminPanelView, operation: int) -> bool:
    if not view.is_current(operation):
        return False
    editor = view.state(ModelEditor)
    if editor.draft is None:
        editor.draft = saved_models(view)
    editor.notice = None
    return True


def model_select(view: AdminPanelView, side: str) -> discord.ui.Select:
    choice = getattr(view.state(ModelEditor).draft, side)
    options = [
        discord.SelectOption(
            label=info.display_name,
            description=info.model,
            value=info.model,
            default=choice is not None and choice.model == info.model,
        )
        for info in MODEL_CATALOG
    ]
    if side == "fallback":
        options.insert(0, discord.SelectOption(label="停用備援", value="off", default=choice is None))
    item = view.control(
        ("model_setting", side),
        discord.ui.Select,
        lambda item, interaction: handle_model_select(view, interaction, side, item.values),
    )
    item.options = options
    item.placeholder = "選擇主模型" if side == "primary" else "選擇備援模型（可停用）"
    return item


async def handle_model_select(
    view: AdminPanelView,
    interaction: discord.Interaction,
    side: str,
    values: list[str],
) -> None:
    if not values:
        return
    value = values[0]
    editor = view.state(ModelEditor)

    async def work(operation: int) -> Callable[[], None] | None:
        if not _model_ready(view, operation):
            return None
        if side == "fallback" and value == "off":
            selected = None
        elif (info := find_model(MODEL_CATALOG, value)) is not None:
            selected = ModelChoice(info.model)
        else:
            return None
        editor.draft = ModelSettings(
            selected if side == "primary" else editor.draft.primary,
            selected if side == "fallback" else editor.draft.fallback,
        )
        editor.notice = None
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
        if not _model_ready(view, operation):
            return None
        try:
            view.services.ai.save_models(editor.draft, operation_id=interaction_operation_id(interaction))
        except StoreError, ValueError:
            editor.notice = ("warn", "設定無法儲存；原設定未變更。")
        else:
            editor.notice = ("ok", "已儲存全 Bot 模型設定；只影響後續接納的請求。")
        return lambda: render_ai_models(view)

    await view.run(interaction, work)


def render_ai_models(view: AdminPanelView) -> None:
    editor = view.state(ModelEditor)
    children = view.header(view.bot_text(after="模型設定"), "ai_models")
    marker = f"**{UNSAVED_MARK}**\n" if unsaved(view) else ""
    children.append(discord.ui.TextDisplay(f"{marker}全 Bot 共用；儲存後只影響後續接納的請求。"))
    for side, label in (("primary", "主模型"), ("fallback", "備援模型")):
        if side == "fallback":
            children.append(spacer())
        choice = getattr(editor.draft, side) if editor.draft is not None else None
        if choice is None:
            status = "**停用**"
        else:
            info = find_model(MODEL_CATALOG, choice.model)
            status = f"**{discord.utils.escape_markdown(info.display_name if info is not None else choice.model)}**"
        children.extend(
            (
                discord.ui.TextDisplay(f"### {label}\n{status}"),
                discord.ui.ActionRow(model_select(view, side)),
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
                emoji=ICONS["save"],
            )
        )
    )
    view.set_container(*children, colour=BRAND_COLOUR, note=editor.notice)
