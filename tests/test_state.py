import subprocess
import sys
import textwrap
import unittest


class StateContractTest(unittest.TestCase):
    def run_contract(self, source):
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_atomic_write_is_private_utf8_and_replace_failure_preserves_original(self):
        self.run_contract('''
            import json
            from pathlib import Path
            import tempfile
            from unittest.mock import patch
            from src.state import write_json_atomic

            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "nested" / "state.json"
                path.parent.mkdir()
                stale = path.with_name("state.json.tmp")
                stale.write_text("unrelated", encoding="utf-8")
                write_json_atomic(str(path), {"name": "測試"})
                original = path.read_bytes()
                assert json.loads(original) == {"name": "測試"}
                assert "測試" in original.decode("utf-8")
                assert path.stat().st_mode & 0o777 == 0o600
                def fail_replace(source, target):
                    assert Path(source).parent == path.parent
                    assert Path(source) != stale
                    assert Path(source).stat().st_mode & 0o777 == 0o600
                    raise OSError("replace denied")
                with patch("src.state.os.replace", side_effect=fail_replace):
                    try:
                        write_json_atomic(path, {"name": "new"})
                    except OSError as error:
                        assert str(error) == "replace denied"
                    else:
                        raise AssertionError("replace failure was swallowed")
                assert path.read_bytes() == original
                assert set(path.parent.iterdir()) == {path, stale}
                assert stale.read_text(encoding="utf-8") == "unrelated"
        ''')

    def test_missing_and_corrupt_access_require_channels_then_roles_and_survive_restart(self):
        self.run_contract('''
            import json
            from pathlib import Path
            import tempfile
            from src.ai.access import CodexAccess

            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "access.json"
                for value in (None, "{", json.dumps({"version": 99})):
                    if value is not None:
                        path.write_text(value, encoding="utf-8")
                    access = CodexAccess(True, 10, state_path=path)
                    assert access.state_available is (value is None)
                    assert not access.configured
                    assert not access.allows(10, 20, frozenset({70}))
                    access.set_channels(10, frozenset({20}))
                    assert not access.configured
                    assert not access.allows(10, 20, frozenset({70}))
                    access.set_roles(10, frozenset({70}))
                    restarted = CodexAccess(True, 10, state_path=path)
                    assert restarted.configured
                    assert restarted.allows(10, 20, frozenset({70}))
                    assert not restarted.allows(10, 20)
        ''')

    def test_legacy_environment_cannot_authorize_or_appear_in_preflight(self):
        self.run_contract('''
            import contextlib
            import io
            import json
            import os
            from pathlib import Path
            import tempfile
            from src.preflight import main

            os.environ.update(DISCORD_TOKEN="configured", CODEX_BRIDGE_TOKEN="a" * 64,
                CODEX_ENABLED="1", CODEX_ALLOWED_GUILD_ID="10",
                CODEX_ALLOWED_CHANNEL_ID="20", CODEX_ALLOWED_USER_IDS="30")
            with tempfile.TemporaryDirectory() as directory:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    main(Path(directory) / "missing.json")
                summary = json.loads(output.getvalue())
                assert not summary["codex_allowlist_configured"]
                assert not any("legacy" in key or "user" in key for key in summary)
        ''')
