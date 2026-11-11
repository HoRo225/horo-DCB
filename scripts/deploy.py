import argparse
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path("/home/horo-dcb/horo-DCB")
DEPLOYS = Path("/home/horo-dcb/deploys")
SUCCESSFUL = DEPLOYS / "successful.txt"
SERVICES = ("bot", "codex")
VERSION = re.compile(r"[0-9a-f]{7,40}")


class Abort(Exception):
    pass


def step(message: str) -> None:
    print(f"== {message}", flush=True)


def run(*args: str, tag: str | None = None, capture: bool = False) -> str:
    print("$", " ".join(args), flush=True)
    env = None if tag is None else {**os.environ, "IMAGE_TAG": tag}
    result = subprocess.run(args, cwd=PROJECT, env=env, text=True, stdout=subprocess.PIPE if capture else None)
    if result.returncode != 0:
        raise Abort(f"{' '.join(args)} 結束碼 {result.returncode}")
    return result.stdout or ""


def record_path(version: str) -> Path:
    return DEPLOYS / f"{version}.yml"


def image_refs(version: str) -> list[str]:
    return [f"horo-dcb-{service}:{version}" for service in SERVICES]


def running_version() -> str:
    image = run(
        "docker", "ps",
        "--filter", "label=com.docker.compose.project=horo-dcb",
        "--filter", "label=com.docker.compose.service=bot",
        "--format", "{{.Image}}",
        capture=True,
    ).strip()
    if not image:
        raise Abort("bot 容器沒有在執行")
    return image.partition(":")[2]


def local_images() -> dict[str, str]:
    output = run("docker", "image", "ls", "--filter", "reference=horo-dcb-*", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}", capture=True)
    pairs = (line.split() for line in output.splitlines())
    return {ref: image_id for ref, image_id in pairs if not ref.endswith(":<none>")}


def recorded_images(version: str) -> dict[str, str]:
    tail = record_path(version).read_text().partition("\n# images\n")[2]
    return dict(line.removeprefix("# ").split() for line in tail.splitlines())


def require_restorable(version: str) -> None:
    step(f"確認 {version} 可以還原")
    if not record_path(version).exists():
        raise Abort(f"缺少紀錄檔 {record_path(version)}")
    recorded = recorded_images(version)
    local = local_images()
    if sorted(recorded) != sorted(image_refs(version)) or any(local.get(ref) != image_id for ref, image_id in recorded.items()):
        raise Abort(f"{version} 的紀錄與本機映像不符，紀錄 {recorded}")


def go_live(version: str) -> None:
    step(f"以紀錄檔上線 {version}")
    record = str(record_path(version))
    run("docker", "compose", "-f", record, "up", "-d", "--pull", "never", "--no-build")
    run("docker", "compose", "-f", record, "ps")


def deploy(version: str) -> None:
    current = running_version()
    step(f"目前執行中的版本是 {current}")
    require_restorable(current)

    step(f"保存 {current} 的容器日誌")
    logs = run("docker", "compose", "-f", str(record_path(current)), "logs", "--no-color", "--timestamps", capture=True)
    with (DEPLOYS / f"{current}.log").open("a") as file:
        file.write(logs)

    step("快轉更新 main")
    if run("git", "symbolic-ref", "--short", "HEAD", capture=True).strip() != "main":
        raise Abort("專案目錄不在 main")
    run("git", "fetch", "origin")
    run("git", "merge", "--ff-only", "origin/main")
    head = run("git", "rev-parse", "--short", "HEAD", capture=True).strip()
    if head != version:
        raise Abort(f"main 是 {head}，不是 {version}")

    step(f"建置 {version}")
    run("docker", "compose", "build", *SERVICES, tag=version)

    step(f"寫入紀錄檔 {record_path(version)}")
    config = run("docker", "compose", "config", tag=version, capture=True)
    local = local_images()
    images = "".join(f"# {ref} {local[ref]}\n" for ref in image_refs(version))
    partial = record_path(version).with_suffix(".partial")
    partial.write_text(f"{config.rstrip()}\n# images\n{images}")
    partial.replace(record_path(version))

    go_live(version)
    step(f"實測通過後執行 mark-success {version}")


def rollback(version: str) -> None:
    require_restorable(version)
    go_live(version)


def successful_versions() -> list[str]:
    if not SUCCESSFUL.exists():
        return []
    return [line.split()[0] for line in SUCCESSFUL.read_text().splitlines() if line.strip()]


def mark_success(version: str) -> None:
    running = running_version()
    if running != version:
        raise Abort(f"執行中的版本是 {running}，不是 {version}")

    versions = successful_versions()
    if versions[-1:] != [version]:
        step(f"把 {version} 記為成功")
        with SUCCESSFUL.open("a") as file:
            file.write(f"{version} {datetime.now(UTC).isoformat(timespec='seconds')}\n")
        versions.append(version)

    keep = {*list(dict.fromkeys(reversed(versions)))[:2], running}
    step(f"保留 {' '.join(sorted(keep))} 的映像")
    for ref in local_images():
        if ref.partition(":")[2] not in keep:
            run("docker", "image", "rm", ref)
    run("docker", "builder", "prune", "-f")


def version_argument(value: str) -> str:
    if not VERSION.fullmatch(value):
        raise argparse.ArgumentTypeError("需要短 commit")
    return value


COMMANDS = {
    "deploy": (deploy, "建置並上線指定 commit"),
    "rollback": (rollback, "用紀錄檔與原映像還原，不重新建置"),
    "mark-success": (mark_success, "記為成功並清掉較舊的映像"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="horo-DCB 部署")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, (handler, help_text) in COMMANDS.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument("version", type=version_argument)
        command.set_defaults(handler=handler)
    args = parser.parse_args()
    try:
        args.handler(args.version)
    except Abort as error:
        print(f"== 中止：{error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
