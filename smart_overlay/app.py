"""Приложение целиком: оверлей, конвейер, горячие клавиши, значок в трее и окно настроек."""

import json
import logging
import os

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .config import CONFIG_PATH, INSTANCE_PATH, load_config, restart_needed, save_config
from .gemini_client import resolve_api_key
from .hotkeys import HotkeyListener
from .overlay import Overlay
from .pipeline import Events, Pipeline
from .settings_server import PACKAGE_DIR, SettingsServer

log = logging.getLogger(__name__)

ICON_PATH = PACKAGE_DIR / "assets" / "icon.png"
HOTKEYS_RESUME_MS = 20_000  # если окно настроек закрыли во время записи сочетания

DEMO_QUESTION = "Чем процесс отличается от потока?"
DEMO_ANSWER = """\
**Процесс — отдельная программа со своей памятью, поток — ход выполнения внутри процесса, \
который делит память с другими потоками.**

- У каждого процесса своё адресное пространство; потоки одного процесса видят общие данные.
- Создать поток и переключиться на него дешевле, чем на процесс.
- Ошибка в одном потоке может уронить весь процесс; процессы изолированы друг от друга.
- Процессы обмениваются данными через IPC (`pipe`, сокеты, shared memory), потокам нужна синхронизация (`mutex`).
"""


class Controller(QObject):
    settings_saved = Signal(dict)  # из потока веб-сервера в GUI-поток
    hotkeys_enabled = Signal(bool)

    def __init__(self, app: QApplication, open_settings: bool = False, demo: bool = False):
        super().__init__()
        self._app = app
        self.cfg = load_config()
        if not CONFIG_PATH.exists():
            save_config(self.cfg)
        self.status = ("Запуск…", "busy")

        self.events = Events()
        self.overlay = Overlay(self.cfg["overlay"])
        self.events.status.connect(self._on_status)
        self.events.transcript.connect(self.overlay.set_transcript)
        self.events.question.connect(self.overlay.start_answer)
        self.events.answer_chunk.connect(self.overlay.append_answer)
        self.events.hotkey.connect(self._on_hotkey)
        self.settings_saved.connect(self._apply_settings)
        self.hotkeys_enabled.connect(self._set_hotkeys_enabled)

        self.pipeline = Pipeline(self.cfg, self.events)
        self.server = SettingsServer(
            get_config=lambda: self.cfg,
            on_saved=self.settings_saved.emit,
            get_status=lambda: self.status,
            set_hotkeys_enabled=self.hotkeys_enabled.emit,
        )
        self.server.start()
        INSTANCE_PATH.write_text(
            json.dumps({"pid": os.getpid(), "port": self.server.port, "token": self.server.token}), encoding="utf-8"
        )

        self._hotkeys: HotkeyListener | None = None
        self._hotkeys_suspended = False
        self._hotkeys_resume = QTimer(self, singleShot=True, interval=HOTKEYS_RESUME_MS)
        self._hotkeys_resume.timeout.connect(lambda: self._set_hotkeys_enabled(True))
        self._restart_hotkeys()

        self._tray = self._make_tray()
        app.aboutToQuit.connect(self._shutdown)
        self.overlay.show()
        if demo:
            self._run_demo()
        else:
            self.pipeline.start()
        if open_settings or (not demo and not resolve_api_key(self.cfg["gemini"])):
            QTimer.singleShot(300, self.server.open_window)

    # ---------- трей и действия ----------

    def _make_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(QIcon(str(ICON_PATH)), self)
        menu = QMenu()
        menu.addAction("Настройки…", self.server.open_window)
        menu.addAction("Показать / скрыть окно", self.overlay.toggle)
        menu.addAction("Спросить сейчас", lambda: self.pipeline.ask_now())
        self._pause_action = menu.addAction("Пауза", self.toggle_pause)
        self._pause_action.setCheckable(True)
        menu.addSeparator()
        menu.addAction("Выход", self._app.quit)
        self._menu = menu  # иначе меню соберёт сборщик мусора
        tray.setContextMenu(menu)
        tray.setToolTip("Smart Overlay")
        tray.activated.connect(
            lambda reason: reason == QSystemTrayIcon.ActivationReason.DoubleClick and self.server.open_window()
        )
        tray.show()
        return tray

    @Slot(str)
    def _on_hotkey(self, name: str) -> None:
        actions = {
            "toggle": self.overlay.toggle,
            "ask": self.pipeline.ask_now,
            "clear": self.overlay.clear_answer,
            "pause": self.toggle_pause,
            "scroll_up": lambda: self.overlay.scroll(-1),
            "scroll_down": lambda: self.overlay.scroll(1),
            "settings": self.server.open_window,
            "quit": self._app.quit,
        }
        if name in actions:
            actions[name]()

    def toggle_pause(self) -> None:
        self.pipeline.toggle_pause()
        self._pause_action.setChecked(self.pipeline.paused)

    @Slot(str, str)
    def _on_status(self, text: str, kind: str) -> None:
        self.status = (text, kind)
        self.overlay.set_status(text, kind)
        self._tray.setToolTip(f"Smart Overlay — {text}"[:120])

    # ---------- настройки ----------

    @Slot(dict)
    def _apply_settings(self, cfg: dict) -> None:
        old, self.cfg = self.cfg, cfg
        if restart_needed(old, cfg):
            log.info("Перезапуск распознавания с новыми настройками")
            self.pipeline.stop()
            self.pipeline = Pipeline(cfg, self.events)
            self.pipeline.start()
            self._pause_action.setChecked(False)
        else:
            self.pipeline.update_config(cfg)
        if old["overlay"] != cfg["overlay"]:
            self.overlay.apply_config(cfg["overlay"])
        if old["hotkeys"] != cfg["hotkeys"]:
            self._restart_hotkeys()

    @Slot(bool)
    def _set_hotkeys_enabled(self, enabled: bool) -> None:
        self._hotkeys_suspended = not enabled
        if enabled:
            self._hotkeys_resume.stop()
        else:
            self._hotkeys_resume.start()
        self._restart_hotkeys()

    def _restart_hotkeys(self) -> None:
        if self._hotkeys is not None:
            self._hotkeys.stop()
            self._hotkeys.join(timeout=1)
            self._hotkeys = None
        if not self._hotkeys_suspended:
            self._hotkeys = HotkeyListener(dict(self.cfg["hotkeys"]), self.events.hotkey.emit)
            self._hotkeys.start()

    # ---------- прочее ----------

    def _run_demo(self) -> None:
        """Пример вопроса и «печатающийся» ответ — проверить вид окна без звука и ключа."""
        self.events.status.emit("Демо-режим", "ok")
        self.events.transcript.emit("Хорошо, тогда следующий вопрос. Чем процесс отличается от потока?")
        self.events.question.emit(0, DEMO_QUESTION)
        words = iter(DEMO_ANSWER.split(" "))
        timer = QTimer(self, interval=40)

        def tick() -> None:
            word = next(words, None)
            if word is None:
                timer.stop()
            else:
                self.events.answer_chunk.emit(0, word + " ")

        timer.timeout.connect(tick)
        timer.start()

    @Slot()
    def _shutdown(self) -> None:
        self.pipeline.stop()
        self._hotkeys_suspended = True
        self._restart_hotkeys()
        self.server.stop()
        try:
            if json.loads(INSTANCE_PATH.read_text(encoding="utf-8")).get("pid") == os.getpid():
                INSTANCE_PATH.unlink()
        except (OSError, ValueError):
            pass
        self._tray.hide()
