import unittest

from src.ai.domain.conversation import MAX_REPLY_IMAGES, conversation_key, normalize_reply_image_urls, scope_matches

THREAD_KEY = conversation_key(1, 5, 3, is_thread=True)
USER_KEY = conversation_key(1, 2, 3, is_thread=False)


class ConversationKeyTests(unittest.TestCase):
    def test_threads_are_shared_and_channels_are_per_user(self) -> None:
        self.assertEqual(conversation_key(1, 5, 3, is_thread=True), conversation_key(1, 5, 4, is_thread=True))
        self.assertNotEqual(conversation_key(1, 2, 3, is_thread=False), conversation_key(1, 2, 4, is_thread=False))
        self.assertEqual((THREAD_KEY, USER_KEY), ("guild:1:thread:5", "guild:1:channel:2:user:3"))


class ScopeMatchesTests(unittest.TestCase):
    def test_guild_scope_covers_every_conversation_in_that_guild(self) -> None:
        self.assertTrue(scope_matches(USER_KEY, 1))
        self.assertTrue(scope_matches(THREAD_KEY, 1))
        self.assertFalse(scope_matches(USER_KEY, 9))
        self.assertFalse(scope_matches(conversation_key(11, 2, 3, is_thread=False), 1))

    def test_channel_scope_covers_its_users_and_its_own_thread(self) -> None:
        self.assertTrue(scope_matches(USER_KEY, 1, 2))
        self.assertFalse(scope_matches(USER_KEY, 1, 4))
        self.assertTrue(scope_matches(THREAD_KEY, 1, 5))
        self.assertFalse(scope_matches(THREAD_KEY, 1, 2))

    def test_children_follow_the_known_parent(self) -> None:
        self.assertTrue(scope_matches(THREAD_KEY, 1, 2, include_children=True))
        self.assertTrue(scope_matches(THREAD_KEY, 1, 2, parent_channel_id=2, include_children=True))
        self.assertFalse(scope_matches(THREAD_KEY, 1, 2, parent_channel_id=7, include_children=True))
        self.assertFalse(scope_matches(conversation_key(1, 7, 3, is_thread=False), 1, 2, include_children=True))


class ReplyImageTests(unittest.TestCase):
    def test_keeps_unique_https_urls_up_to_the_limit(self) -> None:
        urls = [f"https://img.example/{index}.png" for index in range(MAX_REPLY_IMAGES + 2)]
        rejected = ["http://img.example/a.png", "https://user:pw@img.example/a.png", "https://img.example:8443/a.png",
                    "https://img.example/a b.png", "", 5]
        self.assertEqual(normalize_reply_image_urls([*rejected, urls[0], *urls]), tuple(urls[:MAX_REPLY_IMAGES]))
        self.assertEqual(normalize_reply_image_urls("https://img.example/a.png"), ())


if __name__ == "__main__":
    unittest.main()
