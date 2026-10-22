"""Полупрозрачное окно поверх всех окон: статус, последняя фраза, вопрос и ответ.

Окно не перехватывает мышь и фокус — управление только горячими клавишами.
"""

import ctypes
import html

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QLabel, QTextBrowser, QVBoxLayout, QWidget

WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
STATUS_COLORS = {"ok": "#4ade80", "busy": "#facc15", "idle": "#9ca3af", "error": "#f87171"}
PAD = 14
TRANSCRIPT_MAX_CHARS = 180

STYLE = """
#panel {{
    background-color: rgba(18, 18, 22, 235);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 12px;
}}
QLabel, QTextBrowser {{ background: transparent; border: none; color: #ececf1; font-size: {fs}pt; }}
#status {{ color: #a1a1aa; font-size: {small}pt; }}
#transcript {{ color: #8b8b95; font-size: {small}pt; font-style: italic; }}
#question {{ color: #fcd34d; font-weight: 600; }}
QScrollBar:vertical {{ width: 4px; background: transparent; }}
QScrollBar::handle:vertical {{ background: rgba(255, 255, 255, 70); border-radius: 2px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ height: 0; background: none; }}
"""


class Overlay(QWidget):
    def __init__(self, cfg: dict):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("Smart Overlay")

        self._panel = QFrame(self, objectName="panel")
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
        for widget in (self._status, self._transcript, self._question, self._answer):
            inner.addWidget(widget)
            widget.hide()
        self._status.show()

        QVBoxLayout(self).setContentsMargins(0, 0, 0, 0)

        self._answer_id = 0
        self._answer_text = ""
        self.apply_config(cfg)
        self.set_status("Запуск…", "busy")

    def apply_config(self, cfg: dict) -> None:
        """Применяет настройки окна на лету — вызывается и при старте, и после сохранения в WebUI."""
        self._cfg = cfg
        self.setWindowOpacity(cfg["opacity"])
        font_size = cfg["font_size"]
        self.setStyleSheet(STYLE.format(fs=font_size, small=max(font_size - 2, 7)))

        outer = self.layout()
        while outer.count():
            outer.takeAt(0)
        at_bottom = cfg["position"].startswith("bottom")
        if at_bottom:
            outer.addStretch(1)
        outer.addWidget(self._panel)
        if not at_bottom:
            outer.addStretch(1)

        if not cfg["show_transcript"]:
            self._transcript.hide()
        self._place()
        if self.isVisible():
            self._apply_capture_exclusion()
        self._fit()

    def _apply_capture_exclusion(self) -> None:
        # Окно не попадает на скриншоты — иначе Gemini видел бы собственные прошлые ответы.
        affinity = WDA_EXCLUDEFROMCAPTURE if self._cfg["exclude_from_capture"] else WDA_NONE
        ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), affinity)

    def _place(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        width = self._cfg["width"]
        height = int(screen.height() * self._cfg["max_height_ratio"])
        margin = self._cfg["margin"]
        position = self._cfg["position"]
        x = screen.right() - width - margin if position.endswith("right") else screen.left() + margin
        y = screen.bottom() - height - margin if position.startswith("bottom") else screen.top() + margin
        self.setGeometry(x, y, width, height)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_capture_exclusion()

    def _fit(self) -> None:
        """Подгоняет высоту блока ответа под текст, но не выше окна; лишнее прокручивается."""
        layout = self._panel.layout()
        margins = layout.contentsMargins()
        inner_width = self.width() - margins.left() - margins.right()
        used = margins.top() + margins.bottom()
        for widget in (self._status, self._transcript, self._question):
            if not widget.isHidden():
                used += widget.heightForWidth(inner_width) + layout.spacing()
        document = self._answer.document()
        document.setTextWidth(inner_width - 8)
        needed = int(document.size().height()) + 4
        self._answer.setFixedHeight(max(24, min(needed, self.height() - used)))

    @Slot(str, str)
    def set_status(self, text: str, kind: str) -> None:
        color = STATUS_COLORS.get(kind, STATUS_COLORS["idle"])
        self._status.setText(f'<span style="color:{color}">●</span> {html.escape(text)}')
        self._fit()

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

    @Slot()
    def toggle(self) -> None:
        self.setVisible(not self.isVisible())

    @Slot(int)
    def scroll(self, direction: int) -> None:
        scrollbar = self._answer.verticalScrollBar()
        scrollbar.setValue(scrollbar.value() + direction * int(self._answer.height() * 0.8))
