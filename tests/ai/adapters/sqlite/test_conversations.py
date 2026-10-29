import unittest

from src.ai.adapters.sqlite.conversations import Conversation, ConversationStore
from src.core.adapters.sqlite.db import open_db

THREAD_KEY = "guild:1:thread:5"
USER_KEY = "guild:1:channel:2:user:3"


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.store = ConversationStore(db)

    def test_bind_replaces_the_thread_and_its_parent(self) -> None:
        self.store.bind(THREAD_KEY, "t1", 2)
        self.store.bind(THREAD_KEY, "t2", 3)
        self.assertEqual(self.store.get(THREAD_KEY), Conversation("t2", 3))
        self.assertIsNone(self.store.get(USER_KEY))
        self.store.forget(THREAD_KEY)
        self.assertEqual(len(self.store), 0)

    def test_matching_follows_scope_rules(self) -> None:
        self.store.bind(USER_KEY, "a", None)
        self.store.bind(THREAD_KEY, "b", 2)
        self.store.bind("guild:1:thread:6", "c", 4)
        self.store.bind("guild:11:channel:2:user:3", "d", None)
        self.assertEqual(sorted(self.store.matching(1, 2, include_children=True)), [USER_KEY, THREAD_KEY])
        self.assertEqual(self.store.matching(1, 5), [THREAD_KEY])
        self.assertEqual(len(self.store.matching(1)), 3)

    def test_detach_moves_threads_to_pending_until_confirmed(self) -> None:
        self.store.bind(USER_KEY, "a", None)
        self.store.bind(THREAD_KEY, "b", 2)
        self.assertEqual(self.store.detach([USER_KEY, USER_KEY, THREAD_KEY, "guild:1:thread:9"]), ["a", "b"])
        self.assertEqual(len(self.store), 0)
        self.assertEqual(self.store.pending_archives(), ["a", "b"])
        self.store.archive_confirmed("a")
        self.assertEqual(self.store.pending_archives(), ["b"])


if __name__ == "__main__":
    unittest.main()
