from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from graphlib import TopologicalSorter
from pathlib import Path


def loaded_modules(module: str) -> set[str]:
    code = f"import sys, {module}; print(*sys.modules, sep='\\n')"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return set(result.stdout.split())


class ImportBoundaryTests(unittest.TestCase):
    def test_runtime_imports_have_no_cycles(self) -> None:
        def runtime_nodes(node):
            if isinstance(node, ast.If) and (
                isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
                or isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
            ):
                children = node.orelse
            else:
                yield node
                children = ast.iter_child_nodes(node)
            for child in children:
                yield from runtime_nodes(child)

        root = Path(__file__).parent.parent
        graph = {}
        for path in (root / "src").rglob("*.py"):
            if path.name.startswith("test_") or path.name == "testing.py":
                continue
            module = ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")
            package = module if path.name == "__init__.py" else module.rpartition(".")[0]
            dependencies = set()
            for node in runtime_nodes(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    dependencies.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        prefix = package.split(".")[:len(package.split(".")) - node.level + 1]
                        base = ".".join([*prefix, *([base] if base else [])])
                    dependencies.add(base)
                    dependencies.update(f"{base}.{alias.name}" for alias in node.names)
            graph[module] = dependencies
        TopologicalSorter({module: dependencies & graph.keys() - {module}
                           for module, dependencies in graph.items()}).prepare()

    def test_background_prompts_do_not_load_message_pipeline(self) -> None:
        modules = loaded_modules("src.ai.background")
        self.assertTrue({"src.ai.discord", "src.ai.images", "src.ai.output"}.isdisjoint(modules))

    def test_codex_does_not_import_ai(self) -> None:
        pattern = re.compile(r"^\s*(?:(?:from|import)\s+(?:src\.ai|\.\.ai)\b|from\s+(?:src|\.\.)\s+import\s+.*\bai\b)", re.M)
        for sample in ("import src.ai.discord", "from ..ai.output import x", "from src import ai", "from .. import brand, ai"):
            self.assertIsNotNone(pattern.search(sample), sample)
        self.assertIsNone(pattern.search("from src import brand, aiohttp_utils"))
        for path in (Path(__file__).parent / "codex").glob("*.py"):
            if path.name.startswith("test_"):
                continue
            with self.subTest(path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_nothing_imports_the_codex_sdk_or_pydantic(self) -> None:
        pattern = re.compile(r"^\s*(?:from|import)\s+(?:openai_codex|pydantic)\b", re.M)
        for sample in ("import pydantic", "from openai_codex.models import x", "    import openai_codex"):
            self.assertIsNotNone(pattern.search(sample), sample)
        self.assertIsNone(pattern.search("import pydantic_extra_types"))
        root = Path(__file__).parent.parent
        for path in (*(root / "src").rglob("*.py"), *(root / "tools").rglob("*.py")):
            with self.subTest(str(path.relative_to(root))):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))
        self.assertTrue({"openai_codex", "pydantic"}.isdisjoint(loaded_modules("src.bot")))

    def test_frame_worker_loads_no_ai_layers(self) -> None:
        modules = loaded_modules("src.ai.frames")
        for module in ("src.codex.protocol", "src.codex.model_settings", "src.core.app.config"):
            self.assertNotIn(module, modules)

    def test_shared_layers_load_no_features(self) -> None:
        features = ("games", "voice", "steam", "ai", "admin")
        for shared in ("state", "core.domain.clock", "core.app.supervisor", "core.adapters.discord.brand",
                       "core.adapters.discord.channels", "core.adapters.discord.watchdog", "core.app.config", "cards"):
            modules = loaded_modules(f"src.{shared}")
            with self.subTest(shared):
                self.assertEqual({m for m in modules if m.startswith("src.") and m.split(".")[1] in features}, set())

    def test_feature_cogs_do_not_load_other_features(self) -> None:
        features = {"games", "voice", "steam", "ai", "admin"}
        allowed = {"games": {"ai"}, "steam": {"ai"}}
        for feature in features - {"admin"}:
            modules = loaded_modules(f"src.{feature}.cog")
            with self.subTest(feature):
                loaded = {m.split(".")[1] for m in modules if m.startswith("src.") and m.split(".")[1] in features}
                self.assertLessEqual(loaded, {feature, *allowed.get(feature, ())})

    def test_domain_rules_and_stores_load_no_discord_or_ai_runtime(self) -> None:
        for module in ("games.store", "games.blackjack", "games.slots"):
            with self.subTest(module=module):
                loaded = loaded_modules(f"src.{module}")
                self.assertNotIn("discord", loaded)
                self.assertFalse(any(name.endswith("views") or name.endswith(".cog") for name in loaded if name.startswith("src.")))

    def test_panel_pages_do_not_import_state(self) -> None:
        pattern = re.compile(r"^\s*(?:from|import)\s+src\.state\b|^\s*from\s+src\s+import\s+.*\bstate\b", re.M)
        for path in (Path(__file__).parent / "admin" / "pages").glob("*.py"):
            with self.subTest(path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_tests_do_not_import_other_tests(self) -> None:
        pattern = re.compile(r"^\s*(?:from|import)\s+src\.(\w+\.)*test_", re.M)
        for path in Path(__file__).parent.rglob("test_*.py"):
            with self.subTest(path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_icons_read_at_render_time(self) -> None:
        def eager(body: list[ast.stmt]):
            for stmt in body:
                if isinstance(stmt, ast.ClassDef):
                    yield from eager(stmt.body)
                elif not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield stmt

        for path in Path(__file__).parent.rglob("*.py"):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            lines = [
                node.lineno
                for stmt in eager(tree.body)
                for node in ast.walk(stmt)
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("ICONS", "WHITE")
            ]
            with self.subTest(str(path.relative_to(Path(__file__).parent))):
                self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
