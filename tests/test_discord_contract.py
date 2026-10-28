import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def rendered(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
    return None


def discord_contract() -> set[str]:
    found = set()
    for path in SRC.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            keywords = node.keywords if isinstance(node, (ast.Call, ast.ClassDef)) else []
            for keyword in keywords:
                value = rendered(keyword.value)
                if value is None:
                    continue
                if keyword.arg in ("custom_id", "template"):
                    found.add(f"{keyword.arg}:{value}")
                elif keyword.arg == "name" and isinstance(node, ast.Call) and ast.unparse(node.func).endswith(("app_commands.command", "app_commands.Group")):
                    found.add(f"command:{value}")
    return found


class DiscordContractTests(unittest.TestCase):
    def test_persistent_ids_and_commands_are_unchanged(self) -> None:
        self.assertEqual(discord_contract(), {
            "command:控制台",
            "command:新話題",
            "custom_id:horo:game:pick",
            "custom_id:horo:game:rules:{}",
            "custom_id:horo:private:{}",
            "custom_id:horo:steam:claim:{}",
            "custom_id:{}:{}",
            "template:horo:game:pick",
            "template:horo:game:rules:(?P<game>[a-z_]+)",
            "template:horo:private:(?P<token>[A-Za-z0-9_-]+)",
            "template:horo:steam:claim:(?P<app>[0-9]+)",
        })


if __name__ == "__main__":
    unittest.main()
