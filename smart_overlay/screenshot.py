"""Снимок экрана в JPEG для отправки в Gemini."""

import io

import mss
from PIL import Image

from .i18n import msg

_MSS = getattr(mss, "MSS", None) or mss.mss  # в mss 10 класс переименован, старое имя устарело


def list_monitors() -> list[dict]:
    with _MSS() as sct:
        monitors = sct.monitors
    result = [{"index": 0, "label": msg("monitor.all", w=monitors[0]["width"], h=monitors[0]["height"])}]
    for index, m in enumerate(monitors[1:], start=1):
        primary = msg("monitor.primary") if m["left"] == 0 and m["top"] == 0 else ""
        result.append({"index": index, "label": msg("monitor.one", n=index, w=m["width"], h=m["height"]) + primary})
    return result


def grab_screen(monitor: int = 1, max_side: int = 1600, quality: int = 80) -> bytes:
    """monitor: 0 — все мониторы одной картинкой, 1 — основной, 2… — остальные."""
    with _MSS() as sct:
        monitors = sct.monitors
        shot = sct.grab(monitors[monitor if 0 <= monitor < len(monitors) else 1])
    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
