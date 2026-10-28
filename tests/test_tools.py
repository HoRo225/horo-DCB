from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"


class ToolsTests(unittest.TestCase):
    def test_icons_self_check(self) -> None:
        result = subprocess.run([sys.executable, str(TOOLS / "icons.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_samples_and_codex_smoke_import_the_product_modules(self) -> None:
        for tool in ("samples", "smoke_codex"):
            with self.subTest(tool):
                result = subprocess.run([sys.executable, "-c", f"import tools.{tool}"], capture_output=True, text=True,
                                        cwd=TOOLS.parent)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_emojis_rebuild_bars(self) -> None:
        sys.path.insert(0, str(TOOLS))
        try:
            import emojis
        finally:
            sys.path.remove(str(TOOLS))
        self.assertLessEqual({f"barempty{kind}" for kind in "lmr"}, emojis.names())
        retired = {f"die{face}" for face in range(1, 7)} | {"lgdice", "lwdice"}
        self.assertFalse(retired & emojis.names())
