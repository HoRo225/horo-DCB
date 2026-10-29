import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
CONTAINER_CALLS = frozenset({"list", "dict", "set", "bytearray", "defaultdict", "deque", "OrderedDict", "Counter",
                             "WeakSet", "WeakKeyDictionary", "WeakValueDictionary"})
MUTATORS = frozenset({"append", "extend", "insert", "pop", "popitem", "remove", "discard", "add", "clear", "update",
                      "setdefault", "sort", "reverse", "__setitem__", "__delitem__"})
CONSTANTS = {
    "admin/adapters/discord/pages/ai_models.py": {"EFFORT_LABELS"},
    "admin/adapters/discord/pages/common.py": {"STATUS_DOTS"},
    "admin/adapters/discord/pages/effects.py": {"STATES"},
    "admin/adapters/discord/panel.py": {"PAGES"},
    "ai/adapters/codex/rpc.py": {"SERVER_REQUEST_REPLIES"},
    "ai/adapters/discord/actions.py": {"MODEL_CHOICE"},
    "ai/adapters/discord/errors.py": {"_ERROR_TEXT"},
    "ai/adapters/discord/messages.py": {"DENIAL_CODES"},
    "ai/adapters/discord/output.py": {"PROGRESS_OVERRIDES"},
    "ai/adapters/media/images.py": {"SUPPORTED_IMAGE_TYPES"},
    "core/adapters/discord/brand.py": {"_TONE_EMOJIS", "_TONE_COLOURS"},
    "games/adapters/discord/format.py": {"SUIT_SYMBOLS", "SLOT_FALLBACK", "ACTION_LABELS", "RESULTS"},
    "games/adapters/discord/tables.py": {"VIEWS"},
    "games/domain/slots.py": {"PAYS"},
}
STATE = {
    "core/adapters/discord/brand.py": (
        {"ICONS", "APP_EMOJIS", "WHITE", "_brand_urls"},
        "refreshed from the application emojis and avatar by load_app_emojis and sync_discord_brand, read at render time",
    ),
}


def containers(source: str) -> set[str]:
    found = set()
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        value = node.value
        mutable = isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)) or (
            isinstance(value, ast.Call)
            and (getattr(value.func, "id", None) or getattr(value.func, "attr", None)) in CONTAINER_CALLS
        )
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if mutable:
            found.update(target.id for target in targets if isinstance(target, ast.Name) and target.id != "__all__")
    return found


def mutated(source: str) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Global):
            found.update(node.names)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            for target in node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]:
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    found.add(target.value.id)
                elif isinstance(node, ast.AugAssign) and isinstance(target, ast.Name):
                    found.add(target.id)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS
              and isinstance(node.func.value, ast.Name)):
            found.add(node.func.value.id)
    return found


def product_sources():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8")


class ModuleStateTests(unittest.TestCase):
    def test_module_level_containers_are_listed_constants_or_reasoned_state(self) -> None:
        for path, source in product_sources():
            with self.subTest(path):
                self.assertEqual(containers(source), CONSTANTS.get(path, set()) | STATE.get(path, (set(), ""))[0])

    def test_listed_constants_are_never_mutated(self) -> None:
        constants = set().union(*CONSTANTS.values())
        for path, source in product_sources():
            with self.subTest(path):
                self.assertEqual(mutated(source) & constants, set())

    def test_detectors_see_every_spelling(self) -> None:
        self.assertEqual(containers(
            "A = []\nB: dict[str, int] = {}\nC = {x for x in y}\nD = defaultdict(list)\nE = collections.deque()\n"
            "F = (1, 2)\nG = frozenset()\n__all__ = ['A']\ndef f():\n    local = []",
        ), {"A", "B", "C", "D", "E"})
        self.assertEqual(mutated(
            "A[1] = 2\nB.update(x)\ndel C[k]\nD |= {1}\ndef f():\n    global E\n    E = 1\nself.x.append(1)\nF.get(1)",
        ), {"A", "B", "C", "D", "E"})


if __name__ == "__main__":
    unittest.main()
