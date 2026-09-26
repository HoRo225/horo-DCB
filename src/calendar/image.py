from __future__ import annotations

import calendar as month_calendar
from collections.abc import Sequence
from functools import cache
from io import BytesIO
import logging

from PIL import Image, ImageDraw, ImageFont

from src.calendar.discord_models import event_local_time
from src.calendar.models import calendar_now

MONTH_IMAGE_FILENAME = "calendar-month.png"
FONT_DIR = "/usr/share/fonts/opentype/noto"
TC_FACE_INDEX = 3  # Noto CJK TTC face order: JP, KR, SC, TC, HK.

WIDTH = 800
MARGIN = 32
GAP = 8
CELL_HEIGHT = 76
HEADER_HEIGHT = 104
BACKGROUND = (250, 248, 243)
CELL = (236, 241, 237)
SAGE = (142, 170, 153)
SAGE_DARK = (86, 112, 97)
INK = (62, 74, 67)
MUTED = (122, 138, 128)
WEEKEND = (201, 132, 148)
WHITE = (255, 255, 255)


@cache
def _fonts() -> tuple[ImageFont.FreeTypeFont, ...] | None:
    try:
        return (
            ImageFont.truetype(f"{FONT_DIR}/NotoSansCJK-Bold.ttc", 40, index=TC_FACE_INDEX),
            ImageFont.truetype(f"{FONT_DIR}/NotoSansCJK-Regular.ttc", 22, index=TC_FACE_INDEX),
            ImageFont.truetype(f"{FONT_DIR}/NotoSansCJK-Bold.ttc", 28, index=TC_FACE_INDEX),
        )
    except OSError:
        logging.warning("找不到 Noto CJK 字型，行事曆看板改用純文字月曆。")
        return None


def render_month_png(events: Sequence[object]) -> bytes | None:
    fonts = _fonts()
    if fonts is None:
        return None
    title_font, label_font, day_font = fonts
    now = calendar_now()
    event_days = {
        local.day
        for event in events
        if (local := event_local_time(event)) is not None
        and (local.year, local.month) == (now.year, now.month)
    }
    weeks = month_calendar.Calendar(firstweekday=month_calendar.SUNDAY).monthdayscalendar(
        now.year, now.month,
    )
    cell_width = (WIDTH - 2 * MARGIN - 6 * GAP) / 7
    grid_top = MARGIN + HEADER_HEIGHT
    height = grid_top + len(weeks) * (CELL_HEIGHT + GAP) - GAP + MARGIN
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((MARGIN, MARGIN), f"{now.year} 年 {now.month} 月", font=title_font, fill=INK)
    for column, label in enumerate("日一二三四五六"):
        center_x = MARGIN + column * (cell_width + GAP) + cell_width / 2
        draw.text(
            (center_x, grid_top - 20), label, font=label_font, anchor="mm",
            fill=WEEKEND if column in (0, 6) else MUTED,
        )
    for row, week in enumerate(weeks):
        for column, day in enumerate(week):
            if day == 0:
                continue
            left = MARGIN + column * (cell_width + GAP)
            top = grid_top + row * (CELL_HEIGHT + GAP)
            today = day == now.day
            draw.rounded_rectangle(
                (left, top, left + cell_width, top + CELL_HEIGHT),
                radius=14,
                fill=SAGE if today else CELL,
                outline=SAGE_DARK if day in event_days else None,
                width=3,
            )
            draw.text(
                (left + cell_width / 2, top + CELL_HEIGHT / 2), str(day),
                font=day_font, anchor="mm", fill=WHITE if today else INK,
            )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
