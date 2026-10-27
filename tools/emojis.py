import argparse
import asyncio
import io
import json
import os
import pathlib
import urllib.request
from urllib.parse import quote

from PIL import Image

import icons

REPO = "microsoft/fluentui-emoji"
CDN = f"https://cdn.jsdelivr.net/gh/{REPO}@main/assets/"
LICENSE_URL = f"https://raw.githubusercontent.com/{REPO}/main/LICENSE"
CARDS_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "assets" / "fluent"
SIZE = 128

REELS = {
    "cherry": "Cherries", "lemon": "Lemon", "orange": "Tangerine", "watermelon": "Watermelon",
    "bell": "Bell", "star": "Star", "diamond": "Gem stone", "seven": "Keycap 7",
    "unknown": "White question mark",
}
SPIN_ORDER = ["seven", "cherry", "bell", "lemon", "diamond", "orange", "star", "watermelon"]
CARDS = ["Joker", "Slot machine", "Game die", "Rocket"]


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "horo-dcb-emojis/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def check_folders(names: set[str]) -> None:
    api = f"https://api.github.com/repos/{REPO}/git/trees/"
    assets = next(t["sha"] for t in json.loads(get(api + "main"))["tree"] if t["path"] == "assets")
    existing = {t["path"] for t in json.loads(get(api + assets))["tree"]}
    if missing := sorted(names - existing):
        raise SystemExit(f"Fluent folders not found: {', '.join(missing)}")


def snake(name: str) -> str:
    return name.lower().replace(" ", "_")


def fluent(name: str) -> Image.Image:
    return Image.open(io.BytesIO(get(f"{CDN}{quote(name)}/3D/{snake(name)}_3d.png"))).convert("RGBA")


def png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def small(im: Image.Image) -> Image.Image:
    return im.resize((SIZE, SIZE), Image.LANCZOS)


def spin(symbols: dict[str, Image.Image]) -> bytes:
    strip = Image.new("RGBA", (SIZE, SIZE * len(SPIN_ORDER)))
    for i, key in enumerate(SPIN_ORDER):
        strip.alpha_composite(symbols[key].resize((104, 104), Image.LANCZOS), (12, i * SIZE + 12))
    tall = Image.new("RGBA", (SIZE, strip.height * 2))
    tall.alpha_composite(strip, (0, 0))
    tall.alpha_composite(strip, (0, strip.height))
    step, blur = 48, 12
    frames = []
    for f in range(strip.height // step):
        y = strip.height - f * step
        acc = None
        for t in range(blur):
            top = y - t * step // blur
            crop = tall.crop((0, top, SIZE, top + SIZE)).convert("RGBa")
            acc = crop if acc is None else Image.blend(acc, crop, 1 / (t + 1))
        frame = acc.convert("RGBA")
        alpha = frame.getchannel("A").point(lambda a: 255 if a > 60 else 0)
        p = frame.convert("RGB").quantize(colors=255)
        p.paste(255, mask=Image.eval(alpha, lambda a: 255 - a))
        frames.append(p)
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=30, loop=0, transparency=255, disposal=2)
    return out.getvalue()


def names() -> set[str]:
    return set(icons.emojis()) | {f"reel{key}" for key in REELS} | {"reelspin"}


def build() -> dict[str, bytes]:
    check_folders(set(REELS.values()) | set(CARDS))
    art = {name: fluent(name) for name in sorted(set(REELS.values()) | set(CARDS))}
    emojis = icons.emojis()
    emojis |= {f"reel{key}": png(small(art[name])) for key, name in REELS.items()}
    emojis["reelspin"] = spin({key: art[name] for key, name in REELS.items()})
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    for name in CARDS:
        (CARDS_DIR / f"{snake(name)}.png").write_bytes(png(art[name]))
    (CARDS_DIR / "LICENSE").write_bytes(get(LICENSE_URL))
    return emojis


async def upload(emojis: dict[str, bytes]) -> None:
    import discord

    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(os.environ["DISCORD_TOKEN"])
        existing = {emoji.name for emoji in await client.fetch_application_emojis()}
        avatar = await client.user.display_avatar.with_size(256).with_format("png").read()
        emojis = emojis | {"rana": icons.rana(avatar)}
        added = []
        for name, image in emojis.items():
            if name not in existing:
                await client.create_application_emoji(name=name, image=image)
                added.append(name)
        print(f"Uploaded {len(added)}: {' '.join(added) or '-'}")
        kept = [name for name in emojis if name in existing]
        print(f"Already there {len(kept)}: {' '.join(kept) or '-'}")
    finally:
        await client.close()


async def stale() -> None:
    import discord

    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(os.environ["DISCORD_TOKEN"])
        existing = {emoji.name for emoji in await client.fetch_application_emojis()}
    finally:
        await client.close()
    old = sorted(n for n in existing - names() if n != "rana" and not n.startswith("bj_"))
    print(f"Stale {len(old)}: {' '.join(old) or '-'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Builds the bot's application emojis: line icons from tools/icons.py, Fluent Emoji 3D slot symbols")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent / "out")
    parser.add_argument("--upload", action="store_true", help="add missing names as application emojis")
    parser.add_argument("--stale", action="store_true", help="only list uploaded names this build no longer makes")
    args = parser.parse_args()
    if args.stale:
        asyncio.run(stale())
        return
    emojis = build()
    assert set(emojis) == names(), set(emojis) ^ names()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in emojis.items():
        assert "_" not in name.removeprefix("bj_") and 2 <= len(name) <= 32 and len(data) <= 256 * 1024, name
        (args.out / f"{name}.{'gif' if data[:3] == b'GIF' else 'png'}").write_bytes(data)
    print(f"Wrote {len(emojis)} emojis to {args.out} and {len(CARDS)} card images to {CARDS_DIR}")
    if args.upload:
        asyncio.run(upload(emojis))


if __name__ == "__main__":
    main()
