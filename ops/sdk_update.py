"""Root-only, locked Codex image updates with a cold snapshot and crash recovery."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path("/srv/horo-dcb")
DEPLOYMENT = Path("/srv/horo-dcb-data/deployment")
CODEX_HOME = Path("/srv/horo-dcb-data/codex")
STATE = DEPLOYMENT / "state.json"
MARKER = DEPLOYMENT / "maintenance"
BACKUP = DEPLOYMENT / "codex-backup.tar"
PARTIAL_BACKUP = DEPLOYMENT / "codex-backup.pending.tar"
OVERLAY = ROOT / "ai-lifecycle-active.yaml"
PACKAGE = "ghcr.io/horo225/horo-dcb"
SOURCE = "https://github.com/horo225/horo-DCB"
PROTOCOL = "3"
COMPOSE = [
    "docker",
    "compose",
    "--project-directory",
    str(ROOT),
    "-f",
    str(ROOT / "compose.yaml"),
    "-f",
    str(OVERLAY),
]
STATUS_COMMAND = """import json,os,urllib.request
r=urllib.request.Request('http://127.0.0.1:8765/v1/status',headers={'Authorization':'Bearer '+os.environ['CODEX_BRIDGE_TOKEN']})
with urllib.request.urlopen(r,timeout=5) as response:
    s=json.load(response)
