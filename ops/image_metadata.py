"""Validate the SDK interface and describe the exact Docker build inputs."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
from pathlib import Path

PACKAGES = ("openai-codex", "openai-codex-cli-bin")
PROTOCOL = 3


def metadata(root: Path) -> dict[str, str]:
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    versions = {}
    for package in PACKAGES:
        matches = re.findall(
            rf"^{re.escape(package)}==([0-9]+\.[0-9]+\.[0-9]+)$", requirements, re.M
        )
        if len(matches) != 1:
            raise ValueError(f"Expected one stable pin for {package}")
        versions[package] = matches[0]
    if len(set(versions.values())) != 1:
        raise ValueError("SDK and CLI pins must match")
    digest = hashlib.sha256()
    paths = [root / name for name in ("Dockerfile", ".dockerignore", "requirements.txt")]
    paths.extend(
        path
        for path in (root / "src").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in (".pyc", ".pyo", ".pyd")
    )
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        data = path.read_bytes()
        if path.name == "requirements.txt" and path.parent == root:
            for package in PACKAGES:
                data = re.sub(
                    rf"(?m)^{re.escape(package)}==[0-9]+\.[0-9]+\.[0-9]+$".encode(),
                    f"{package}==SDK_VERSION".encode(),
                    data,
                )
        name = path.relative_to(root).as_posix().encode()
        digest.update(len(name).to_bytes(8, "big") + name + len(data).to_bytes(8, "big") + data)
    return {
        "sdk": versions[PACKAGES[0]],
        "cli": versions[PACKAGES[1]],
        "protocol": str(PROTOCOL),
        "fingerprint": digest.hexdigest(),
    }


def check_sdk() -> None:
    from openai_codex import AsyncCodex, AsyncThread, AsyncTurnHandle
    from openai_codex.generated.v2_all import ModelListParams

    for name, parameters in {
        "thread_start": ("developer_instructions",),
        "thread_resume": ("include_turns", "developer_instructions"),
        "models": ("include_hidden",),
    }.items():
        signature = inspect.signature(getattr(AsyncCodex, name))
        if not all(parameter in signature.parameters for parameter in parameters):
            raise RuntimeError(f"Incompatible SDK method: {name}")
    signature = inspect.signature(AsyncThread.turn)
    if not all(parameter in signature.parameters for parameter in ("model", "effort", "summary")):
        raise RuntimeError("Incompatible SDK turn options")
    if not all(hasattr(AsyncTurnHandle, name) for name in ("stream", "interrupt")):
        raise RuntimeError("Incompatible SDK turn handle")
    if "cursor" not in ModelListParams.model_fields:
        raise RuntimeError("Incompatible SDK model pagination")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check-sdk", action="store_true")
    args = parser.parse_args()
    if args.check_sdk:
        check_sdk()
    print(json.dumps(metadata(args.root), sort_keys=True))


if __name__ == "__main__":
    main()
