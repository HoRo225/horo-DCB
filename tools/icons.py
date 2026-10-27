import argparse
import ast
import io
import json
import math
import os
import pathlib
import re
import urllib.request

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLD, WHITE, ORANGE, RED = (232, 196, 92), (255, 255, 255), (232, 131, 58), (224, 90, 90)
GREY, INK, EMPTY = (128, 132, 142), (31, 25, 51), (78, 80, 88)
SIZE, BIG = 128, 512


def gear(cx, cy, outer, inner, teeth):
    pts = []
    for i in range(teeth):
        for a, r in ((-17, inner), (-9, outer), (9, outer), (17, inner)):
            t = math.radians(360 * i / teeth + a - 90)
            pts += [cx + r * math.cos(t), cy + r * math.sin(t)]
    return ("p", *pts)


def mirror(shape):
    kind, *v = shape
    assert kind[-1] in "lpqba", kind
    if kind[-1] == "a":
        cx, cy, r, a0, a1 = v
        return (kind, 24 - cx, cy, r, 180 - a1, 180 - a0)
    return (kind, *[24 - n if i % 2 == 0 else n for i, n in enumerate(v)])


def flip(shapes):
    return [mirror(s) for s in shapes]


_PAGE = [("q", 12, 6.5, 8, 4, 3, 4.5), ("l", 3, 4.5, 3, 18), ("q", 3, 18, 8, 17.5, 12, 20)]
_PAD = [("b", 17, 6, 21, 6, 22, 19, 18.5, 19), ("q", 18.5, 19, 16.5, 19, 15, 16)]
_BRAIN = [("b", 12, 4.5, 7, 2.5, 2.5, 7, 4.5, 12), ("b", 4.5, 12, 2, 16, 6.5, 21, 12, 19.5)]
SHAPES = {
    "overview": [("l", 3, 11.5, 12, 4, 21, 11.5), ("l", 5.5, 10, 5.5, 20, 18.5, 20, 18.5, 10),
                 ("l", 10, 20, 10, 14.5, 14, 14.5, 14, 20)],
    "ai": [("r", 4, 8, 16, 12, 3), ("l", 12, 8, 12, 5), ("fc", 12, 3.5, 1.5),
           ("fc", 9, 14, 1.6), ("fc", 15, 14, 1.6)],
    "voice": [("r", 9, 3, 6, 10.5, 3), ("a", 12, 11, 6.5, 0, 180), ("l", 12, 17.5, 12, 21),
              ("l", 8.5, 21, 15.5, 21)],
    "steam": [("l", 7, 6, 17, 6), *_PAD, *flip(_PAD), ("l", 15, 16, 9, 16),
              ("l", 8, 8.5, 8, 12.5), ("l", 6, 10.5, 10, 10.5), ("fc", 14.5, 11.5, 1.2), ("fc", 16.8, 9.2, 1.2)],
    "games": [("l", 13, 7, 13, 3, 3, 3, 3, 17, 7, 17), ("r", 10, 10, 11, 11, 2.5),
              ("fc", 13.25, 13.25, 1.2), ("fc", 17.75, 17.75, 1.2)],
    "blackjack": [("r", 5, 2.5, 14, 19, 2.5), ("fc", 10, 11, 2.3), ("fc", 14, 11, 2.3),
                  ("fp", 8, 12.2, 16, 12.2, 12, 16.8)],
    "slots": [("r", 3, 4, 14, 16, 2.5), ("fr", 5.6, 8, 2.2, 6, 0.6), ("fr", 8.9, 8, 2.2, 6, 0.6),
              ("fr", 12.2, 8, 2.2, 6, 0.6), ("l", 17, 13, 20, 13, 20, 7.5), ("fc", 20, 6, 1.8)],
    "models": [("l", 3, 6, 21, 6), ("l", 3, 12, 21, 12), ("l", 3, 18, 21, 18),
               ("fc", 8, 6, 2.3), ("fc", 15.5, 12, 2.3), ("fc", 10, 18, 2.3)],
    "refresh": [("a", 12, 12.5, 7.5, -40, 250), ("fp", 11.9, 4.6, 9.7, 7.9, 8.05, 3.4)],
    "save": [("p", 4, 4, 16.5, 4, 20, 7.5, 20, 20, 4, 20), ("l", 8, 4, 8, 8.5, 15, 8.5, 15, 4),
             ("l", 7.5, 20, 7.5, 13.5, 16.5, 13.5, 16.5, 20)],
    "prev": [("l", 15, 5, 8, 12, 15, 19)],
    "next": [("l", 9, 5, 16, 12, 9, 19)],
    "settings": [gear(12, 12, 9.5, 7, 6), ("c", 12, 12, 3)],
    "sync": [("l", 8, 20, 8, 4.5), ("l", 4.5, 8, 8, 4.5, 11.5, 8), ("l", 16, 4, 16, 19.5),
             ("l", 12.5, 16, 16, 19.5, 19.5, 16)],
    "fallback": [("l", 3, 7, 7, 7, 15, 17, 20.5, 17), ("l", 3, 17, 7, 17, 15, 7, 20.5, 7),
                 ("l", 17.5, 4, 20.5, 7, 17.5, 10), ("l", 17.5, 14, 20.5, 17, 17.5, 20)],
    "query": [("c", 10.5, 10.5, 6.5), ("l", 15.2, 15.2, 20.5, 20.5)],
    "closed": [("r", 5, 10.5, 14, 10.5, 2.5), ("l", 8, 10.5, 8, 7.5), ("a", 12, 7.5, 4, 180, 360),
               ("l", 16, 7.5, 16, 10.5), ("l", 12, 14.5, 12, 17)],
    "leave": [("l", 11, 3.5, 4.5, 3.5, 4.5, 20.5, 11, 20.5), ("l", 9.5, 12, 20.5, 12),
              ("l", 16.5, 8, 20.5, 12, 16.5, 16)],
    "cancel": [("c", 12, 12, 9), ("l", 5.6, 18.4, 18.4, 5.6)],
    "gift": [("r", 3, 7.5, 18, 4, 1), ("l", 5, 11.5, 5, 20.5, 19, 20.5, 19, 11.5), ("l", 12, 7.5, 12, 20.5),
             ("b", 12, 7.5, 10, 2.5, 5, 3.5, 8.5, 7.5), ("b", 12, 7.5, 14, 2.5, 19, 3.5, 15.5, 7.5)],
    "info": [("c", 12, 12, 9), ("l", 12, 11, 12, 16.5), ("fc", 12, 7.8, 1.3)],
    "ok": [("c", 12, 12, 9), ("l", 8, 12.3, 10.8, 15, 16, 9)],
    "warn": [("p", 12, 3, 21.5, 19.5, 2.5, 19.5), ("l", 12, 9.5, 12, 13.5), ("fc", 12, 16.5, 1.3)],
    "error": [("c", 12, 12, 9), ("l", 8.5, 8.5, 15.5, 15.5), ("l", 15.5, 8.5, 8.5, 15.5)],
    "status": [("l", 3.5, 3.5, 3.5, 20.5, 20.5, 20.5), ("l", 8.5, 16.5, 8.5, 12),
               ("l", 13, 16.5, 13, 7.5), ("l", 17.5, 16.5, 17.5, 10)],
    "access": [("c", 8, 16, 4.5), ("l", 11.2, 12.8, 20, 4), ("l", 17.5, 6.5, 19.8, 8.8),
               ("l", 14.5, 9.5, 16.3, 11.3)],
    "thinking": [*_BRAIN, *flip(_BRAIN), ("l", 12, 4.5, 12, 19.5), ("q", 4.5, 12, 8, 12, 8.5, 9),
                 ("q", 19.5, 12, 16, 12, 15.5, 15)],
    "tech": [("a", 16, 8, 4.5, 0, 270), ("l", 20.5, 8, 16, 8, 16, 3.5), ("l", 12.8, 11.2, 4, 20)],
    "image": [("r", 3, 4, 18, 16, 2.5), ("fc", 15.5, 9, 1.8), ("l", 3.5, 18, 9, 12.5, 14, 17.5),
              ("l", 12.5, 16, 15, 13.5, 20.5, 19)],
    "reconnect": [("l", 9, 3.5, 9, 8.5), ("l", 15, 3.5, 15, 8.5), ("l", 6, 12, 6, 8.5, 18, 8.5, 18, 12),
                  ("a", 12, 12, 6, 0, 180), ("l", 12, 18, 12, 21.5)],
    "deadline": [("c", 12, 13.5, 6.8), ("l", 12, 10, 12, 13.5, 14.5, 16), ("a", 12, 13.5, 10, 205, 245),
                 ("a", 12, 13.5, 10, 295, 335)],
    "hit": [("l", 12, 4.5, 12, 19.5), ("l", 4.5, 12, 19.5, 12)],
    "stand": [("l", 7.5, 13, 7.5, 5.5), ("l", 11, 12, 11, 4), ("l", 14.5, 12, 14.5, 5), ("l", 18, 13, 18, 7.5),
              ("b", 18, 13, 18, 19, 15, 21, 12, 21), ("b", 12, 21, 8, 21, 6.5, 18.5, 3.5, 13.5)],
    "manage": [("l", 4, 12, 20, 12), ("l", 14, 6, 20, 12, 14, 18)],
    "back": [("l", 20, 12, 4, 12), ("l", 10, 6, 4, 12, 10, 18)],
    "dots": [("fc", 5, 12, 2), ("fc", 12, 12, 2), ("fc", 19, 12, 2)],
    "rules": [*_PAGE, *flip(_PAGE), ("l", 12, 6.5, 12, 20)],
    "dotok": [("fc", 12, 12, 6)],
    "dotwarn": [("fc", 12, 12, 6)],
    "dotoff": [("c", 12, 12, 5.5)],
}
FIXED = {"dotok": GOLD, "dotwarn": ORANGE, "dotoff": GREY}


