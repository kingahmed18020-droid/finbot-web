"""يولّد صور الترحيب (واحدة لكل بيت شعر) بأسلوب الخلفية: أبيض وأسود + glitch + خط عربي.

التشغيل مرة واحدة:  python make_assets.py
يقرأ  assets/wallpaper.png  ويكتب  assets/welcome_1.jpg ... welcome_N.jpg
(Pillow لازم يكون مبني بـ raqm عشان تشكيل العربي — الإصدارات الحديثة فيها ده تلقائيًا)
"""
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from verses import VERSES

BASE = Path(__file__).resolve().parent
ASSETS = BASE / "assets"
FONTS = ASSETS / "fonts"
W, H = 1080, 1920
PAPER = (232, 229, 224)
INK = (10, 10, 10)


def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size, layout_engine=ImageFont.Layout.RAQM)


def fit(draw, text, name, max_w, start, min_size=40):
    size = start
    while size > min_size:
        f = font(name, size)
        box = draw.textbbox((0, 0), text, font=f, direction="rtl", language="ar")
        if box[2] - box[0] <= max_w:
            return f
        size -= 2
    return font(name, min_size)


def centered(draw, y, text, f, fill):
    box = draw.textbbox((0, 0), text, font=f, direction="rtl", language="ar")
    x = (W - (box[2] - box[0])) // 2 - box[0]
    draw.text((x, y), text, font=f, fill=fill, direction="rtl", language="ar")
    return box[3] - box[1]


def base_image(rng):
    img = Image.open(ASSETS / "wallpaper.png").convert("RGB").resize((W, H), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=70, threshold=3))

    # تدرّج للأسود تحت الصورة عشان البيت يتقري بوضوح
    grad = Image.new("L", (1, H), 0)
    top, full = 1330, 1620
    for y in range(H):
        a = 0 if y < top else min(1.0, (y - top) / (full - top)) ** 1.4
        grad.putpixel((0, y), int(255 * a * 0.97))
    grad = grad.resize((W, H))
    img = Image.composite(Image.new("RGB", (W, H), INK), img, grad)

    # شرائح glitch أفقية خفيفة على منطقة التدرّج
    px = img.load()
    for _ in range(9):
        y = rng.randint(1380, 1560)
        h = rng.randint(3, 11)
        shift = rng.randint(-46, 46)
        strip = img.crop((0, y, W, y + h))
        img.paste(strip, (shift, y))
    return img


def draw_ornaments(d, rng):
    # مربعات صغيرة بنفس روح الخلفية
    for _ in range(7):
        x, y = rng.randint(70, W - 90), rng.randint(1420, 1520)
        s = rng.choice([8, 10, 14, 18])
        d.rectangle([x, y, x + s, y + s], fill=rng.choice([PAPER, (90, 90, 90)]))
    # خط رفيع + علامة +
    d.line([(W // 2 - 90, 1822), (W // 2 + 90, 1822)], fill=(120, 120, 120), width=2)
    cx, cy = W // 2, 1822
    d.line([(cx - 9, cy), (cx + 9, cy)], fill=PAPER, width=2)
    d.line([(cx, cy - 9), (cx, cy + 9)], fill=PAPER, width=2)


def grain(img, rng, amount=14):
    noise = Image.effect_noise((W, H), amount).convert("RGB")
    return Image.blend(img, Image.composite(img, noise, Image.new("L", (W, H), 235)), 0.35)


def make(i, verse):
    rng = random.Random(1000 + i)
    img = base_image(rng)
    d = ImageDraw.Draw(img)
    draw_ornaments(d, rng)

    s1, s2 = verse["shatr1"], verse["shatr2"]
    f1 = fit(d, s1, "ArefRuqaa-Bold.ttf", 900, 86)
    f2 = fit(d, s2, "ArefRuqaa-Bold.ttf", 900, 86)
    size = min(f1.size, f2.size)
    f = font("ArefRuqaa-Bold.ttf", size)
    centered(d, 1545, s1, f, PAPER)
    centered(d, 1685, s2, f, PAPER)

    pf = font("Amiri-Bold.ttf", 46)
    centered(d, 1838, verse["poet"], pf, (175, 175, 175))

    img = grain(img, rng)
    out = ASSETS / f"welcome_{i}.jpg"
    img.save(out, quality=90, optimize=True)
    print("wrote", out.name, out.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    for i, v in enumerate(VERSES, 1):
        make(i, v)
