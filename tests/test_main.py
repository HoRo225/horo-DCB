import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import horo_dcb.__main__ as main_module


class MainTests(unittest.TestCase):
    def test_missing_token_exits_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": str(missing)}):
                self.assertTrue(callable(getattr(main_module, "main", None)))
                with self.assertRaises(SystemExit) as caught:
                    main_module.main()

        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
