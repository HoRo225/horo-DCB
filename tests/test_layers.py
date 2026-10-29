import ast
import re
import sys
import unittest
from collections.abc import Iterable
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SQL = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s")
FEATURES = ("ai", "games", "voice", "steam", "admin")
ROOT_FILES = frozenset({"__init__.py", "bot.py"})
PACKAGES = frozenset({"core", *FEATURES})
SQL_HOMES = frozenset({"bot.py"})


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(SRC.parent).with_suffix("").parts).removesuffix(".__init__")


def within(name: str, package: str) -> bool:
    return name == package or name.startswith(f"{package}.")


def imports(path: Path, tree: ast.AST) -> set[str]:
    module = module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join([*parts[:len(parts) - node.level + 1], *([base] if base else [])])
            found.add(base)
    return found


def allowed(name: str, prefixes: tuple[str, ...]) -> bool:
    return name.split(".")[0] in sys.stdlib_module_names - {"sqlite3"} or any(within(name, prefix) for prefix in prefixes)


def tree_problems(entries: Iterable[tuple[str, bool]]) -> list[str]:
    return sorted(
        name for name, is_dir in entries
        if name != "__pycache__" and (name not in PACKAGES if is_dir else name not in ROOT_FILES)
    )


def layer_problems(path: Path, source: str) -> list[str]:
    parts = path.relative_to(SRC).parts
    if len(parts) < 3:
        return []
    package, layer = parts[0], parts[1]
    rules = {
        "domain": (f"src.{package}.domain", "src.core.domain"),
        "app": (f"src.{package}.domain", f"src.{package}.app", "src.core.domain", "src.core.app"),
    }
    names = imports(path, ast.parse(source))
    problems = []
    if layer in rules:
        problems += [f"{layer} imports {name}" for name in sorted(names) if not allowed(name, rules[layer])]
    if layer == "adapters":
        kind = parts[2]
        if kind in {"sqlite", "media", "codex"}:
            problems += [f"{kind} adapter imports discord" for name in names if name.split(".")[0] == "discord"]
        if kind == "discord":
            problems += ["discord adapter imports sqlite3" for name in names if name == "sqlite3"]
    return problems


def sql_problems(path: Path, source: str) -> list[str]:
    relative = path.relative_to(SRC)
    if relative.parts[1:3] == ("adapters", "sqlite") or relative.as_posix() in SQL_HOMES:
        return []
    tree = ast.parse(source)
    return ["imports sqlite3" for name in imports(path, tree) if name == "sqlite3"] + [
        f"SQL: {node.value.strip()[:30]}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL.match(node.value)
    ]


def init_problems(path: Path, source: str) -> list[str]:
    package = path.parent.name
    problems = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            own = any(within(node.module or "", f"src.{package}.{layer}") for layer in ("domain", "app"))
            if node.level or not own:
                problems.append(f"exports from {node.module}")
        elif not (isinstance(node, ast.Assign) and [getattr(target, "id", None) for target in node.targets] == ["__all__"]):
            problems.append(f"line {node.lineno} is not an export")
    return problems


def layered() -> list[Path]:
    return sorted(path for path in SRC.iterdir() if any((path / layer).is_dir() for layer in ("domain", "app", "adapters")))


def sources(package: Path):
    for path in sorted(package.rglob("*.py")):
        yield path, path.read_text(encoding="utf-8")


class LayerTests(unittest.TestCase):
    def test_src_root_holds_only_the_composition_root_and_known_packages(self) -> None:
        self.assertEqual(tree_problems((path.name, path.is_dir()) for path in SRC.iterdir()), [])
        self.assertEqual(sorted(SRC.rglob("test_*.py")), [])
        self.assertEqual({path.name for path in layered()}, {"core", *FEATURES})

    def test_layers_import_only_inward(self) -> None:
        for package in layered():
            for path, source in sources(package):
                with self.subTest(str(path.relative_to(SRC))):
                    self.assertEqual(layer_problems(path, source), [])

    def test_sqlite_and_sql_stay_in_sqlite_adapters(self) -> None:
        for path, source in sources(SRC):
            with self.subTest(str(path.relative_to(SRC))):
                self.assertEqual(sql_problems(path, source), [])

    def test_feature_packages_export_only_their_domain_and_app(self) -> None:
        for feature in FEATURES:
            path = SRC / feature / "__init__.py"
            with self.subTest(feature):
                self.assertEqual(init_problems(path, path.read_text(encoding="utf-8")), [])

    def test_detectors_catch_each_violation(self) -> None:
        voice = SRC / "voice"
        cases = {
            voice / "domain" / "x.py": ("import discord", "from src.voice.app.voice import Voice",
                                        "from ..adapters.sqlite import store", "import sqlite3"),
            SRC / "core" / "domain" / "x.py": ("import sqlite3", "from src.core.app.ports import Actor"),
            voice / "app" / "x.py": ("import aiohttp", "from src.core.adapters.sqlite.db import transaction",
                                     "from src.games import x", "from src.voice.adapters.discord import gateway"),
            voice / "adapters" / "sqlite" / "x.py": ("import discord",),
            SRC / "ai" / "adapters" / "codex" / "x.py": ("import discord", "from discord import Embed"),
            voice / "adapters" / "discord" / "x.py": ("import sqlite3",),
        }
        for path, samples in cases.items():
            for sample in samples:
                with self.subTest(sample):
                    self.assertNotEqual(layer_problems(path, sample), [])
        self.assertEqual(layer_problems(voice / "app" / "x.py", "import asyncio\nfrom ..domain.rooms import x\n"
                                                               "from src.core.app.supervisor import TaskGroup"), [])
        for path, sample in (
            (voice / "adapters" / "discord" / "x.py", "QUERY = 'SELECT 1 FROM voice_rooms'"),
            (SRC / "core" / "adapters" / "discord" / "x.py", "db.execute('UPDATE operations SET status = 1')"),
            (SRC / "ai" / "adapters" / "codex" / "rpc.py", "import sqlite3"),
            (SRC / "ai" / "adapters" / "codex" / "conversations.py", "Q = 'SELECT 1 FROM codex_threads'"),
        ):
            with self.subTest(sample):
                self.assertNotEqual(sql_problems(path, sample), [])
        for path in (SRC / "core" / "adapters" / "sqlite" / "x.py", SRC / "ai" / "adapters" / "sqlite" / "conversations.py",
                     SRC / "bot.py"):
            with self.subTest(str(path)):
                self.assertEqual(sql_problems(path, "import sqlite3\nQ = 'SELECT 1'"), [])
        for sample in ("from src.voice.adapters.sqlite.store import X", "VALUE = 1", "from .app.voice import Voice",
                       "from typing import Any", "from src.core.domain.json import json_value"):
            with self.subTest(sample):
                self.assertNotEqual(init_problems(voice / "__init__.py", sample), [])
        self.assertEqual(init_problems(voice / "__init__.py", "from src.voice.app.voice import Voice\n__all__ = ['Voice']"), [])
        self.assertEqual(tree_problems([("__init__.py", False), ("bot.py", False), ("games", True), ("__pycache__", True)]), [])
        self.assertEqual(tree_problems([("state.py", False), ("cards", True), ("bot.py", True)]), ["bot.py", "cards", "state.py"])


if __name__ == "__main__":
    unittest.main()
