import ast
import re
import subprocess
import sys
import unittest
from graphlib import TopologicalSorter
from pathlib import Path

from discord.ext import commands

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
FEATURES = ("ai", "games", "voice", "steam", "admin")
HOSTS = frozenset({"bot", "client"})
BOT_API = frozenset(dir(commands.Bot))
PRIVATE_ACCESS = {
    "os._exit": "the stdlib hard exit used for failstop after shutdown cannot finish",
}


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")


def runtime_nodes(node: ast.AST):
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


def is_module(name: str) -> bool:
    path = ROOT.joinpath(*name.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def imported(source: str, package: str, *, runtime: bool = False) -> set[str]:
    tree = ast.parse(source)
    found = set()
    for node in runtime_nodes(tree) if runtime else ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*parts, *([base] if base else [])])
            found.update(f"{base}.{alias.name}" if is_module(f"{base}.{alias.name}") else base for alias in node.names)
    return found


def imports(path: Path, *, runtime: bool = False) -> set[str]:
    module = module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    return imported(path.read_text(encoding="utf-8"), package, runtime=runtime)


def product_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def within(name: str, package: str) -> bool:
    return name == package or name.startswith(f"{package}.")


def feature_of(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "src" and parts[1] in FEATURES else None


def cross_feature_problems(module: str, names: set[str]) -> list[str]:
    owner = feature_of(module)
    if owner is None:
        return []
    return sorted(
        name for name in names
        if (other := feature_of(name)) not in (None, owner) and not (owner == "admin" and name == f"src.{other}")
    )


def core_problems(module: str, names: set[str]) -> list[str]:
    if not within(module, "src.core"):
        return []
    return sorted(name for name in names if feature_of(name) or within(name, "src.integrations"))


def host_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def service_lookups(source: str, *, composition_root: bool = False) -> list[str]:
    problems = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and node.args and host_name(node.args[0]) in HOSTS | {f"_{host}" for host in HOSTS}):
            problems.append(f"getattr({host_name(node.args[0])}, ...)")
        if not isinstance(node, ast.Attribute) or node.attr in BOT_API:
            continue
        value = node.value
        through_attribute = isinstance(value, ast.Attribute) and value.attr in HOSTS
        through_name = isinstance(value, ast.Name) and value.id == "bot" and not composition_root
        if through_attribute or through_name:
            problems.append(f"{host_name(value)}.{node.attr}")
    return problems


def private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def private_access(source: str) -> list[str]:
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and private(node.attr):
            if not (isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")):
                problems.append(ast.unparse(node))
        elif isinstance(node, ast.ImportFrom):
            problems += [f"from {node.module} import {alias.name}" for alias in node.names if private(alias.name)]
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "setattr")
              and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
              and isinstance(node.args[1].value, str) and private(node.args[1].value)):
            problems.append(ast.unparse(node))
    return [problem for problem in problems if problem not in PRIVATE_ACCESS]


def loaded_modules(*modules: str) -> set[str]:
    code = f"import sys, {', '.join(modules)}; print(*sys.modules, sep=chr(10))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT)
    return set(result.stdout.split())


def modules_under(*packages: str) -> list[str]:
    return [module_name(path) for path in product_files()
            if any(within(module_name(path), package) for package in packages)]