def bez(ps, t):
    while len(ps) > 1:
        ps = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1]) for a, b in zip(ps, ps[1:])]
    return ps[0]


def flatten(shape):
    kind, *v = shape
    base = kind[-1]
    if base in "lp":
        pts = list(zip(v[::2], v[1::2]))
    elif base in "ca":
        cx, cy, r = v[:3]
        a0, a1 = v[3:] if base == "a" else (0, 360)
        pts = [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / 48)),
                cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / 48))) for i in range(49)]
    elif base in "qb":
        pts = [bez(list(zip(v[::2], v[1::2])), i / 24) for i in range(25)]
    else:
        x, y, w, h, r = v
        pts = [(cx + r * math.cos(math.radians(a + 90 * i / 8)), cy + r * math.sin(math.radians(a + 90 * i / 8)))
               for cx, cy, a in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90),
                                 (x + r, y + r, 180)) for i in range(9)]
    if base in "pcr":
        pts.append(pts[0])
    return pts, len(kind) == 2


def stroke(d, pts, width, colour):
    d.line(pts, fill=colour, width=round(width), joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=colour)


def draw(shapes, colour) -> Image.Image:
    im = Image.new("RGBA", (BIG, BIG))
    d = ImageDraw.Draw(im)
    k = BIG / 24
    flat = [([(x * k, y * k) for x, y in pts], filled) for pts, filled in map(flatten, shapes)]
    for c, grow in ((INK, 1.5), (colour, 0)):
        for pts, filled in flat:
            if filled:
                d.polygon(pts, fill=c)
                if grow:
                    stroke(d, pts, grow * k, c)
            else:
                stroke(d, pts, (2 + grow) * k, c)
    return im.resize((SIZE, SIZE), Image.LANCZOS)


