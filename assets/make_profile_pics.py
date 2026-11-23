"""Generate original profile pictures for the clip pages (1080x1080 PNG).

Retro-sunset look, the streamer's name in big type and "GTA 6 CLIPS" below.
No streamer photos and no Rockstar logo, so nothing to get taken down, and
it clearly reads as a clips page, not the streamer's official account.
Everything important sits inside the centre circle that YouTube/Instagram crop to.

Run: .venv/bin/pip install pillow && .venv/bin/python assets/make_profile_pics.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 1080
OUT = Path(__file__).resolve().parent / "profile"
_FONT_FILES = {"Impact.ttf": ["/System/Library/Fonts/Supplemental/Impact.ttf", "C:/Windows/Fonts/impact.ttf"],
               "Arial Black.ttf": ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "C:/Windows/Fonts/ariblk.ttf"]}


def font(name: str, size: int):
    return ImageFont.truetype(next(p for p in _FONT_FILES[name] if Path(p).exists()), size)

PAGES = {
    # name, gradient top -> middle -> bottom, sun colours, accent
    "lacy": ("LACY", [(32, 6, 58), (196, 24, 120), (255, 140, 50)], [(255, 214, 90), (255, 70, 140)], (255, 226, 120)),
    "clix": ("CLIX", [(10, 10, 48), (60, 40, 200), (0, 200, 230)], [(120, 255, 240), (90, 110, 255)], (140, 255, 235)),
    "yourragegaming": ("YOURRAGE", [(30, 4, 10), (170, 20, 30), (255, 120, 30)], [(255, 230, 90), (255, 60, 40)],
                       (255, 210, 90)),
    # your own multi-game page (not tied to one streamer or game)
    "crazygamer": ("CRAZY GAMER", [(8, 4, 30), (110, 20, 170), (255, 60, 140)], [(255, 230, 90), (255, 70, 170)],
                   (120, 240, 255)),
    "asianjeff": ("ASIANJEFF", [(6, 18, 40), (20, 110, 90), (250, 200, 60)], [(255, 240, 150), (60, 220, 160)],
                  (170, 255, 210)),
}
LABELS = {"crazygamer": "GAMING CLIPS"}  # pill text; default "GTA 6 CLIPS"
MAX_NAME_WIDTH = 840  # keeps long names inside the circle crop


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(stops, w=SIZE, h=SIZE):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        c = lerp(stops[0], stops[1], t * 2) if t < 0.5 else lerp(stops[1], stops[2], (t - 0.5) * 2)
        d.line([(0, y), (w, y)], fill=c)
    return img


def sun(img, colors, r=330, cy=None):
    """Striped retro sun behind the text."""
    W, H = img.size
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = W // 2, cy if cy is not None else H // 2 + 40
    for y in range(cy - r, cy + r):
        t = (y - (cy - r)) / (2 * r)
        half = int((r * r - (y - cy) ** 2) ** 0.5)
        # cut horizontal gaps that widen towards the bottom
        if t > 0.5 and int((y - cy) / (8 + 22 * (t - 0.5))) % 2:
            continue
        d.line([(cx - half, y), (cx + half, y)], fill=lerp(colors[0], colors[1], t) + (235,))
    glow = layer.filter(ImageFilter.GaussianBlur(40))
    img.paste(glow, (0, 0), glow)
    img.paste(layer, (0, 0), layer)


def grid(img, horizon_frac=0.74):
    """Perspective floor grid at the bottom (synthwave style)."""
    W, H = img.size
    d = ImageDraw.Draw(img, "RGBA")
    horizon = int(H * horizon_frac)
    for i in range(1, 9):
        y = horizon + int((i / 8) ** 1.8 * (H - horizon))
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 60), width=2)
    n = W // 80
    for i in range(-n, n + 1):
        d.line([(W // 2 + i * 40, horizon), (W // 2 + i * 260, H)], fill=(255, 255, 255, 50), width=2)


def text_center(d, y, text, font, fill, stroke, spacing=0, width=SIZE):
    if spacing:
        widths = [d.textlength(ch, font=font) for ch in text]
        total = sum(widths) + spacing * (len(text) - 1)
        x = (width - total) / 2
        for ch, w in zip(text, widths):
            d.text((x, y), ch, font=font, fill=fill, stroke_width=stroke[0], stroke_fill=stroke[1])
            x += w + spacing
    else:
        w = d.textlength(text, font=font)
        d.text(((width - w) / 2, y), text, font=font, fill=fill, stroke_width=stroke[0], stroke_fill=stroke[1])


def make(key):
    name, stops, sun_colors, accent = PAGES[key]
    img = gradient(stops).convert("RGBA")
    sun(img, sun_colors)
    grid(img)
    d = ImageDraw.Draw(img)
    size = 300
    while size > 120:  # shrink long names until they fit
        big = font("Impact.ttf", size)
        if sum(d.textlength(ch, font=big) for ch in name) + 12 * (len(name) - 1) <= MAX_NAME_WIDTH:
            break
        size -= 10
    y = 300 + (300 - size) * 0.6  # keep a smaller name visually centred on the sun
    small = font("Arial Black.ttf", 78)
    # drop shadow for the name
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    text_center(sd, y + 14, name, big, (0, 0, 0, 170), (0, (0, 0, 0)), spacing=12)
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(shadow)
    text_center(d, y, name, big, (255, 255, 255), (8, (20, 6, 40)), spacing=12)
    # "GTA 6 CLIPS" pill
    label = LABELS.get(key, "GTA 6 CLIPS")
    lw = sum(d.textlength(ch, font=small) for ch in label) + 8 * (len(label) - 1)
    pill = [((SIZE - lw) / 2 - 36, 668), ((SIZE + lw) / 2 + 36, 668 + 112)]
    d.rounded_rectangle(pill, radius=56, fill=(12, 6, 30, 225), outline=accent + (255,), width=5)
    text_center(d, 676, label, small, accent, (0, (0, 0, 0)), spacing=8)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{key}_profile.png"
    img.convert("RGB").save(path, optimize=True)
    # circle-crop preview, to check nothing important is cut off
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, SIZE, SIZE], fill=255)
    preview = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    preview.paste(img, (0, 0), mask)
    preview.resize((360, 360)).save(OUT / f"{key}_circle_preview.png")
    return path


BW, BH = 2560, 1440          # YouTube banner upload size
SAFE = (1546, 423)           # the part visible on every device (phone crops to this)


def banner(key):
    """Matching YouTube banner; name and label stay inside the centre safe area."""
    name, stops, sun_colors, accent = PAGES[key]
    img = gradient(stops, BW, BH).convert("RGBA")
    sun(img, sun_colors, r=420, cy=BH // 2 + 60)
    grid(img, horizon_frac=0.66)
    d = ImageDraw.Draw(img)
    big = font("Impact.ttf", 230)
    small = font("Arial Black.ttf", 56)
    y = BH // 2 - 190
    shadow = Image.new("RGBA", (BW, BH), (0, 0, 0, 0))
    text_center(ImageDraw.Draw(shadow), y + 14, name, big, (0, 0, 0, 170), (0, (0, 0, 0)), spacing=14, width=BW)
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)))
    text_center(d, y, name, big, (255, 255, 255), (8, (20, 6, 40)), spacing=14, width=BW)
    label = LABELS.get(key, "GTA 6 CLIPS") + "  ·  NEW SHORTS DAILY"
    lw = sum(d.textlength(ch, font=small) for ch in label) + 6 * (len(label) - 1)
    top = BH // 2 + 90
    d.rounded_rectangle([((BW - lw) / 2 - 40, top), ((BW + lw) / 2 + 40, top + 92)], radius=46,
                        fill=(12, 6, 30, 225), outline=accent + (255,), width=5)
    text_center(d, top + 8, label, small, accent, (0, (0, 0, 0)), spacing=6, width=BW)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{key}_banner.png"
    img.convert("RGB").save(path, optimize=True)
    # preview with the phone-safe area outlined, to check nothing important is cut off
    prev = img.copy()
    sx, sy = (BW - SAFE[0]) // 2, (BH - SAFE[1]) // 2
    ImageDraw.Draw(prev).rectangle([sx, sy, sx + SAFE[0], sy + SAFE[1]], outline=(255, 0, 0, 255), width=6)
    prev.convert("RGB").resize((BW // 4, BH // 4)).save(OUT / f"{key}_banner_safe_preview.png")
    return path


if __name__ == "__main__":
    import sys

    for key in sys.argv[1:] or PAGES:
        print(make(key))
        print(banner(key))
