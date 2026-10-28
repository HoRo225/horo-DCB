import ast
import re
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SQL = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s")


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(SRC.parent).with_suffix("").parts).removesuffix(".__init__")


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
    return name.split(".")[0] in sys.stdlib_module_names - {"sqlite3"} or any(
        name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes
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
    tree = ast.parse(source)
    names = imports(path, tree)
    problems = []
    if layer in rules:
        problems += [f"{layer} imports {name}" for name in sorted(names) if not allowed(name, rules[layer])]
    if layer == "adapters":
        kind = parts[2]
        if kind in {"sqlite", "media"}:
            problems += [f"{kind} adapter imports discord" for name in names if name.split(".")[0] == "discord"]
        if kind == "discord":
            problems += ["discord adapter imports sqlite3" for name in names if name == "sqlite3"]
    if package != "core" and (SRC / package / "adapters").is_dir() and parts[1:3] != ("adapters", "sqlite"):
        problems += ["imports sqlite3 outside adapters/sqlite" for name in names if name == "sqlite3"]
        problems += [
            f"SQL outside adapters/sqlite: {node.value.strip()[:30]}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL.match(node.value)
        ]
    return problems


def init_problems(path: Path, source: str) -> list[str]:
    package = path.parent.name
    problems = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            if not allowed(node.module or "", (f"src.{package}.domain", f"src.{package}.app")) or node.level:
                problems.append(f"exports from {node.module}")
        elif not (isinstance(node, ast.Assign) and [getattr(target, "id", None) for target in node.targets] == ["__all__"]):
            problems.append(f"line {node.lineno} is not an export")
    return problems


def layered() -> list[Path]:
    return sorted(path for path in SRC.iterdir() if (path / "domain").is_dir() or (path / "app").is_dir())


def sources(package: Path):
    for path in sorted(package.rglob("*.py")):
        if not path.name.startswith("test_"):
            yield path, path.read_text(encoding="utf-8")


class LayerTests(unittest.TestCase):
    def test_voice_is_layered(self) -> None:
        self.assertIn(SRC / "voice", layered())

    def test_layers_import_only_inward(self) -> None:
        for package in layered():
            for path, source in sources(package):
                with self.subTest(str(path.relative_to(SRC))):
                    self.assertEqual(layer_problems(path, source), [])

    def test_feature_packages_export_only_domain_and_app(self) -> None:
        for package in layered():
            if package.name == "core" or not (package / "adapters").is_dir():
                continue
            path = package / "__init__.py"
            with self.subTest(package.name):
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
            voice / "adapters" / "discord" / "x.py": ("import sqlite3", "QUERY = 'SELECT 1 FROM voice_rooms'"),
        }
        for path, samples in cases.items():
            for sample in samples:
                with self.subTest(sample):
                    self.assertNotEqual(layer_problems(path, sample), [])
        self.assertEqual(layer_problems(voice / "app" / "x.py", "import asyncio\nfrom ..domain.rooms import x\n"
                                                               "from src.core.app.supervisor import TaskGroup"), [])
        for sample in ("from src.voice.adapters.sqlite.store import X", "VALUE = 1", "from .app.voice import Voice"):
            with self.subTest(sample):
                self.assertNotEqual(init_problems(voice / "__init__.py", sample), [])


if __name__ == "__main__":
    unittest.main()