class FeatureBoundaryTests(unittest.TestCase):
    def test_features_reach_each_other_only_through_admin_package_imports(self) -> None:
        for path in product_files():
            with self.subTest(path=str(path.relative_to(SRC))):
                self.assertEqual(cross_feature_problems(module_name(path), imports(path)), [])

    def test_admin_lives_only_in_its_discord_adapter(self) -> None:
        stray = [path for path in (SRC / "admin").rglob("*.py")
                 if path.parent != SRC / "admin" and SRC / "admin" / "adapters" not in path.parents]
        self.assertEqual(stray, [])
        self.assertFalse((SRC / "admin" / "domain").exists() or (SRC / "admin" / "app").exists())

    def test_no_feature_service_is_looked_up_on_the_bot_or_client(self) -> None:
        for path in product_files():
            with self.subTest(path=str(path.relative_to(SRC))):
                source = path.read_text(encoding="utf-8")
                self.assertEqual(service_lookups(source, composition_root=path == SRC / "bot.py"), [])

    def test_boundary_detectors_catch_each_violation(self) -> None:
        for module, source in (
            ("src.admin.adapters.discord.pages.games", "from src.games.adapters.discord.access import hub_link"),
            ("src.admin.adapters.discord.pages.ai", "from src.ai.adapters.discord.errors import codex_error_text"),
            ("src.games.app.play", "from src.ai import AiText"),
            ("src.steam.adapters.discord.cog", "import src.voice"),
        ):
            with self.subTest(source):
                self.assertNotEqual(cross_feature_problems(module, imported(source, module.rpartition(".")[0])), [])
        self.assertEqual(cross_feature_problems(
            "src.admin.adapters.discord.ports", imported("from src.games import Lobby\nfrom src.core.app import ports", "src"),
        ), [])
        self.assertNotEqual(cross_feature_problems(
            "src.admin.adapters.discord.ports", imported("from src.games import adapters", "src.admin.adapters.discord"),
        ), [])
        for source in (
            "getattr(bot, 'steam')", "getattr(self.bot, 'games', None)", "getattr(interaction.client, 'ai')",
            "self.bot.admin_panels", "interaction.client.games", "bot.temp_voice", "self.cog.bot.effects",
        ):
            with self.subTest(source):
                self.assertNotEqual(service_lookups(source), [])
        for source in ("self.bot.user", "interaction.client.get_guild(1)", "bot.add_dynamic_items(x)",
                       "self._client.request()", "client.request()"):
            with self.subTest(source):
                self.assertEqual(service_lookups(source), [])
        self.assertEqual(service_lookups("bot.db_safe_to_close", composition_root=True), [])


class CoreBoundaryTests(unittest.TestCase):
    def test_core_imports_no_feature_or_integration(self) -> None:
        for path in product_files():
            with self.subTest(path=str(path.relative_to(SRC))):
                self.assertEqual(core_problems(module_name(path), imports(path)), [])

    def test_core_loads_no_feature_or_integration_at_runtime(self) -> None:
        loaded = loaded_modules(*modules_under("src.core"))
        self.assertEqual({m for m in loaded if feature_of(m) or within(m, "src.integrations")}, set())

    def test_detector_catches_core_reaching_out(self) -> None:
        for source in ("from src.games import Lobby", "import src.voice.app.voice", "from src.integrations.codex import rpc",
                       "from ...ai.domain import models"):
            with self.subTest(source):
                self.assertNotEqual(core_problems("src.core.app.x", imported(source, "src.core.app")), [])
        self.assertEqual(core_problems("src.core.app.x", imported("from src.core.domain import json", "src.core.app")), [])
        self.assertEqual(core_problems("src.bot", imported("from src.games import Lobby", "src")), [])


class IntegrationBoundaryTests(unittest.TestCase):
    def test_codex_imports_only_the_ai_package_and_core(self) -> None:
        for path in (SRC / "integrations" / "codex").rglob("*.py"):
            for name in imports(path):
                if not within(name, "src"):
                    continue
                with self.subTest(path=path.name, imported=name):
                    self.assertTrue(name == "src.ai" or within(name, "src.core") or within(name, "src.integrations.codex"))

    def test_only_bot_composes_integrations(self) -> None:
        for path in product_files():
            if within(module_name(path), "src.integrations") or path == SRC / "bot.py":
                continue
            with self.subTest(path=str(path.relative_to(SRC))):
                self.assertFalse({name for name in imports(path) if within(name, "src.integrations")})

    def test_detector_resolves_every_spelling(self) -> None:
        package = "src.integrations.codex"
        for source, expected in (
            ("from src.ai import ModelError", {"src.ai"}),
            ("from src import ai, core", {"src.ai", "src.core"}),
            ("from src.games import adapters", {"src.games.adapters"}),
            ("import src.ai.adapters.discord.cog", {"src.ai.adapters.discord.cog"}),
            ("from ...ai.adapters import x", {"src.ai.adapters"}),
            ("from .. import codex", {"src.integrations.codex"}),
            ("from .rpc import x", {"src.integrations.codex.rpc"}),
            ("if TYPE_CHECKING:\n    from src.games.store import GameStore", {"src.games.store"}),
        ):
            with self.subTest(source):
                self.assertEqual(imported(source, package), expected)
        self.assertEqual(imported("if TYPE_CHECKING:\n    import discord\nimport os", package, runtime=True), {"os"})


