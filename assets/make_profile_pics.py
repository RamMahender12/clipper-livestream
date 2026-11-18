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
FONTS = Path("/System/Library/Fonts/Supplemental")
OUT = Path(__file__).resolve().parent / "profile"

PAGES = {
    # name, gradient top -> middle -> bottom, sun colours, accent
    "lacy": ("LACY", [(32, 6, 58), (196, 24, 120), (255, 140, 50)], [(255, 214, 90), (255, 70, 140)], (255, 226, 120)),
    "clix": ("CLIX", [(10, 10, 48), (60, 40, 200), (0, 200, 230)], [(120, 255, 240), (90, 110, 255)], (140, 255, 235)),
    "yourragegaming": ("YOURRAGE", [(30, 4, 10), (170, 20, 30), (255, 120, 30)], [(255, 230, 90), (255, 60, 40)],
                       (255, 210, 90)),
}
MAX_NAME_WIDTH = 840  # keeps long names inside the circle crop


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(stops):
    img = Image.new("RGB", (SIZE, SIZE))
    px = img.load()
    for y in range(SIZE):
        t = y / (SIZE - 1)
        c = lerp(stops[0], stops[1], t * 2) if t < 0.5 else lerp(stops[1], stops[2], (t - 0.5) * 2)
        for x in range(SIZE):
            px[x, y] = c
    return img


def sun(img, colors):
    """Striped retro sun behind the text."""
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r, cx, cy = 330, SIZE // 2, SIZE // 2 + 40
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


def grid(img):
    """Perspective floor grid at the bottom (synthwave style)."""
    d = ImageDraw.Draw(img, "RGBA")
    horizon = int(SIZE * 0.74)
    for i in range(1, 9):
        y = horizon + int((i / 8) ** 1.8 * (SIZE - horizon))
        d.line([(0, y), (SIZE, y)], fill=(255, 255, 255, 60), width=2)
    for i in range(-10, 11):
        d.line([(SIZE // 2 + i * 40, horizon), (SIZE // 2 + i * 260, SIZE)], fill=(255, 255, 255, 50), width=2)


def text_center(d, y, text, font, fill, stroke, spacing=0):
    if spacing:
        widths = [d.textlength(ch, font=font) for ch in text]
        total = sum(widths) + spacing * (len(text) - 1)
        x = (SIZE - total) / 2
        for ch, w in zip(text, widths):
            d.text((x, y), ch, font=font, fill=fill, stroke_width=stroke[0], stroke_fill=stroke[1])
            x += w + spacing
    else:
        w = d.textlength(text, font=font)
        d.text(((SIZE - w) / 2, y), text, font=font, fill=fill, stroke_width=stroke[0], stroke_fill=stroke[1])


def make(key):
    name, stops, sun_colors, accent = PAGES[key]
    img = gradient(stops).convert("RGBA")
    sun(img, sun_colors)
    grid(img)
    d = ImageDraw.Draw(img)
    size = 300
    while size > 120:  # shrink long names until they fit
        big = ImageFont.truetype(str(FONTS / "Impact.ttf"), size)
        if sum(d.textlength(ch, font=big) for ch in name) + 12 * (len(name) - 1) <= MAX_NAME_WIDTH:
            break
        size -= 10
    y = 300 + (300 - size) * 0.6  # keep a smaller name visually centred on the sun
    small = ImageFont.truetype(str(FONTS / "Arial Black.ttf"), 78)
    # drop shadow for the name
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    text_center(sd, y + 14, name, big, (0, 0, 0, 170), (0, (0, 0, 0)), spacing=12)
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(shadow)
    text_center(d, y, name, big, (255, 255, 255), (8, (20, 6, 40)), spacing=12)
    # "GTA 6 CLIPS" pill
    label = "GTA 6 CLIPS"
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


if __name__ == "__main__":
    import sys

    for key in sys.argv[1:] or PAGES:
        print(make(key))
