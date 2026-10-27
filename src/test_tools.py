from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path("/app/tools")


@unittest.skipUnless(TOOLS.exists(), "請加掛 tools/")
class ToolsTests(unittest.TestCase):
    def test_icons_self_check(self) -> None:
        result = subprocess.run([sys.executable, str(TOOLS / "icons.py"), "--check"], capture_output=True, text=True)
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
