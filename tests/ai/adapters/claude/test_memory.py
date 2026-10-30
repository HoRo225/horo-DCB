import unittest

from src.ai.adapters.claude.memory import ConversationMemory

KEY = "guild:1:channel:2:user:3"
THREAD = "guild:1:thread:9"


def turn(user: str, assistant: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.memory = ConversationMemory(max_turns=3, max_chars=20, idle_seconds=100, clock=lambda: self.now)

    def test_history_alternates_user_and_assistant_oldest_first(self) -> None:
        self.memory.append(KEY, "問一", "答一")
        self.memory.append(KEY, "問二", "答二")
        self.assertEqual(self.memory.history(KEY), turn("問一", "答一") + turn("問二", "答二"))
        self.assertEqual(self.memory.history(THREAD), [])

    def test_only_the_newest_turns_are_kept(self) -> None:
        for n in range(5):
            self.memory.append(KEY, f"問{n}", f"答{n}")
        self.assertEqual(self.memory.history(KEY), turn("問2", "答2") + turn("問3", "答3") + turn("問4", "答4"))

    def test_character_budget_drops_whole_turns_from_the_oldest(self) -> None:
        self.memory.append(KEY, "一" * 8, "二" * 8)
        self.memory.append(KEY, "三" * 4, "四" * 4)
        self.memory.append(KEY, "五" * 4, "六" * 4)
        self.assertEqual(self.memory.history(KEY), turn("三" * 4, "四" * 4) + turn("五" * 4, "六" * 4))

    def test_idle_conversations_are_forgotten(self) -> None:
        self.memory.append(KEY, "舊", "舊答")
        self.now = 60
        self.memory.append(THREAD, "新", "新答")
        self.now = 100
        self.assertEqual(self.memory.history(KEY), [])
        self.assertEqual(self.memory.history(THREAD), turn("新", "新答"))
        self.assertEqual(len(self.memory), 1)

    def test_forget_where_sees_owner_and_parent(self) -> None:
        self.memory.append(KEY, "a", "b")
        self.memory.append(THREAD, "c", "d", parent_channel_id=2)
        self.assertEqual(self.memory.forget_where(lambda c: c.user_id == 3), 1)
        self.assertEqual(self.memory.forget_where(lambda c: c.user_id == 3), 0)
        self.assertEqual(self.memory.forget_where(lambda c: c.parent_channel_id == 2), 1)
        self.assertEqual(len(self.memory), 0)


if __name__ == "__main__":
    unittest.main()