print(json.dumps({k:s.get(k) for k in ('ready','authenticated','protocol_version','sdk_version','runtime_version')}))
"""


def run(*args: str, timeout: float = 300) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


def atomic(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def image_metadata(image: str) -> dict[str, str]:
    details = json.loads(run("docker", "image", "inspect", image))[0]
    labels = details.get("Config", {}).get("Labels") or {}
    result = {
        name: labels.get(f"io.horo-dcb.{name}", "")
        for name in ("sdk", "cli", "protocol", "fingerprint")
    }
    if (
        labels.get("org.opencontainers.image.source") != SOURCE
        or not re.fullmatch(r"[0-9a-f]{40}", labels.get("org.opencontainers.image.revision", ""))
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", result["sdk"])
        or result["sdk"] != result["cli"]
        or result["protocol"] != PROTOCOL
        or not re.fullmatch(r"[0-9a-f]{64}", result["fingerprint"])
    ):
        raise ValueError("Image provenance, stable SDK pair, or protocol metadata is invalid")
    return result


def immutable_image(image: str) -> str:
    details = json.loads(run("docker", "image", "inspect", image))[0]
    digests = [
        item
        for item in details.get("RepoDigests", [])
        if re.fullmatch(re.escape(PACKAGE) + r"@sha256:[0-9a-f]{64}", item)
    ]
    if len(digests) != 1:
        raise ValueError("Image must have exactly one approved package digest")
    return digests[0]


def container(service: str, *, timeout: float = 30) -> str:
    value = run(*COMPOSE, "ps", "--all", "--quiet", service, timeout=timeout)
    if not re.fullmatch(r"[0-9a-f]{12,64}", value):
        raise ValueError(f"Expected one {service} container")
    return value


def running_image(service: str) -> str:
    details = json.loads(run("docker", "inspect", container(service)))[0]
    if details["State"].get("Running") is not True:
        raise RuntimeError(f"Expected running {service} service")
    return immutable_image(details["Image"])


def ready(metadata: dict[str, str], *, seconds: int = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            cid = container("codex", timeout=min(5, max(0.1, deadline - time.monotonic())))
            status = json.loads(
                run(
                    "docker",
                    "exec",
                    cid,
                    "python",
                    "-c",
                    STATUS_COMMAND,
                    timeout=min(8, max(0.1, deadline - time.monotonic())),
                )
            )
            if (
                status.get("ready") is True
                and status.get("authenticated") is True
                and str(status.get("protocol_version")) == metadata["protocol"]
                and status.get("sdk_version") == metadata["sdk"]
                and status.get("runtime_version") == metadata["cli"]
            ):
                return
        except subprocess.SubprocessError, ValueError, KeyError:
            pass
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RuntimeError("Codex readiness, authentication, or version validation failed")


def overlay(bot: str, codex: str) -> None:
    atomic(OVERLAY, {"services": {"bot": {"image": bot}, "codex": {"image": codex}}})


def checked_directory(path: Path) -> None:
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Deployment paths must be direct, fixed host directories")


def marker() -> None:
    atomic(MARKER, {"maintenance": True})


def clear_marker() -> None:
    MARKER.unlink(missing_ok=True)


def restore_data() -> None:
    checked_directory(CODEX_HOME)
    if not BACKUP.is_file() or BACKUP.is_symlink():
        raise RuntimeError("Complete cold snapshot is missing")
    # Validate the completed archive before removing candidate data.
    run("tar", "-tf", str(BACKUP))
    for child in CODEX_HOME.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    run("tar", "--acls", "--xattrs", "--numeric-owner", "-xpf", str(BACKUP), "-C", str(CODEX_HOME))


def rollback(state: dict) -> None:
    transaction = state["pending"]
    marker()
    run(*COMPOSE, "stop", "codex", timeout=90)
    if transaction["stage"] in ("backup_ready", "applied", "verified"):
        restore_data()
    elif transaction["stage"] not in ("prepared", "backing_up"):
        raise ValueError("Unknown transaction stage; maintenance retained")
    overlay(state["bot_image"], transaction["old_image"])
    run(*COMPOSE, "up", "-d", "--no-deps", "--no-build", "--force-recreate", "codex")
    ready(transaction["old_metadata"])
    state["current_image"] = transaction["old_image"]
    state["metadata"] = transaction["old_metadata"]
    state["blocked_digest"] = transaction["new_image"]
    state["pending"] = None
    atomic(STATE, state)
    clear_marker()
    PARTIAL_BACKUP.unlink(missing_ok=True)
    print("Codex update rolled back; failed digest blocked")


def recover(state: dict) -> bool:
    if state.get("pending"):
        rollback(state)
        return True
    if MARKER.exists():
        # Receipt may have been committed immediately before a crash.
        if running_image("codex") != state["current_image"]:
            raise RuntimeError("Committed image differs from the running service")
        overlay(state["bot_image"], state["current_image"])
        ready(state["metadata"])
        clear_marker()
        print("Validated committed deployment and cleared maintenance")
        return True
    return False


def bootstrap() -> None:
    if STATE.exists() and json.loads(STATE.read_text()).get("pending"):
        raise RuntimeError("Recover the pending transaction before bootstrap")
    if MARKER.exists():
        raise RuntimeError("Recover maintenance before bootstrap")
    running = {}
    metadata = {}
    for service in ("bot", "codex"):
        running[service] = running_image(service)
        metadata[service] = image_metadata(running[service])
    if metadata["bot"]["fingerprint"] != metadata["codex"]["fingerprint"]:
        raise RuntimeError("Bootstrap requires a synchronized Bot/bridge baseline")
    ready(metadata["codex"])
    state = {
        "schema": 1,
        "baseline": {"fingerprint": metadata["codex"]["fingerprint"], "protocol": PROTOCOL},
        "bot_image": running["bot"],
        "current_image": running["codex"],
        "metadata": metadata["codex"],
        "pending": None,
        "blocked_digest": None,
        "receipt": {"image": running["codex"], "time": int(time.time())},
    }
    overlay(running["bot"], running["codex"])
    atomic(STATE, state)
    print("Pinned running approved containers and established SDK update baseline")


def update(state: dict) -> None:
    if recover(state):
        return
    if (
        running_image("bot") != state["bot_image"]
        or running_image("codex") != state["current_image"]
    ):
        raise RuntimeError("Running images differ from the approved deployment baseline")
    run("docker", "pull", f"{PACKAGE}:sdk-stable")
    candidate = immutable_image(f"{PACKAGE}:sdk-stable")
    if candidate in (state["current_image"], state.get("blocked_digest")):
        return
    try:
        metadata = image_metadata(candidate)
    except ValueError:
        state["blocked_digest"] = candidate
        atomic(STATE, state)
        raise
    if (
        metadata["fingerprint"] != state["baseline"]["fingerprint"]
        or metadata["protocol"] != state["baseline"]["protocol"]
    ):
        print("Candidate held: synchronize the application baseline before SDK update")
        return

    def version(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split("."))

    if version(metadata["sdk"]) <= version(state["metadata"]["sdk"]):
        print("Candidate held: SDK version does not advance the current deployment")
        return
    transaction = {
        "old_image": state["current_image"],
        "old_metadata": state["metadata"],
        "new_image": candidate,
        "new_metadata": metadata,
        "stage": "prepared",
    }
    state["pending"] = transaction
    atomic(STATE, state)
    try:
        marker()
        run(*COMPOSE, "stop", "codex", timeout=90)
        details = json.loads(run("docker", "inspect", container("codex")))[0]
        if details["State"].get("Running") is not False:
            raise RuntimeError("Codex must stop before the cold snapshot")
        transaction["stage"] = "backing_up"
        atomic(STATE, state)
        PARTIAL_BACKUP.unlink(missing_ok=True)
        run(
            "tar",
            "--acls",
            "--xattrs",
            "--numeric-owner",
            "-cpf",
            str(PARTIAL_BACKUP),
            "-C",
            str(CODEX_HOME),
            ".",
        )
        with PARTIAL_BACKUP.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(PARTIAL_BACKUP, BACKUP)
        transaction["stage"] = "backup_ready"
        atomic(STATE, state)
        overlay(state["bot_image"], candidate)
        # Commit intent before starting a candidate that can migrate CODEX_HOME.
        transaction["stage"] = "applied"
        atomic(STATE, state)
        run(*COMPOSE, "up", "-d", "--no-deps", "--no-build", "--force-recreate", "codex")
        ready(metadata)
        transaction["stage"] = "verified"
        atomic(STATE, state)
        state["current_image"] = candidate
        state["metadata"] = metadata
        state["receipt"] = {"image": candidate, "time": int(time.time())}
        state["pending"] = None
        state["blocked_digest"] = None
        atomic(STATE, state)
        clear_marker()
        print("Codex SDK update verified and committed")
    except Exception:
        # Any rollback failure deliberately leaves the transaction and marker intact.
        if state.get("pending"):
            rollback(state)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("update", "bootstrap", "recover"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("This updater must run as root")
    os.umask(0o077)
    for directory in (ROOT, DEPLOYMENT, CODEX_HOME):
        checked_directory(directory)
    if not ROOT.is_dir() or not CODEX_HOME.is_dir():
        raise RuntimeError("Expected production directories are missing")
    DEPLOYMENT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(DEPLOYMENT, 0, 0)
    os.chmod(DEPLOYMENT, 0o700)
    with (ROOT / "ai-lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == "bootstrap":
            bootstrap()
            return
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if state.get("schema") != 1:
            raise ValueError("Unsupported deployment state")
        if args.command == "recover":
            recover(state)
        else:
            update(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Captured command output may contain account data; never emit it to journal.
        print(f"Codex updater failed ({type(error).__name__}); check deployment state", flush=True)
        raise SystemExit(1) from None
