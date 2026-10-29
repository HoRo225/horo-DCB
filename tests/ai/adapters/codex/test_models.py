import unittest

from src.ai import find_model
from src.ai.adapters.codex.models import parse_model_list


def entry(model: str = "gpt-x", **overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "id": model,
        "model": model,
        "displayName": model,
        "description": "",
        "hidden": False,
        "isDefault": False,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": effort, "description": ""} for effort in ("low", "medium")],
        "inputModalities": ["text"],
    }
    raw.update(overrides)
    return raw


class ModelListTests(unittest.TestCase):
    def test_reads_upstream_model_list(self) -> None:
        catalog = parse_model_list([entry("a"), entry("b", inputModalities=["text", "image"])])
        self.assertEqual(find_model(catalog, "b").input_modalities, ("text", "image"))
        self.assertEqual(find_model(catalog, "a").supported_efforts, ("low", "medium"))
        self.assertEqual(find_model(catalog, "a").default_effort, "medium")

    def test_rejects_inconsistent_catalog(self) -> None:
        for name, models in {
            "empty": [],
            "duplicate": [entry("a"), entry("a")],
            "default not supported": [entry(defaultReasoningEffort="high")],
            "bad effort option": [entry(supportedReasoningEfforts=["low"])],
            "no name": [entry(displayName=" ")],
            "bad identifier": [entry("bad model")],
        }.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_model_list(models)


if __name__ == "__main__":
    unittest.main()