def png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def segment(kind: str, colour, s: int = BIG) -> Image.Image:
    im = Image.new("RGBA", (s, s))
    d = ImageDraw.Draw(im)
    h = s * 0.36
    y0, y1 = (s - h) / 2, (s + h) / 2
    d.rectangle([h / 2 if kind == "l" else 0, y0, s - h / 2 if kind == "r" else s, y1], fill=colour)
    if kind == "l":
        d.ellipse([0, y0, h, y1], fill=colour)
    if kind == "r":
        d.ellipse([s - h, y0, s, y1], fill=colour)
    return im.resize((SIZE, SIZE), Image.LANCZOS)


def dots() -> bytes:
    frames = []
    for f in range(12):
        im = Image.new("RGBA", (SIZE, SIZE))
        d = ImageDraw.Draw(im)
        for i in range(3):
            phase = (f - i * 2) % 12
            lift = [0, 7, 14, 18, 14, 7][phase] if phase < 6 else 0
            cx, cy = 28 + i * 36, 74 - lift
            d.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill=GOLD + (255,))
        frames.append(im)
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0, disposal=2, transparency=0)
    return out.getvalue()


def rana(avatar: bytes) -> bytes:
    im = Image.open(io.BytesIO(avatar)).convert("RGBA")
    side = min(im.size)
    left, top = (im.width - side) // 2, (im.height - side) // 2
    inner = BIG - 30
    im = im.crop((left, top, left + side, top + side)).resize((inner, inner), Image.LANCZOS)
    mask = Image.new("L", (inner, inner))
    ImageDraw.Draw(mask).ellipse([0, 0, inner - 1, inner - 1], fill=255)
    out = Image.new("RGBA", (BIG, BIG))
    out.paste(im, (15, 15), mask)
    ImageDraw.Draw(out).ellipse([0, 0, BIG - 1, BIG - 1], outline=GOLD, width=16)
    return png(out.resize((SIZE, SIZE), Image.LANCZOS))


