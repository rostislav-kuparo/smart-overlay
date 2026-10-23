"""Окна поверх всех окон: строка состояния и окна ответов.

Строка состояния (StatusOverlay) живёт всё время: состояние, пресет и последняя распознанная фраза.
Каждый ответ открывается в своём окне (AnswerWindow) и висит, пока его не закроют.

Окна не берут фокус. Их можно таскать мышью за любое место, растягивать за края и углы, колёсико
прокручивает ответ. Строка состояния может пропускать клики сквозь себя (настройка), окна ответов —
нет: их нужно уметь закрыть.
"""

import ctypes
import html
from datetime import datetime

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLayout, QTextBrowser, QToolButton, QVBoxLayout, QWidget

from .i18n import msg

WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_LAYERED = 0x80000
STATUS_COLORS = {"ok": "#4ade80", "busy": "#facc15", "idle": "#9ca3af", "error": "#f87171"}
ACCENT = "#b9adff"
PAD = 14
TRANSCRIPT_MAX_CHARS = 180
RESIZE_MARGIN = 8  # ширина «рамки», за которую окно растягивается
MIN_WIDTH, MIN_HEIGHT = 280, 60
BUTTONS_WIDTH = 64  # место справа в заголовке окна ответа под кнопки
EDGE_CURSORS = {
    "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
    "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
    "lt": Qt.CursorShape.SizeFDiagCursor, "rb": Qt.CursorShape.SizeFDiagCursor,
    "rt": Qt.CursorShape.SizeBDiagCursor, "lb": Qt.CursorShape.SizeBDiagCursor,
}

STYLE = """
#panel {{
    background-color: rgba(18, 18, 22, 235);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 12px;
}}
#panel[moving="true"] {{ border: 2px dashed #8b7bff; }}
QLabel, QTextBrowser {{ background: transparent; border: none; color: #ececf1; font-size: {fs}pt; }}
#hint {{ color: #c9c0ff; font-size: {small}pt; }}
#status, #meta {{ color: #a1a1aa; font-size: {small}pt; }}
#transcript {{ color: #8b8b95; font-size: {small}pt; font-style: italic; }}
#question {{ color: #fcd34d; font-weight: 600; }}
#error {{ color: #f87171; }}
QToolButton {{ background: rgba(255, 255, 255, 18); border: none; border-radius: 6px; color: #d4d4dc;
               font-size: {small}pt; padding: 0; }}
QToolButton:hover {{ background: rgba(255, 255, 255, 45); color: #ffffff; }}
QScrollBar:vertical {{ width: 4px; background: transparent; }}
QScrollBar::handle:vertical {{ background: rgba(255, 255, 255, 70); border-radius: 2px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ height: 0; background: none; }}
"""


