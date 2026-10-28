import unittest

from src.voice.domain.rooms import Overwrite, RoomShape, RoomSpec, bot_problem, build_temp_voice_name, creation_notice

SHAPE = RoomShape("▍A 的語音-🔊", 7, (Overwrite("role", 3, 1024, 0), Overwrite("member", 9, 1048576, 0)), 5, 96000, None, 1)


class NameTests(unittest.TestCase):
    def test_names_collapse_whitespace_and_drop_unprintable_characters(self) -> None:
        self.assertEqual(build_temp_voice_name("  小 \n 明​ "), "▍小 明 的語音-🔊")

    def test_names_fall_back_and_fit_discord_limit(self) -> None:
        self.assertEqual(build_temp_voice_name("​"), "▍使用者 的語音-🔊")
        name = build_temp_voice_name("長" * 200)
        self.assertEqual(len(name), 100)
        self.assertTrue(name.endswith(" 的語音-🔊"))


class RecordTests(unittest.TestCase):
    def test_creation_inputs_keep_the_persisted_record_format(self) -> None:
        self.assertEqual(RoomSpec(9, 100, 10, SHAPE).inputs(), {
            "owner_id": 9, "guild_id": 100, "entry_channel_id": 10, "category_id": 7, "name": "▍A 的語音-🔊",
            "overwrites": [
                {"target_id": 9, "target_type": "member", "allow": 1048576, "deny": 0},
                {"target_id": 3, "target_type": "role", "allow": 1024, "deny": 0},
            ],
            "user_limit": 5, "bitrate": 96000, "rtc_region": None, "video_quality_mode": 1,
        })

    def test_a_room_matches_its_record_regardless_of_overwrite_order(self) -> None:
        recorded = RoomSpec(9, 100, 10, SHAPE).inputs()
        reordered = RoomShape(SHAPE.name, 7, tuple(reversed(SHAPE.overwrites)), 5, 96000, None, 1)
        self.assertTrue(reordered.matches(recorded))
        for changed in (
            RoomShape("別的", 7, SHAPE.overwrites, 5, 96000, None, 1),
            RoomShape(SHAPE.name, None, SHAPE.overwrites, 5, 96000, None, 1),
            RoomShape(SHAPE.name, 7, SHAPE.overwrites[:1], 5, 96000, None, 1),
            RoomShape(SHAPE.name, 7, SHAPE.overwrites, 5, 64000, None, 1),
        ):
            with self.subTest(changed=changed):
                self.assertFalse(changed.matches(recorded))


class TextTests(unittest.TestCase):
    def test_bot_problem_lists_missing_permissions(self) -> None:
        self.assertIsNone(bot_problem(()))
        self.assertEqual(bot_problem(("連線", "移動成員")), "Bot 缺少權限：連線、移動成員。")
        self.assertEqual(bot_problem(None), "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。")

    def test_creation_notice_mentions_the_member(self) -> None:
        self.assertEqual(creation_notice(1, "問題。"), "<@1>，無法建立你的語音房：問題。請通知管理員。")


if __name__ == "__main__":
    unittest.main()
