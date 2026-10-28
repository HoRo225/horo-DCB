import ast
import unittest
from pathlib import Path

from discord.ext import commands

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FEATURES = ("ai", "games", "voice", "steam", "admin")
HOSTS = frozenset({"bot", "client"})
BOT_API = frozenset(dir(commands.Bot))


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
        self.assertEqual(cross_feature_problems("src.admin.adapters.discord.ports", {"src.games", "src.core.app"}), [])
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
