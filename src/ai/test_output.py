from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.ai.message_context import build_codex_prompt
from src.ai.formatting import (
    DISCORD_MESSAGE_LIMIT,
    build_ai_native_image_links,
    discord_tables,
    split_markdown,
    truncation_notice,
)
from src.codex.protocol import MAX_PROMPT_CHARACTERS


class SplitMarkdownTests(unittest.TestCase):
    def test_short_text_is_single_chunk(self) -> None:
        self.assertEqual(split_markdown("hi", 100, 3), ["hi"])

    def test_long_text_respects_limit(self) -> None:
        text = "word " * 200
        chunks = split_markdown(text, 100, 20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_code_block_reopened_across_chunks(self) -> None:
        text = "intro\n```python\n" + "x = 1\n" * 40 + "```\nend"
        chunks = split_markdown(text, 80, 20)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
        self.assertTrue(chunks[0].endswith("\n```"), chunks[0])
        self.assertTrue(chunks[1].startswith("```python\n"), chunks[1])

    def test_truncates_at_max_chunks(self) -> None:
        chunks = split_markdown("word " * 200, 100, 3)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(chunks[-1].endswith(truncation_notice()))
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
        self.assertEqual(
            discord_tables(text),
            "- **A**：價格 100 · 速度 快\n- **B**：速度 慢\n結尾",
        )

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


class CodexPromptTests(unittest.TestCase):
    def test_long_question_without_reference(self) -> None:
        prompt = build_codex_prompt("a" * (MAX_PROMPT_CHARACTERS + 500), None)
        self.assertLessEqual(len(prompt), MAX_PROMPT_CHARACTERS)

    def test_reference_sections_within_limit(self) -> None:
        referenced = SimpleNamespace(content="r" * 3000, components=())
        prompt = build_codex_prompt("q" * 3000, referenced)
        self.assertIn("【被回覆的訊息】", prompt)
        self.assertIn("【本次問題】", prompt)
        self.assertLessEqual(len(prompt), MAX_PROMPT_CHARACTERS)

    def test_image_order_note(self) -> None:
        prompt = build_codex_prompt(
            "看看", None, referenced_image_count=1, current_image_count=2
        )
        self.assertIn("被回覆訊息 1 張在前", prompt)
        self.assertIn("本次訊息 2 張在後", prompt)

    def test_images_only_gets_default_question(self) -> None:
        prompt = build_codex_prompt("", None, current_image_count=1)
        self.assertTrue(prompt.startswith("請說明我提供的內容。"), prompt)


class ImageLinksTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(build_ai_native_image_links(()), "")

    def test_long_links_are_omitted_with_note(self) -> None:
        urls = tuple(f"https://cdn.example.com/{i}/" + "a" * 300 for i in range(10))
        content = build_ai_native_image_links(urls)
        self.assertLessEqual(len(content), DISCORD_MESSAGE_LIMIT)
        self.assertRegex(content, r"另有 \d+ 個圖片連結")


if __name__ == "__main__":
    unittest.main()
