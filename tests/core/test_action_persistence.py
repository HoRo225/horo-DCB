import unittest

from tests.support.actions import ActionHarness

DONE = '{"code":"done","ok":true,"text":"管理操作已完成。"}'
HELP = '{"code":"done","ok":true,"private_ref":"<ref>","text":"已查詢功能說明。"}'
NO_TABLE = '{"code":"no_table","ok":false,"text":"請先從遊戲大廳開桌。"}'
NO_HUB = '{"code":"publish","ok":false,"text":"請先選擇遊戲頻道。"}'


def receipt(request: str, *results: str) -> tuple:
    payload = '{"results":[' + ",".join(results) + '],"source_message_id":1000}'
    return (f"request:{request}:receipt", None, "action.receipt", "done", 10, 3000, "<nonce>", payload, None)


class ActionPersistenceGoldenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = ActionHarness(self)

    async def test_settings_plan_rows(self) -> None:
        self.harness.admin()
        await self.harness.execute(("ai_enabled", {"enabled": True}), ("ai_enabled", {"enabled": True}))
        self.assertEqual(self.harness.rows(), {
            "operations": [
                ("<req0>:0", "ai.enabled", '{"enabled":true}', "done", '{"ok":true}', None),
                ("<req0>:1", "ai.enabled", '{"enabled":true}', "done", '{"ok":true}', None),
            ],
            "effects": [receipt("<req0>", DONE, DONE)],
        })

    async def test_network_plan_rows_stop_at_the_failed_step(self) -> None:
        self.harness.admin()
        await self.harness.execute(("voice_sync", {}), ("publish_hub", {}))
        self.assertEqual(self.harness.rows(), {
            "operations": [
                ("<req0>:0", "action.network", '{"name":"voice_sync","params":{}}', "done", '{"ok":true}', None),
                ("<req0>:1", "action.network", '{"name":"publish_hub","params":{}}', "failed", NO_HUB, None),
            ],
            "effects": [receipt("<req0>", DONE, NO_HUB)],
        })

    async def test_private_query_rows(self) -> None:
        await self.harness.execute(("help", {}))
        self.assertEqual(self.harness.rows(), {
            "operations": [("<req0>:0", "action.query", '{"name":"help","params":{}}', "done", HELP, None)],
            "effects": [receipt("<req0>", HELP)],
        })

    async def test_game_plan_rows_keep_the_received_snapshot(self) -> None:
        await self.harness.execute(("deal", {}), game=self.harness.game_snapshot())
        self.assertEqual(self.harness.rows(), {
            "operations": [
                ("<req0>:0", "game_action",
                 '{"action":"deal","actor_id":1,"channel_id":10,"game":null,"guild_id":5,"state_token":"<token>"}',
                 "done", NO_TABLE, None),
            ],
            "effects": [receipt("<req0>", NO_TABLE)],
        })

    async def test_failed_preflight_writes_nothing(self) -> None:
        results = await self.harness.execute(("help", {}), ("ai_enabled", {"enabled": False}))
        self.assertEqual(results, [{"ok": False, "code": "forbidden", "text": "此操作僅限伺服器管理員使用。"}])
        self.assertEqual(self.harness.rows(), {"operations": [], "effects": []})


if __name__ == "__main__":
    unittest.main()