class FloatingWindow(QWidget):
    """Общее для окон поверх экрана: вид, перетаскивание, растягивание, сквозные клики, скрытие от захвата."""

    moved = Signal(int, int)  # левый верхний угол после перетаскивания
    resized = Signal(int, int, int, int)  # x, y, ширина, высота после растягивания

    def __init__(self, style_cfg: dict):
        # Флаг Qt WindowTransparentForInput здесь не используется: с ним Qt выбрасывает нажатия мыши,
        # даже если Windows их доставила. Сквозные клики включаются стилем окна — см. _apply_native_input_mode().
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)  # чтобы курсор менялся над краями ещё до нажатия

        self._panel = QFrame(self, objectName="panel")
        self._panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # мышью занимается само окно
        self._inner = QVBoxLayout(self._panel)
        self._inner.setContentsMargins(PAD, PAD - 4, PAD, PAD)
        self._inner.setSpacing(6)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)  # размер окна задаём сами
        outer.addWidget(self._panel)

        self._labels: list[QLabel] = []  # текст над областью ответа, высота по содержимому
        self._browser: QTextBrowser | None = None
        self._interactive = True
        # Перетаскивание: (что делаем: "move" или края вроде "rb", точка нажатия, геометрия в момент нажатия)
        self._drag: tuple[str, QPoint, QRect] | None = None
        self._style_cfg = style_cfg

    # ---------- вид ----------

    def apply_style(self, style_cfg: dict) -> None:
        self._style_cfg = style_cfg
        self.setWindowOpacity(style_cfg["opacity"])
        font_size = style_cfg["font_size"]
        self.setStyleSheet(STYLE.format(fs=font_size, small=max(font_size - 2, 7)))
        if self.isVisible():
            self._apply_capture_exclusion()

    def _apply_capture_exclusion(self) -> None:
        # Окна не попадают на скриншоты — иначе Gemini видел бы собственные прошлые ответы.
        affinity = WDA_EXCLUDEFROMCAPTURE if self._style_cfg["exclude_from_capture"] else WDA_NONE
        ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), affinity)

    def _apply_native_input_mode(self) -> None:
        """Сквозные клики — стиль WS_EX_TRANSPARENT у «слоистого» окна (полупрозрачные окна Qt такие и есть)."""
        user32 = ctypes.windll.user32
        hwnd = int(self.winId())
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if self._interactive:
            style &= ~WS_EX_TRANSPARENT
        elif style & WS_EX_LAYERED:
            style |= WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_capture_exclusion()
        self._apply_native_input_mode()

    def paintEvent(self, event) -> None:
        # Почти прозрачная заливка всего окна: полностью прозрачные пиксели «слоистого» окна пропускают
        # мышь, а так за скруглённые углы панели тоже можно ухватиться.
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, 1))

    # ---------- размер ----------

    @staticmethod
    def _screen_area(point: QPoint | None = None) -> QRect:
        screen = QGuiApplication.screenAt(point) if point is not None else None
        return (screen or QGuiApplication.primaryScreen()).availableGeometry()

    def _layout_content(self, width: int, fixed_height: int, max_ratio: float, area: QRect) -> int:
        """Раскладывает содержимое под ширину окна и возвращает высоту окна.

        fixed_height = 0 — высота по содержимому (область ответа — до max_ratio высоты экрана),
        иначе окно ровно такой высоты, а длинный ответ прокручивается.
        """
        margins = self._inner.contentsMargins()
        inner_width = width - margins.left() - margins.right()
        visible = [w for w in self._labels if not w.isHidden()]
        used = margins.top() + margins.bottom()
        used += sum(w.heightForWidth(inner_width) for w in visible) + self._inner.spacing() * max(0, len(visible) - 1)
        total = max(MIN_HEIGHT, min(fixed_height, area.height())) if fixed_height else used
        if self._browser is not None and not self._browser.isHidden():
            document = self._browser.document()
            document.setTextWidth(inner_width - 8)
            needed = int(document.size().height()) + 4
            if fixed_height:
                answer_height = total - used - self._inner.spacing()
            else:
                answer_height = min(needed, int(area.height() * max_ratio) - used - self._inner.spacing())
                total = used + self._inner.spacing() + max(24, answer_height)
            self._browser.setFixedHeight(max(24, answer_height))
        return total

    # ---------- мышь ----------

    def _edges_at(self, pos: QPoint) -> str:
        edges = ""
        if pos.x() <= RESIZE_MARGIN:
            edges += "l"
        elif pos.x() >= self.width() - RESIZE_MARGIN:
            edges += "r"
        if pos.y() <= RESIZE_MARGIN:
            edges += "t"
        elif pos.y() >= self.height() - RESIZE_MARGIN:
            edges += "b"
        return edges

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        mode = self._edges_at(event.position().toPoint()) or "move"
        self._drag = (mode, event.globalPosition().toPoint(), self.geometry())
        if mode == "move":
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            edges = self._edges_at(event.position().toPoint())
            self.setCursor(EDGE_CURSORS.get(edges, Qt.CursorShape.OpenHandCursor))
            return
        mode, start_pos, start = self._drag
        delta = event.globalPosition().toPoint() - start_pos
        if mode == "move":
            self.move(start.topLeft() + delta)
            return
        x, y, width, height = start.x(), start.y(), start.width(), start.height()
        if "r" in mode:
            width = max(MIN_WIDTH, start.width() + delta.x())
        if "l" in mode:
            width = max(MIN_WIDTH, start.width() - delta.x())
            x = start.x() + start.width() - width
        if "b" in mode:
            height = max(MIN_HEIGHT, start.height() + delta.y())
        if "t" in mode:
            height = max(MIN_HEIGHT, start.height() - delta.y())
            y = start.y() + start.height() - height
        self.setGeometry(x, y, width, height)
        self._relayout_live(width, height)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is None or event.button() != Qt.MouseButton.LeftButton:
            return
        mode = self._drag[0]
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_finished(mode)
        if mode == "move":
            self.moved.emit(self.x(), self.y())
        else:
            self.resized.emit(self.x(), self.y(), self.width(), self.height())

    def wheelEvent(self, event) -> None:
        if self._browser is not None:
            scrollbar = self._browser.verticalScrollBar()
            scrollbar.setValue(scrollbar.value() - event.angleDelta().y() // 2)

    def _relayout_live(self, width: int, height: int) -> None:
        """Перекладка содержимого, пока окно растягивают мышью."""

    def _drag_finished(self, mode: str) -> None:
        """Окно отпустили после перетаскивания или растягивания."""


class StatusOverlay(FloatingWindow):
    """Строка состояния: состояние, пресет и последняя распознанная фраза. Видна всё время."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.setWindowTitle("Smart Overlay")
        self._hint = QLabel(objectName="hint", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self._status = QLabel(objectName="status", wordWrap=True)
        self._transcript = QLabel(objectName="transcript", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self._labels = [self._hint, self._status, self._transcript]
        for widget in self._labels:
            self._inner.addWidget(widget)
            widget.hide()
        self._inner.addStretch(1)  # при заданной вручную высоте содержимое прижато к верху
        self._status.show()

        self._status_text = ("", "idle")
        self._preset = ""
        self._move_mode = False
        self.apply_config(cfg)
        self.set_status(msg("status.starting"), "busy")

    def apply_config(self, cfg: dict) -> None:
        """Применяет настройки на лету — и при старте, и после сохранения в окне настроек."""
        self._cfg = cfg
        self.apply_style(cfg)
        if not cfg["show_transcript"]:
            self._transcript.hide()
        self._update_input_mode()
        self._fit()

    def set_move_mode(self, enabled: bool, hint: str) -> None:
        self._move_mode = enabled
        self._hint.setText(hint)
        self._update_input_mode()
        self._fit()

    def _update_input_mode(self) -> None:
        self._interactive = self._move_mode or not self._cfg["click_through"]
        self._hint.setVisible(self._move_mode)
        self._panel.setProperty("moving", self._move_mode)
        self._panel.style().unpolish(self._panel)
        self._panel.style().polish(self._panel)
        self.setCursor(Qt.CursorShape.OpenHandCursor if self._interactive else Qt.CursorShape.ArrowCursor)
        if self.isVisible():
            self._apply_native_input_mode()

    def _fit(self) -> None:
        """Ставит строку на место из настроек и подгоняет высоту под содержимое."""
        cfg = self._cfg
        custom = cfg["position"] == "custom"
        area = self._screen_area(QPoint(cfg["x"] + 20, cfg["y"] + 20) if custom else None)
        width = max(MIN_WIDTH, min(cfg["width"], area.width()))
        if self._drag is not None:  # во время перетаскивания положением (и размером) управляет мышь
            if self._drag[0] == "move":
                self.resize(self.width(), self._layout_content(self.width(), cfg["height"], 1.0, area))
            return
        height = self._layout_content(width, cfg["height"], 1.0, area)
        margin = cfg["margin"]
        if custom:
            x = min(max(cfg["x"], area.left()), area.right() + 1 - width)
            y = min(max(cfg["y"], area.top()), area.bottom() + 1 - height)
        else:
            position = cfg["position"]
            x = area.right() + 1 - width - margin if position.endswith("right") else area.left() + margin
            y = area.bottom() + 1 - height - margin if position.startswith("bottom") else area.top() + margin
        self.setGeometry(x, y, width, height)

    def _relayout_live(self, width: int, height: int) -> None:
        self._layout_content(width, height, 1.0, self._screen_area(self.geometry().center()))

    @Slot(str, str)
    def set_status(self, text: str, kind: str) -> None:
        self._status_text = (text, kind)
        color = STATUS_COLORS.get(kind, STATUS_COLORS["idle"])
        preset = f' <span style="color:{ACCENT}">· {html.escape(self._preset)}</span>' if self._preset else ""
        self._status.setText(f'<span style="color:{color}">●</span> {html.escape(text)}{preset}')
        self._fit()

    def set_preset(self, name: str) -> None:
        self._preset = name
        self.set_status(*self._status_text)

    @Slot(str)
    def set_transcript(self, text: str) -> None:
        if not self._cfg["show_transcript"]:
            return
        if len(text) > TRANSCRIPT_MAX_CHARS:
            text = "…" + text[-TRANSCRIPT_MAX_CHARS:]
        self._transcript.setText(text)
        self._transcript.show()
        self._fit()


class AnswerWindow(FloatingWindow):
    """Отдельное окно одного ответа. Живёт, пока его не закроют."""

    closed = Signal(int)

    def __init__(self, style_cfg: dict, answer_id: int, question: str, preset: str, max_height_ratio: float):
        super().__init__(style_cfg)
        self.answer_id = answer_id
        self.setWindowTitle("Smart Overlay — " + question[:60])
        self._max_ratio = max_height_ratio
        self._question = QLabel(question, objectName="question", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        meta = " · ".join(filter(None, [datetime.now().strftime("%H:%M:%S"), preset]))
        self._meta = QLabel(meta, objectName="meta", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self._error = QLabel(objectName="error", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self._error.hide()
        self._browser = QTextBrowser(openLinks=False)
        self._browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._browser.document().setDocumentMargin(0)
        self._browser.document().setIndentWidth(18)
        self._browser.setPlainText("…")
        self._labels = [self._question, self._meta, self._error]
        self._question.setContentsMargins(0, 0, BUTTONS_WIDTH, 0)  # справа от вопроса — кнопки
        for widget in (self._question, self._meta, self._error, self._browser):
            self._inner.addWidget(widget)
        self._inner.addStretch(1)

        # Кнопки — дети самого окна, а не панели: панель пропускает мышь насквозь, а кнопки должны нажиматься
        self._copy_button = QToolButton(self, text="⧉", toolTip=msg("answer.copy"))
        self._close_button = QToolButton(self, text="✕", toolTip=msg("answer.close"))
        for button in (self._copy_button, self._close_button):
            button.setFixedSize(24, 22)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._copy_button.clicked.connect(self.copy_answer)
        self._copy_button.hide()  # появится вместе с первыми словами ответа
        self._close_button.clicked.connect(self.close)

        self._text = ""
        self._anchor = QPoint(0, 0)
        self._width = 520
        self._fixed_height = 0
        self.apply_style(style_cfg)

    def place(self, x: int, y: int, width: int, height: int) -> None:
        """Ставит окно: левый верхний угол, ширина и высота (0 — высота по размеру ответа)."""
        self._anchor = QPoint(x, y)
        self._width = max(MIN_WIDTH, width)
        self._fixed_height = height
        self._fit()

    def _fit(self) -> None:
        if self._drag is not None:
            return
        area = self._screen_area(self._anchor + QPoint(20, 20))
        width = min(self._width, area.width())
        height = self._layout_content(width, self._fixed_height, self._max_ratio, area)
        x = min(max(self._anchor.x(), area.left()), area.right() + 1 - width)
        y = min(max(self._anchor.y(), area.top()), area.bottom() + 1 - height)
        self.setGeometry(x, y, width, height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        top = PAD - 6
        self._close_button.move(self.width() - PAD - 24, top)
        self._copy_button.move(self.width() - PAD - 52, top)
        self._copy_button.raise_()
        self._close_button.raise_()

    def _relayout_live(self, width: int, height: int) -> None:
        self._layout_content(width, height, self._max_ratio, self._screen_area(self.geometry().center()))

    def _drag_finished(self, mode: str) -> None:
        self._anchor = self.pos()
        if mode != "move":  # растянули — дальше размер этого окна не меняется сам
            self._width, self._fixed_height = self.width(), self.height()

    def append(self, chunk: str) -> None:
        self._text += chunk
        self._copy_button.show()
        scrollbar = self._browser.verticalScrollBar()
        position = scrollbar.value()
        self._browser.setMarkdown(self._text)
        self._fit()
        scrollbar.setValue(position)

    def finish(self) -> None:
        if not self._text:
            self._browser.hide()
            self._fit()

    def fail(self, error: str) -> None:
        self._error.setText(error)
        self._error.show()
        if not self._text:
            self._browser.hide()
        self._fit()

    @Slot()
    def copy_answer(self) -> None:
        if self._text:
            QApplication.clipboard().setText(self._text)
            self._copy_button.setText("✓")
            QTimer.singleShot(1200, lambda: self._copy_button.setText("⧉"))

    def scroll(self, direction: int) -> None:
        scrollbar = self._browser.verticalScrollBar()
        scrollbar.setValue(scrollbar.value() + direction * int(self._browser.height() * 0.8))

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        self.closed.emit(self.answer_id)
