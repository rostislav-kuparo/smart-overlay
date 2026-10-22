"""Рисует значок приложения: smart_overlay/assets/icon.png и icon.ico. Запуск: python tools/make_icon.py"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 1024  # рисуем крупно и уменьшаем — так края получаются сглаженными
ASSETS = Path(__file__).resolve().parent.parent / "smart_overlay" / "assets"


def gradient(size: int, start: tuple[int, int, int], end: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            pixels[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(start, end))
    return image


def main() -> None:
    background = gradient(SIZE, (124, 92, 255), (61, 139, 255))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, SIZE - 40, SIZE - 40), radius=220, fill=255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(background, mask=mask)

    draw = ImageDraw.Draw(icon)
    bubble = (190, 220, SIZE - 190, 700)
    draw.rounded_rectangle(bubble, radius=150, fill="white")
    draw.polygon([(300, 640), (290, 840), (470, 690)], fill="white")  # хвостик облачка

    font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 400)
    center = ((bubble[0] + bubble[2]) / 2, (bubble[1] + bubble[3]) / 2)
    draw.text(center, "?", font=font, fill=(98, 80, 240), anchor="mm")

    ASSETS.mkdir(parents=True, exist_ok=True)
    icon.resize((256, 256), Image.Resampling.LANCZOS).save(ASSETS / "icon.png")
    icon.save(ASSETS / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
