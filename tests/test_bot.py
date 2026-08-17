import unittest

import horo_dcb.bot as bot_module


class CreateBotTests(unittest.TestCase):
    def test_privileged_intents_are_disabled_by_default(self) -> None:
        self.assertTrue(callable(getattr(bot_module, "create_bot", None)))

        bot = bot_module.create_bot()

        self.assertFalse(bot.intents.members)
        self.assertFalse(bot.intents.presences)
        self.assertFalse(bot.intents.message_content)


if __name__ == "__main__":
    unittest.main()