def emojis() -> dict[str, bytes]:
    out = {}
    for key, shapes in SHAPES.items():
        for prefix, colour in (("lg", GOLD), ("lw", WHITE)):
            out[prefix + key] = png(draw(shapes, FIXED.get(key, colour)))
    out["lowarn"] = png(draw(SHAPES["warn"], ORANGE))
    out["lrerror"] = png(draw(SHAPES["error"], RED))
    out["lgdots"] = dots()
    out |= {f"bargold{kind}": png(segment(kind, GOLD)) for kind in "lmr"}
    out |= {f"barempty{kind}": png(segment(kind, EMPTY)) for kind in "lmr"}
    return out


def avatar() -> bytes | None:
    if not (token := os.environ.get("DISCORD_TOKEN")):
        return None
    ua = "DiscordBot (https://github.com/Rapptz/discord.py, 2.7.1)"
    try:
        req = urllib.request.Request("https://discord.com/api/v10/users/@me",
                                     headers={"Authorization": f"Bot {token}", "User-Agent": ua})
        with urllib.request.urlopen(req, timeout=20) as r:
            me = json.load(r)
        if not me.get("avatar"):
            return None
        req = urllib.request.Request(f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=256",
                                     headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception as e:
        print(f"Avatar fetch failed ({type(e).__name__}); using a placeholder.")
        return None


def sheet(icons: dict[str, bytes], face: bytes, path: pathlib.Path) -> None:
    dark, light, page, text = (49, 51, 56), (255, 255, 255), (30, 31, 34), (219, 222, 225)
    font = ImageFont.truetype(str(ROOT / "src/assets/fonts/NotoSansTC-VF.ttf"), 15)
    font.set_variation_by_name("Regular")

    def at(data, px):
        return Image.open(io.BytesIO(data)).convert("RGBA").resize((px, px), Image.LANCZOS)

    def bar(fraction):
        im = Image.new("RGBA", (220, 22))
        for n in range(10):
            kind = "l" if n == 0 else "r" if n == 9 else "m"
            im.alpha_composite(at(icons[f"bar{'gold' if n < round(fraction * 10) else 'empty'}{kind}"], 22), (n * 22, 0))
        return im

    cell, label, groups = 56, 110, 3
    keys = list(SHAPES)
    per = -(-len(keys) // groups)
    top = 150
    out = Image.new("RGBA", (groups * (label + 6 * cell) + 20, top + per * cell + 10), page)
    d = ImageDraw.Draw(out)

    def put(im, x, y, bg, w=None, h=None, button=None):
        w, h = w or im.width + 12, h or im.height + 12
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=bg)
        if button:
            d.rounded_rectangle([x + (w - 44) // 2, y + (h - 32) // 2, x + (w + 44) // 2, y + (h + 32) // 2], 6, fill=button)
        out.alpha_composite(im, (x + (w - im.width) // 2, y + (h - im.height) // 2))
        return x + w + 6

    x, y = 10, 10
    for name in ("lowarn", "lrerror", "lgdots"):
        d.text((x, y), name, fill=text, font=font)
        x = put(at(icons[name], 22), x, y + 22, dark)
        x = put(at(icons[name], 22), x, y + 22, light)
        x = put(at(icons[name], 48), x, y + 22, dark) + 14
    d.text((x, y), "rana", fill=text, font=font)
    for bg in (dark, light):
        x = put(at(face, 22), x, y + 22, bg)
        x = put(at(face, 48), x, y + 22, bg)
    x, y = 10, 95
    for bg in (dark, light):
        for fraction in (0.3, 0.7):
            x = put(bar(fraction), x, y, bg)
    d.text((x + 4, y + 8), "bargold 30% / 70%", fill=text, font=font)

    cols = [(dark, "lg", 22, None), (light, "lg", 22, None), (page, "lw", 20, (88, 101, 242)),
            (page, "lw", 20, (218, 55, 60)), (page, "lg", 20, (78, 80, 88)), (dark, "lg", 48, None)]
    for i, key in enumerate(keys):
        x, y = 10 + (i // per) * (label + 6 * cell), top + (i % per) * cell
        d.text((x, y + cell // 2 - 10), key, fill=text, font=font)
        x += label
        for bg, prefix, px, button in cols:
            put(at(icons[prefix + key], px), x, y + 2, bg, cell - 4, cell - 4, button)
            x += cell
    out.save(path)


def icon_keys() -> set[str]:
    tree = ast.parse((ROOT / "src" / "brand.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "ICONS")
    return set(ast.literal_eval(node.value))


def check() -> None:
    icons = emojis()
    for key in icon_keys():
        assert f"lg{key}" in icons and f"lw{key}" in icons, key
    for name, data in icons.items():
        assert re.fullmatch(r"[a-z0-9]{2,32}", name) and len(data) <= 256 * 1024, name
        im = Image.open(io.BytesIO(data))
        assert im.size == (SIZE, SIZE), name
        if not name.startswith("bar"):
            box = im.convert("RGBA").getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox()
            assert box and min(box[:2]) >= 1 and max(box[2:]) <= SIZE - 1, (name, box)
    assert Image.open(io.BytesIO(rana(png(Image.new("RGB", (300, 200), "red"))))).size == (SIZE, SIZE)
    print(f"OK: {len(SHAPES)} icons, {len(icons)} emojis")


def main() -> None:
    parser = argparse.ArgumentParser(description="Draws the bot's original line icons as gold and white application emojis.")
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "tools" / "out")
    parser.add_argument("--sheet", action="store_true", help="write every emoji and sheet.png")
    parser.add_argument("--check", action="store_true", help="assert names, sizes and coverage")
    args = parser.parse_args()
    if args.check:
        check()
    if args.sheet:
        icons = emojis()
        args.out.mkdir(parents=True, exist_ok=True)
        for name, data in icons.items():
            (args.out / f"{name}.{'gif' if data[:3] == b'GIF' else 'png'}").write_bytes(data)
        if found := avatar():
            face = rana(found)
        else:
            im = Image.new("RGB", (256, 256), (88, 80, 120))
            ImageDraw.Draw(im).ellipse([80, 50, 176, 146], fill=(200, 200, 210))
            face = rana(png(im))
        (args.out / "rana.png").write_bytes(face)
        sheet(icons, face, args.out / "sheet.png")
        print(f"Wrote {len(icons)} emojis, rana.png and sheet.png to {args.out}")


if __name__ == "__main__":
    main()
