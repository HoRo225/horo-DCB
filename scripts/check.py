import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STEPS = {
    "pytest": [sys.executable, "-m", "pytest", "-q"],
    "ruff": [sys.executable, "-m", "ruff", "check", "."],
    "ty": ["uv", "check"],
}


def main() -> int:
    failed = []
    for name, command in STEPS.items():
        print(f"== {name}", flush=True)
        if subprocess.run(command, cwd=ROOT).returncode != 0:
            failed.append(name)
    print(f"== FAILED: {', '.join(failed)}" if failed else "== ALL PASSED", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
