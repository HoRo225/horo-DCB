import unittest

from src.ai.adapters.discord.help import help_view
from src.core.adapters.discord.brand import ICONS
from tests.support.testing import text


class HelpViewTests(unittest.TestCase):
    def test_help_names_the_bot_and_the_games_channel(self) -> None:
        rendered = text(help_view(10, "霍蘿"))
        self.assertTrue(rendered.startswith(f"## {ICONS['ai']} 霍蘿能幫你做的事\n-# 直接 @霍蘿 聊天\n"))
        self.assertIn("21 點、拉霸要到 <#10> 玩", rendered)
        rendered = text(help_view(None, "Ho_Ro"))
        self.assertIn("21 點、拉霸要到 遊戲頻道 玩", rendered)
        self.assertIn("直接 @Ho\\_Ro 聊天", rendered)


if __name__ == "__main__":
    unittest.main()