class PrivacyTests(unittest.TestCase):
    def test_no_private_name_crosses_an_object_or_module(self) -> None:
        for path in product_files():
            with self.subTest(path=str(path.relative_to(SRC))):
                self.assertEqual(private_access(path.read_text(encoding="utf-8")), [])

    def test_detector_catches_each_spelling(self) -> None:
        for source in ("table._lock", "self.store._close_table()", "from src.ai.adapters.discord.output import _receipt_view",
                       "bot._tasks", "getattr(bot, '_tasks')", "type(self)._cache"):
            with self.subTest(source):
                self.assertNotEqual(private_access(source), [])
        for source in ("self._lock", "cls._registry", "value.__class__", "os._exit(1)", "from x import public"):
            with self.subTest(source):
                self.assertEqual(private_access(source), [])


class RuntimeImportTests(unittest.TestCase):
    def test_runtime_imports_have_no_cycles(self) -> None:
        graph = {module_name(path): imports(path, runtime=True) for path in product_files()}
        TopologicalSorter({module: dependencies & graph.keys() - {module}
                           for module, dependencies in graph.items()}).prepare()

    def test_domain_and_app_load_no_discord_sqlite_or_adapters(self) -> None:
        loaded = loaded_modules(*(m for m in modules_under("src") if ".domain" in m or ".app" in m))
        self.assertTrue({"discord", "sqlite3"}.isdisjoint(loaded))
        self.assertEqual({m for m in loaded if ".adapters" in m or within(m, "src.integrations")}, set())

    def test_frame_worker_loads_no_discord_config_or_integrations(self) -> None:
        modules = loaded_modules("src.ai.adapters.media.frames")
        self.assertNotIn("discord", modules)
        self.assertNotIn("src.core.app.config", modules)
        self.assertFalse({module for module in modules if within(module, "src.integrations")})

    def test_feature_cogs_do_not_load_other_features(self) -> None:
        for feature in set(FEATURES) - {"admin"}:
            modules = loaded_modules(f"src.{feature}.adapters.discord.cog")
            with self.subTest(feature):
                self.assertLessEqual({feature_of(m) for m in modules} - {None}, {feature})

    def test_nothing_imports_the_codex_sdk_or_pydantic(self) -> None:
        pattern = re.compile(r"^\s*(?:from|import)\s+(?:openai_codex|pydantic)\b", re.M)
        for sample in ("import pydantic", "from openai_codex.models import x", "    import openai_codex"):
            self.assertIsNotNone(pattern.search(sample), sample)
        self.assertIsNone(pattern.search("import pydantic_extra_types"))
        for path in SRC.rglob("*.py"):
            with self.subTest(str(path.relative_to(ROOT))):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))
        self.assertTrue({"openai_codex", "pydantic"}.isdisjoint(loaded_modules("src.bot")))

    def test_panel_pages_do_not_import_the_database(self) -> None:
        for path in (SRC / "admin").rglob("*.py"):
            with self.subTest(path.name):
                self.assertFalse({name for name in imports(path) if within(name, "src.core.adapters.sqlite")})

    def test_tests_do_not_import_other_tests(self) -> None:
        for path in TESTS.rglob("*.py"):
            with self.subTest(str(path.relative_to(TESTS))):
                self.assertEqual({name for name in imports(path)
                                  if within(name, "tests") and any(part.startswith("test_") for part in name.split("."))},
                                 set())

    def test_icons_read_at_render_time(self) -> None:
        def eager(body: list[ast.stmt]):
            for stmt in body:
                if isinstance(stmt, ast.ClassDef):
                    yield from eager(stmt.body)
                elif not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield stmt

        for path in product_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            lines = [
                node.lineno
                for stmt in eager(tree.body)
                for node in ast.walk(stmt)
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("ICONS", "WHITE")
            ]
            with self.subTest(str(path.relative_to(SRC))):
                self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
