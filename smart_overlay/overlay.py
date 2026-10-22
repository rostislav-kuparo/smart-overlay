"""Полупрозрачное окно поверх всех окон: состояние, пресет, последняя фраза, вопрос и ответ.

Окно не берёт фокус. Его можно таскать мышью за любое место, растягивать за края и углы,
а колёсико прокручивает ответ; новое место и размер сохраняются в настройках. Если включены
сквозные клики, мышь проходит насквозь — тогда двигать окно можно в режиме перемещения.
"""

import ctypes
import html

from PySide6.QtCore import QPoint, QRect, Qt, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QFrame, QLabel, QLayout, QTextBrowser, QVBoxLayout, QWidget

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
MIN_WIDTH, MIN_HEIGHT = 280, 90
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
#status {{ color: #a1a1aa; font-size: {small}pt; }}
#transcript {{ color: #8b8b95; font-size: {small}pt; font-style: italic; }}
#question {{ color: #fcd34d; font-weight: 600; }}
QScrollBar:vertical {{ width: 4px; background: transparent; }}
QScrollBar::handle:vertical {{ background: rgba(255, 255, 255, 70); border-radius: 2px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ height: 0; background: none; }}
"""


class Overlay(QWidget):
    moved = Signal(int, int)  # левый верхний угол после перетаскивания
    resized = Signal(int, int, int, int)  # x, y, ширина, высота после растягивания

    def __init__(self, cfg: dict):
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
        self.setWindowTitle("Smart Overlay")
        self.setMouseTracking(True)  # чтобы курсор менялся над краями ещё до нажатия

        self._panel = QFrame(self, objectName="panel")
        self._panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # мышью занимается само окно
        self._hint = QLabel(objectName="hint", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self._status = QLabel(objectName="status", wordWrap=True)
        self._transcript = QLabel(objectName="transcript", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self._question = QLabel(objectName="question", wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self._answer = QTextBrowser(openLinks=False)
        self._answer.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._answer.document().setDocumentMargin(0)
        self._answer.document().setIndentWidth(18)

        inner = QVBoxLayout(self._panel)
        inner.setContentsMargins(PAD, PAD - 4, PAD, PAD)
        inner.setSpacing(6)
        for widget in (self._hint, self._status, self._transcript, self._question, self._answer):
            inner.addWidget(widget)
            widget.hide()
        inner.addStretch(1)  # при заданной вручную высоте содержимое прижато к верху
        self._status.show()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)  # размер окна задаёт _fit()
        outer.addWidget(self._panel)

        self._answer_id = 0
        self._answer_text = ""
        self._status_text = ("", "idle")
        self._preset = ""
        self._move_mode = False
        self._interactive = False
        # Перетаскивание: (что делаем: "move" или края вроде "rb", точка нажатия, геометрия в момент нажатия)
        self._drag: tuple[str, QPoint, QRect] | None = None
        self.apply_config(cfg)
        self.set_status(msg("status.starting"), "busy")

    # ---------- настройки и режим ввода ----------

    def apply_config(self, cfg: dict) -> None:
        """Применяет настройки окна на лету — и при старте, и после сохранения в окне настроек."""
        self._cfg = cfg
        self.setWindowOpacity(cfg["opacity"])
        font_size = cfg["font_size"]
        self.setStyleSheet(STYLE.format(fs=font_size, small=max(font_size - 2, 7)))
        if not cfg["show_transcript"]:
            self._transcript.hide()
        self._update_input_mode()
        if self.isVisible():
            self._apply_capture_exclusion()
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

    def _apply_capture_exclusion(self) -> None:
        # Окно не попадает на скриншоты — иначе Gemini видел бы собственные прошлые ответы.
        affinity = WDA_EXCLUDEFROMCAPTURE if self._cfg["exclude_from_capture"] else WDA_NONE
        ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), affinity)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_capture_exclusion()
        self._apply_native_input_mode()

    def paintEvent(self, event) -> None:
        # Почти прозрачная заливка всего окна: полностью прозрачные пиксели «слоистого» окна пропускают
        # мышь, а так за скруглённые углы панели тоже можно ухватиться.
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, 1))

    # ---------- размер и положение ----------

    def _screen_area(self, point: QPoint | None = None) -> QRect:
        screen = QGuiApplication.screenAt(point) if point is not None else None
        return (screen or QGuiApplication.primaryScreen()).availableGeometry()

    def _layout_content(self, width: int, fixed_height: int, area: QRect) -> int:
        """Раскладывает содержимое под ширину окна и возвращает высоту окна.

        fixed_height = 0 — высота подстраивается под ответ (до max_height_ratio экрана),
        иначе окно ровно такой высоты, а длинный ответ прокручивается.
        """
        layout = self._panel.layout()
        margins = layout.contentsMargins()
        inner_width = width - margins.left() - margins.right()
        visible = [w for w in (self._hint, self._status, self._transcript, self._question) if not w.isHidden()]
        used = margins.top() + margins.bottom()
        used += sum(w.heightForWidth(inner_width) for w in visible) + layout.spacing() * (len(visible) - 1)
        total = max(MIN_HEIGHT, min(fixed_height, area.height())) if fixed_height else used
        if not self._answer.isHidden():
            document = self._answer.document()
            document.setTextWidth(inner_width - 8)
            needed = int(document.size().height()) + 4
            if fixed_height:
                answer_height = total - used - layout.spacing()
            else:
                answer_height = min(needed, int(area.height() * self._cfg["max_height_ratio"]) - used - layout.spacing())
                total = used + layout.spacing() + max(24, answer_height)
            self._answer.setFixedHeight(max(24, answer_height))
        return total

    def _fit(self) -> None:
        """Ставит окно на место из настроек и подгоняет высоту под содержимое."""
        cfg = self._cfg
        custom = cfg["position"] == "custom"
        area = self._screen_area(QPoint(cfg["x"] + 20, cfg["y"] + 20) if custom else None)
        width = max(MIN_WIDTH, min(cfg["width"], area.width()))
        if self._drag is not None:  # во время перетаскивания положением (и размером) управляет мышь
            if self._drag[0] == "move":
                self.resize(self.width(), self._layout_content(self.width(), cfg["height"], area))
            else:
                self._layout_content(self.width(), self.height(), area)
            return
        height = self._layout_content(width, cfg["height"], area)
        margin = cfg["margin"]
        if custom:
            x = min(max(cfg["x"], area.left()), area.right() + 1 - width)
            y = min(max(cfg["y"], area.top()), area.bottom() + 1 - height)
        else:
            position = cfg["position"]
            x = area.right() + 1 - width - margin if position.endswith("right") else area.left() + margin
            y = area.bottom() + 1 - height - margin if position.startswith("bottom") else area.top() + margin
        self.setGeometry(x, y, width, height)

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
        self._layout_content(width, height, self._screen_area(QPoint(x + 20, y + 20)))

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is None or event.button() != Qt.MouseButton.LeftButton:
            return
        mode = self._drag[0]
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if mode == "move":
            self.moved.emit(self.x(), self.y())
        else:
            self.resized.emit(self.x(), self.y(), self.width(), self.height())

    def wheelEvent(self, event) -> None:
        scrollbar = self._answer.verticalScrollBar()
        scrollbar.setValue(scrollbar.value() - event.angleDelta().y() // 2)

    # ---------- содержимое ----------

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

    @Slot(int, str)
    def start_answer(self, answer_id: int, question: str) -> None:
        self._answer_id = answer_id
        self._answer_text = ""
        self._question.setText(question)
        self._question.show()
        self._answer.setPlainText("…")
        self._answer.show()
        self._fit()

    @Slot(int, str)
    def append_answer(self, answer_id: int, chunk: str) -> None:
        if answer_id != self._answer_id:
            return
        self._answer_text += chunk
        scrollbar = self._answer.verticalScrollBar()
        position = scrollbar.value()
        self._answer.setMarkdown(self._answer_text)
        self._fit()
        scrollbar.setValue(position)

    @Slot()
    def clear_answer(self) -> None:
        self._answer_id = -1  # хвост текущего ответа, если он ещё идёт, больше не показываем
        self._question.hide()
        self._answer.hide()
        self._fit()

    @Slot(int)
    def scroll(self, direction: int) -> None:
        scrollbar = self._answer.verticalScrollBar()
        scrollbar.setValue(scrollbar.value() + direction * int(self._answer.height() * 0.8))
