import unittest

from src.ai.domain.formatting import discord_tables, reasoning_title, split_markdown

NOTICE = "\n\n-# 截斷"


class SplitMarkdownTests(unittest.TestCase):
    def test_short_text_is_single_chunk(self) -> None:
        self.assertEqual(split_markdown("hi", 100, 3, NOTICE), ["hi"])
        self.assertEqual(split_markdown("", 100, 3, NOTICE), [])

    def test_long_text_respects_limit(self) -> None:
        text = "word " * 200
        chunks = split_markdown(text, 100, 20, NOTICE)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_code_block_reopened_across_chunks(self) -> None:
        text = "intro\n```python\n" + "x = 1\n" * 40 + "```\nend"
        chunks = split_markdown(text, 80, 20, NOTICE)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
        self.assertTrue(chunks[0].endswith("\n```"), chunks[0])
        self.assertTrue(chunks[1].startswith("```python\n"), chunks[1])

    def test_truncates_at_max_chunks_with_the_notice(self) -> None:
        chunks = split_markdown("word " * 200, 100, 3, NOTICE)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(chunks[-1].endswith(NOTICE))
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))


class DiscordTablesTests(unittest.TestCase):
    def test_tables_become_bullets_outside_code(self) -> None:
        long_cell = "x" * 90
        text = (
            "前言\n| 方案 | 價格 | 速度 |\n|---|:--:|---|\n| **A** | 100 | 快 |\n| B | a\\|b | |\n"
            f"| C | {long_cell} | 慢 |\n| D | ||雷|| | 快 |\n答案：||梳子||\n答案：|| 梳子 ||\n"
            "```\n| a | b |\n|---|---|\n```"
        )
        self.assertEqual(
            discord_tables(text),
            "前言\n- **A**：價格 100 · 速度 快\n- **B**：價格 a|b\n"
            f"- **C**\n  - 價格：{long_cell}\n  - 速度：慢\n- **D**：價格 ||雷|| · 速度 快\n"
            "答案：||梳子||\n答案：|| 梳子 ||\n```\n| a | b |\n|---|---|\n```",
        )

    def test_rows_without_outer_pipes_and_empty_cells(self) -> None:
        text = "方案 | 價格 | 速度\n--- | --- | ---\nA | 100 | 快\nB || 慢\n結尾"
        self.assertEqual(discord_tables(text), "- **A**：價格 100 · 速度 快\n- **B**：速度 慢\n結尾")

    def test_malformed_tables_keep_their_header(self) -> None:
        for text in (
            "| a | b |\n|---|---|",
            "| a | b |\n|---|---|\n| 1 | 2 | 3 |",
            "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 | 5 |\n| 6 | 7 |",
        ):
            self.assertEqual(discord_tables(text), text)

    def test_spoiler_line_after_table_stays_text(self) -> None:
        text = "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n|| 梳子 ||"
        self.assertEqual(discord_tables(text), "- **1**：b 2 · c 3\n|| 梳子 ||")


class ReasoningSummaryTests(unittest.TestCase):
    def test_bold_heading_is_the_title(self) -> None:
        self.assertEqual(reasoning_title("**Checking code**\n\n正在追蹤呼叫路徑。"), "Checking code")
        self.assertEqual(reasoning_title("沒有標題的摘要"), "")
        self.assertEqual(reasoning_title("**粗體**接著文字"), "")

    def test_partial_and_placeholder_headings(self) -> None:
        self.assertEqual(reasoning_title("**檢查程"), "檢查程")
        self.assertEqual(reasoning_title("**檢查程式碼*"), "檢查程式碼")
        self.assertEqual(reasoning_title("**"), "")
        self.assertEqual(reasoning_title("**整理答案**\n\n<!-- -->"), "整理答案")
        self.assertEqual(reasoning_title("<!-- -->"), "")


if __name__ == "__main__":
    unittest.main()
