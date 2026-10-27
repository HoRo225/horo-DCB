import json
import os
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.cards import t3
from src.games.cards import banner_file

OUT = pathlib.Path(__file__).resolve().parent / "out"
UA = {"User-Agent": "DiscordBot (https://github.com/Rapptz/discord.py, 2.7.1)"}


def rana_avatar() -> bytes | None:
    if not (token := os.environ.get("DISCORD_TOKEN")):
        return None
    try:
        me = json.load(urllib.request.urlopen(urllib.request.Request(
            "https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}", **UA}), timeout=10))
        url = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=256"
        return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read()
    except OSError:
        return None


def rename(file, filename: str):
    file.filename = filename
    return file


SAMPLES = [
    lambda face: rename(banner_file(), "sample-hub.png"),
    lambda face: t3("Apex Legends 排位衝鑽石 出發！", "4 人", [face, None] * 2, "rocket", "sample-t3-depart.png"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    face = rana_avatar()
    for sample in SAMPLES:
        file = sample(face)
        (OUT / file.filename).write_bytes(file.fp.read())
        print("wrote", OUT / file.filename)


if __name__ == "__main__":
    main()
