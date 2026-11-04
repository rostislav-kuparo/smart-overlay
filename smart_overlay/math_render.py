"""Формулы в ответах: находит LaTeX в тексте модели и рисует его картинками (ziamath → SVG → QImage)."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

log = logging.getLogger(__name__)

# Код и экранированный доллар пропускаем как есть. Правило для $…$ — как в pandoc: сразу после открывающего
# и перед закрывающим долларом нет пробела, а за закрывающим нет цифры. Иначе «от $5 до $10» стало бы формулой.
_MATH = re.compile(
    r"""
    (?:^[ \t]*```.*?(?:^[ \t]*```|\Z))
    | `[^`\n]*`
    | \\\$
    | \$\$(?P<dollars>.+?)\$\$
    | \\\[(?P<brackets>.+?)\\\]
    | \\\((?P<parens>.+?)\\\)
    | (?<![\\\w$])\$(?P<inline>[^\s$](?:[^$\n]*?[^\s\\$])?)\$(?!\d)
    """,
    re.DOTALL | re.MULTILINE | re.VERBOSE,
)


def replace_math(text: str, replace: Callable[[str, bool, str], str]) -> str:
    """Подставляет вместо формул результат replace(latex, на отдельной строке, исходный текст с разделителями)."""

    def substitute(match: re.Match) -> str:
        display = match["dollars"] or match["brackets"]
        latex = display or match["parens"] or match["inline"]
        return replace(latex.strip(), bool(display), match[0]) if latex else match[0]

    return _MATH.sub(substitute, text)


@dataclass(frozen=True)
class Formula:
    source: str  # как формула написана в ответе — так она и копируется
    display: bool
    image: QImage
    width: float
    height: float


def warm_up() -> None:
    """Первый импорт ziamath занимает полсекунды — лучше потратить их при запуске, а не посреди ответа."""
    try:
        import ziamath  # noqa: F401
    except ImportError:
        log.warning("Нет пакета ziamath: формулы будут показаны как текст. Запустите install.bat")


@lru_cache(maxsize=512)
def render_formula(
    latex: str, display: bool, source: str, size: float, color: str, half_x_height: float, ratio: float
) -> Formula | None:
    """Рисует формулу. Возвращает None, если LaTeX разобрать не удалось.

    Строчную формулу документ ставит по центру строчных букв (AlignMiddle). Картинка дополняется сверху или снизу
    так, чтобы при этом базовая линия формулы совпала с базовой линией текста: дробь не уезжает вверх.
    """
    try:
        import ziamath

        ziamath.config.svg2 = False  # QtSvg понимает только SVG 1.x
        math = ziamath.Math.fromlatex(latex, size=size, color=color, inline=not display)
        svg = math.svg()
    except Exception as e:
        log.info("Формула не разобрана (%s): %s", e, latex)
        return None
    renderer = QSvgRenderer(svg.encode("utf-8"))
    if not renderer.isValid():
        return None
    view = renderer.viewBoxF()
    # В SVG базовая линия — это y = 0, выше неё координаты отрицательные
    above, below = -view.top(), view.bottom()
    top = bottom = 0.0
    if not display:
        top = max(0.0, below + half_x_height - above)
        bottom = max(0.0, above - half_x_height - below)
    width, height = view.width(), view.height() + top + bottom

    image = QImage(round(width * ratio), round(height * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(ratio, ratio)
    renderer.render(painter, QRectF(0, top, view.width(), view.height()))
    painter.end()
    image.setDevicePixelRatio(ratio)
    return Formula(source, display, image, width, height)
