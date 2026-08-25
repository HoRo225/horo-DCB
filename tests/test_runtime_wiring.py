from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.bot import HoroBot, main


class RuntimeWiringTest(unittest.TestCase):
    def test_semantic_memory_can_be_disabled_without_changing_other_services(self):
        config = SimpleNamespace(
            ninerouter_url="http://9router:20128/v1",
            ninerouter_model="configured-model",
            web_search_provider="configured-search",
            image_search_provider="configured-images",
            web_fetch_provider="configured-fetch",
            embedding_model="configured-embedding",
            embedding_dimensions=4,
            semantic_memory_enabled=False,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
            ai_text_display_enabled=False,
        )
        setattr(config, "discord_" + "token", "configured")
        setattr(config, "ninerouter_" + "api_" + "key", "configured")

        with (
            patch("src.bot.AppConfig.from_env", return_value=config) as from_env,
            patch("src.bot.AIClient") as ai_client_class,
            patch("src.bot.SemanticMemory") as semantic_memory_class,
            patch("src.bot.TempVoiceManager") as temp_voice_class,
            patch("src.bot.SteamFreeGamesNotifier") as steam_class,
            patch("src.bot.CalendarManager") as calendar_class,
            patch("src.bot.AgentTools") as agent_tools_class,
            patch("src.bot.ChatManager") as chat_class,
            patch("src.bot.HoroBot") as bot_class,
        ):
            main()

        from_env.assert_called_once_with()
        ai_client_class.assert_called_once_with(
            config.ninerouter_url,
            getattr(config, "ninerouter_" + "api_" + "key"),
            config.ninerouter_model,
        )
        semantic_memory_class.assert_not_called()
        calendar_class.assert_called_once_with()
        agent_tools_class.assert_called_once_with(
            steam_class.return_value,
            ai_client_class.return_value,
            semantic_memory=None,
            calendar=calendar_class.return_value,
            search_provider=config.web_search_provider,
            image_search_provider=config.image_search_provider,
            fetch_provider=config.web_fetch_provider,
        )
        bot_class.assert_called_once_with(
            chat_class.return_value,
            temp_voice_class.return_value,
            steam_class.return_value,
            semantic_memory=None,
            calendar=calendar_class.return_value,
            ai_text_display_enabled=False,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
        )
        bot_class.return_value.run.assert_called_once_with(
            getattr(config, "discord_" + "token"),
            log_handler=None,
        )

    def test_horo_bot_does_not_discover_semantic_memory_implicitly(self):
        hidden_memory = object()
        chat = SimpleNamespace(
            agent_tools=SimpleNamespace(semantic_memory=hidden_memory),
        )

        bot = HoroBot(chat, SimpleNamespace(), SimpleNamespace())

        self.assertIsNone(bot.semantic_memory)


if __name__ == "__main__":
    unittest.main()
