from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.ai.domain.conversation import MAX_PROMPT_CHARACTERS


@dataclass(frozen=True, slots=True)
class Quote:
    author: str
    text: str


CHAT_INSTRUCTIONS = """玩法摘要（說明玩法以這裡和查詢結果為準；沒寫到的就說不確定，不要編造數字或規則）：
- 遊戲：21 點、拉霸跟 Bot 玩，純娛樂、不下注、沒有點數，要到遊戲頻道玩。21 點只能叫牌、停牌，結果分贏、Blackjack、平手、輸、爆牌；拉霸只看連線與倍數。
- 臨時語音：進語音入口頻道會自動開一個自己的語音頻道，沒人後自動清掉。
- Steam 通知：Steam 有限時免費遊戲時發到通知頻道。
- 管理：管理員用 /控制台 調整 AI、遊戲、臨時語音、Steam 通知等設定。
- 在 AI 頻道直接跟 Bot 說就能聊天。21 點、拉霸要到遊戲頻道；說「說明」會出功能卡。

預設以繁體中文（台灣用語）回答，即使問題、引文或先前回覆使用其他語言。
只有使用者明確指定其他輸出語言或翻譯目標時才改用該語言。
謎語、雙關、笑話等文字遊戲也用繁體中文，改用在中文裡成立的版本。
程式碼、命令、路徑、識別名稱及必要引文保留原樣。

回覆會顯示在 Discord，以下格式規則優先於其他格式指引（包括 GitHub Markdown、表格、圖表與視覺化的建議）：
- 只用 Discord 支援的 Markdown：**粗體**、*斜體*、__底線__、~~刪除線~~、`行內程式碼`、標明語言的程式碼區塊、> 引言、- 或 1. 清單、[文字](網址) 連結。
- 長回答用 ## 或 ### 標題分段；短回答直接回答，不加標題。
- 來源與附註放在回覆最後，以 -# 開頭的小字行呈現，例如：-# 來源：[名稱](網址)
- 謎題答案、劇情雷等劇透內容用 ||文字|| 包起來。
- 不要使用表格（即使使用者要求，也改用條列或「項目：說明」）、#### 以下標題、--- 分隔線、HTML、LaTeX、註腳、核取清單、Mermaid 或其他圖表語法。
- Discord 會照字面顯示空行：標題與清單前不必空行，段落之間最多空一行。"""


ASK = "\n\n【本次問題】\n"
REPLIED = "被回覆的訊息"
CHAIN_NOTE = "（以下是這串回覆，依時間由舊到新排列，編號越小越早；【被回覆的訊息】是我這次直接回覆的那則。）"


def _block(label: str, author: str, text: str) -> str:
    return f"【{label}】{author}：\n{text}"


def chat_prompt(
    question: str,
    chain: Sequence[Quote],
    *,
    referenced_image_count: int = 0,
    current_image_count: int = 0,
    media_note: str = "",
) -> str:
    total_images = referenced_image_count + current_image_count
    if referenced_image_count and current_image_count:
        note = (
            f"\n\n（圖片輸入順序：被回覆訊息 {referenced_image_count} 張在前、"
            f"本次訊息 {current_image_count} 張在後。）"
        )
    elif referenced_image_count:
        note = f"\n\n（圖片輸入來自被回覆訊息，共 {referenced_image_count} 張。）"
    else:
        note = ""
    note += media_note

    if not chain:
        question = question or ("請說明我提供的內容。" if total_images else "")
        return question[: max(0, MAX_PROMPT_CHARACTERS - len(note))] + note

    *earlier, replied = chain
    preface = f"{CHAIN_NOTE}\n\n" if earlier else ""
    head = _block(REPLIED, replied.author, "")
    question = (question or "請說明我回覆的內容。")[
        : max(0, MAX_PROMPT_CHARACTERS - len(preface) - len(head) - len(ASK) - len(note))
    ]
    room = max(0, MAX_PROMPT_CHARACTERS - len(preface) - len(ASK) - len(question) - len(note))
    blocks = [_block(REPLIED, replied.author, replied.text)[:room]]
    room -= len(blocks[0])
    for n, quote in reversed(list(enumerate(earlier, 1))):
        block = _block(f"回覆鏈 {n}", quote.author, quote.text)
        room -= len(block) + 2
        if room < 0:
            break
        blocks.insert(0, block)
    return (preface if len(blocks) > 1 else "") + "\n\n".join(blocks) + ASK + question + note
