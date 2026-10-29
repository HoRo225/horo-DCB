from __future__ import annotations

from collections.abc import Callable, Collection

from src.games.domain import slots

IDLE_SECONDS = 10 * 60
NO_LOBBY = "遊戲大廳尚未開放，請等管理員設定遊戲頻道。"
NO_IDENTITY = "目前無法確認你的伺服器身分，請稍後再試。"
NO_ROLE = "你沒有使用這個功能的身分組。"
TABLE_RULES = (
    "- 每人一張賭桌，開新桌會刪掉舊桌。\n"
    "- Bot 重新啟動時會收掉所有賭桌，請重新開桌。\n"
)
BLACKJACK_BRIEF = (
    "玩法　叫牌或停牌，比{name}更接近 21 點就贏\n"
    "結果　超過 21 點爆牌 · 起手 21 點是 Blackjack · 同點平手"
)
BLACKJACK_SECTIONS = (
    ("玩法", "- 叫牌：再拿一張牌，超過 21 點就爆牌輸掉；拿到 21 點會自動停牌。\n"
             "- 停牌：不再拿牌，換{name}補牌。"),
    ("結果", "- Blackjack：起手兩張就是 21 點；{name}也是 Blackjack 時算平手。\n"
             "- 比點數：比{name}接近 21 點就贏，同點平手；{name}爆牌時你贏。"),
    ("發牌", "- 6 副牌、每局重洗；{name}軟 17 停牌，明牌 A 或 10 會先偷看底牌。"),
    ("賭桌", TABLE_RULES + f"- 閒置 {IDLE_SECONDS // 60} 分鐘會自動關桌，進行中的那局以停牌結算。"),
)
SLOT_BRIEF = (
    f"盤面　3×3 共 {len(slots.LINES)} 條線，一條線三格相同就連線\n"
    f"倍數　一條線最高 ×{max(slots.PAYS.values())} · 各線倍數相加"
)


def blackjack_rules(symbol: Callable[[str], str]) -> list[tuple[str, str]]:
    return list(BLACKJACK_SECTIONS)


def slot_rules(symbol: Callable[[str], str]) -> list[tuple[str, str]]:
    return [
        ("倍數表", "一條線三格相同：\n"
                   + "\n".join(f"- {symbol(name) * 3}　×{pay}" for name, pay in slots.PAYS.items())
                   + f"\n- 前兩格是 {symbol('cherry')}{symbol('cherry')}、第三格不是：×{slots.CHERRY_PAIR}"
                   "（橫線、斜線從左數，直線從上數）"),
        ("連線", f"- 3×3 盤面共 {len(slots.LINES)} 條線：3 橫、3 直、2 斜，各線倍數相加。"),
        ("賭桌", TABLE_RULES + f"- 閒置 {IDLE_SECONDS // 60} 分鐘會自動關桌。"),
    ]


def play_problem(
    channel_id: int | None, games_channel_id: int | None, role_ids: Collection[int] | None, required: Collection[int],
) -> str | None:
    if games_channel_id is None:
        return NO_LOBBY
    if channel_id != games_channel_id:
        return f"請到 <#{games_channel_id}> 使用遊戲大廳。"
    if role_ids is None:
        return NO_IDENTITY
    if required and not set(role_ids).intersection(required):
        return NO_ROLE
    return None
