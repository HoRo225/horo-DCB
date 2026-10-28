import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")


def imported(source: str, package: str) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*parts, *([base] if base else [])])
            if node.module is None or base == "src":
                found.update(f"{base}.{alias.name}" for alias in node.names)
            else:
                found.add(base)
    return found


def imports(path: Path) -> set[str]:
    module = module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    return imported(path.read_text(encoding="utf-8"), package)


def product_files() -> list[Path]:
    return [path for path in SRC.rglob("*.py") if not path.name.startswith("test_") and path.name != "testing.py"]


def within(name: str, package: str) -> bool:
    return name == package or name.startswith(f"{package}.")


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

    def test_ai_package_exports_only_its_domain_and_app(self) -> None:
        tree = ast.parse((SRC / "ai" / "__init__.py").read_text(encoding="utf-8"))
        for node in tree.body:
            with self.subTest(line=node.lineno):
                if isinstance(node, ast.ImportFrom):
                    self.assertTrue(within(node.module, "src.ai.domain") or within(node.module, "src.ai.app"))
                else:
                    self.assertIsInstance(node, ast.Assign)
                    self.assertEqual([target.id for target in node.targets], ["__all__"])

    def test_detector_resolves_every_spelling(self) -> None:
        package = "src.integrations.codex"
        for source, expected in (
            ("from src.ai import ModelError", {"src.ai"}),
            ("from src import ai, core", {"src.ai", "src.core"}),
            ("import src.ai.adapters.discord.cog", {"src.ai.adapters.discord.cog"}),
            ("from ...ai.adapters import x", {"src.ai.adapters"}),
            ("from .. import codex", {"src.integrations.codex"}),
            ("from .rpc import x", {"src.integrations.codex.rpc"}),
            ("if TYPE_CHECKING:\n    from src.games.store import GameStore", {"src.games.store"}),
        ):
            with self.subTest(source):
                self.assertEqual(imported(source, package), expected)


if __name__ == "__main__":
    unittest.main()
