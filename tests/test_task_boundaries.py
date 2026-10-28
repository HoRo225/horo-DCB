import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SUPERVISOR = "core/app/supervisor.py"
SCOPED_CHILDREN = {
    "ai/background.py": "awaited at once so the request can tell its own cancellation from the caller's",
    "ai/discord.py": "typing indicator scoped to one request and cancelled when it ends",
    "ai/output.py": "progress card reply and edit loop scoped to one AiProgress; late replies go to its cleanup group",
    "codex/rpc.py": "connection internals drained by CodexService.close until integrations/codex moves",
    "codex/service.py": "connection internals drained by CodexService.close until integrations/codex moves",
    "codex/testing_server.py": "fake app-server used only by tests",
    "core/adapters/discord/watchdog.py": "lag probe scoped to run_watchdog and cancelled when it returns",
}


def spawns(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in {"create_task", "ensure_future"}:
            continue
        target = node.func.value
        if (
            isinstance(target, ast.Name) and (target.id == "asyncio" or "loop" in target.id)
            or isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute)
            and target.func.attr in {"get_running_loop", "get_event_loop"}
        ):
            count += 1
    return count


class TaskBoundaryTests(unittest.TestCase):
    def test_only_the_supervisor_and_scoped_children_create_tasks(self) -> None:
        found = {
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if not path.name.startswith("test_") and spawns(ast.parse(path.read_text(encoding="utf-8")))
        }
        self.assertEqual(found, {SUPERVISOR, *SCOPED_CHILDREN})

    def test_detector_sees_every_spelling(self) -> None:
        for source in ("asyncio.create_task(x())", "asyncio.ensure_future(x())", "loop.create_task(x())",
                       "asyncio.get_running_loop().create_task(x())"):
            with self.subTest(source):
                self.assertEqual(spawns(ast.parse(source)), 1)
        self.assertEqual(spawns(ast.parse("group.create_task(x())")), 0)


if __name__ == "__main__":
    unittest.main()
