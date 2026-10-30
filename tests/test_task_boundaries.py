import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SUPERVISOR = "core/app/supervisor.py"
SCOPED_CHILDREN = {
    "ai/adapters/claude/service.py": "clewdr reachability poll owned by ClaudeService.start and cancelled in close",
    "ai/adapters/discord/request.py": "typing indicator scoped to one request and cancelled when it ends",
    "ai/adapters/discord/output.py": "progress card reply and edit loop scoped to one AiProgress; late replies go to its cleanup group",
    "core/adapters/discord/watchdog.py": "lag probe scoped to run_watchdog and cancelled when it returns",
}


def constructs_groups(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call) and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "TaskGroup"
        for node in ast.walk(tree)
    )


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
            if spawns(ast.parse(path.read_text(encoding="utf-8")))
        }
        self.assertEqual(found, {SUPERVISOR, *SCOPED_CHILDREN})

    def test_every_task_group_comes_from_the_supervisor_so_shutdown_sees_it(self) -> None:
        found = {
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if constructs_groups(ast.parse(path.read_text(encoding="utf-8")))
        }
        self.assertEqual(found, {SUPERVISOR})

    def test_detector_sees_every_spelling(self) -> None:
        for source in ("asyncio.create_task(x())", "asyncio.ensure_future(x())", "loop.create_task(x())",
                       "asyncio.get_running_loop().create_task(x())"):
            with self.subTest(source):
                self.assertEqual(spawns(ast.parse(source)), 1)
        self.assertEqual(spawns(ast.parse("group.create_task(x())")), 0)
        for source in ("TaskGroup('games', WORK)", "supervisor.TaskGroup('x', SCHEDULED)"):
            with self.subTest(source):
                self.assertTrue(constructs_groups(ast.parse(source)))
        self.assertFalse(constructs_groups(ast.parse("supervisor.group('games', WORK)")))


if __name__ == "__main__":
    unittest.main()
