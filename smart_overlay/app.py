"""Приложение целиком: оверлей, конвейер, горячие клавиши, значок в трее и окно настроек."""

import json
import logging
import os

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QActionGroup, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .config import INSTANCE_PATH, ConfigStore, restart_needed
from .gemini_client import resolve_api_key
from .history import HistoryStore
from .hotkeys import HotkeyListener, pretty_hotkey
from .i18n import msg
from .overlay import Overlay
from .pipeline import Events, Pipeline
from .settings_server import PACKAGE_DIR, SettingsServer

log = logging.getLogger(__name__)

ICON_PATH = PACKAGE_DIR / "assets" / "icon.png"
HOTKEYS_RESUME_MS = 20_000  # если окно настроек закрыли во время записи сочетания
HISTORY_PRUNE_MS = 3_600_000  # раз в час удаляем записи старше срока хранения


class Controller(QObject):
    settings_changed = Signal(object, object)  # (было, стало) — из потока веб-сервера в GUI-поток
    control_requested = Signal(str, bool)
    hotkeys_enabled = Signal(bool)

    def __init__(self, app: QApplication, open_settings: bool = False, demo: bool = False):
        super().__init__()
        self._app = app
        self.store = ConfigStore()
        cfg = self.store.get()
        self.status = (msg("status.starting"), "busy")
        # Состояние, которое не хранится в файле: пауза, видимость окна, режим перемещения.
        self.runtime = {"listening": True, "overlay_visible": True, "move_mode": False}

        self.events = Events()
        self.overlay = Overlay(cfg["overlay"])
        self.overlay.set_preset(cfg["presets"]["active"])
        self.overlay.moved.connect(self._on_overlay_moved)
        self.overlay.resized.connect(self._on_overlay_resized)
        self.events.status.connect(self._on_status)
        self.events.transcript.connect(self.overlay.set_transcript)
        self.events.question.connect(self.overlay.start_answer)
        self.events.answer_chunk.connect(self.overlay.append_answer)
        self.events.hotkey.connect(self._on_hotkey)
        self.settings_changed.connect(self._apply)
        self.control_requested.connect(self._on_control)
        self.hotkeys_enabled.connect(self._set_hotkeys_enabled)

        self.history = HistoryStore()
        self._prune_history()
        self._history_prune = QTimer(self, interval=HISTORY_PRUNE_MS)
        self._history_prune.timeout.connect(self._prune_history)
        self._history_prune.start()

        self.pipeline = Pipeline(cfg, self.events, self.history)
        self.server = SettingsServer(
            store=self.store,
            history=self.history,
            on_changed=self.settings_changed.emit,
            get_status=self._status_snapshot,
            on_control=self.control_requested.emit,
            on_history_ask=self._on_history_ask,
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

        self._tray = QSystemTrayIcon(QIcon(str(ICON_PATH)), self)
        self._tray.activated.connect(
            lambda reason: reason == QSystemTrayIcon.ActivationReason.DoubleClick and self.server.open_window()
        )
        self._menu: QMenu | None = None
        self._tray_rebuild = QTimer(self, singleShot=True, interval=0)
        self._tray_rebuild.timeout.connect(self._build_tray_menu)
        self._build_tray_menu()
        self._tray.show()

        app.aboutToQuit.connect(self._shutdown)
        self.overlay.show()
        if demo:
            self._run_demo()
        else:
            self.pipeline.start()
        if open_settings or (not demo and not resolve_api_key(cfg["gemini"])):
            QTimer.singleShot(300, self.server.open_window)

    # ---------- настройки ----------

    def _set_setting(self, section: str, key: str, value) -> None:
        old, new = self.store.update(lambda cfg: cfg[section].__setitem__(key, value))
        self._apply(old, new)

    @Slot(object, object)
    def _apply(self, old: dict, new: dict) -> None:
        """Применяет изменения настроек на лету — откуда бы они ни пришли (окно настроек, трей, мышь)."""
        if restart_needed(old, new):
            log.info("Перезапуск распознавания с новыми настройками")
            self.pipeline.stop()
            self.pipeline = Pipeline(new, self.events, self.history)
            self.pipeline.start()
            self.runtime["listening"] = True
        else:
            self.pipeline.update_config(new)
        if old["overlay"] != new["overlay"]:
            self.overlay.apply_config(new["overlay"])
        if old["presets"]["active"] != new["presets"]["active"]:
            self.overlay.set_preset(new["presets"]["active"])
        if old["hotkeys"] != new["hotkeys"]:
            self._restart_hotkeys()
            self._update_move_hint()
        if old["language"]["interface"] != new["language"]["interface"]:
            self._update_move_hint()
            self.pipeline.refresh_status()
        elif old["detector"]["auto_answer"] != new["detector"]["auto_answer"]:
            self.pipeline.refresh_status()
        self._tray_rebuild.start()

    @Slot(int, int)
    def _on_overlay_moved(self, x: int, y: int) -> None:
        def place(cfg: dict) -> None:
            cfg["overlay"].update(position="custom", x=x, y=y)

        self._apply(*self.store.update(place))

    @Slot(int, int, int, int)
    def _on_overlay_resized(self, x: int, y: int, width: int, height: int) -> None:
        def place(cfg: dict) -> None:
            cfg["overlay"].update(position="custom", x=x, y=y, width=width, height=height)

        self._apply(*self.store.update(place))

    def _on_history_ask(self, request: dict) -> None:
        """Повторный вопрос из окна истории. Вызывается из потока веб-сервера: конвейер сам уходит в свои потоки."""
        if "event_id" in request:
            self.pipeline.ask_again(request["event_id"])
        else:
            self.pipeline.answer_phrase(request["phrase_id"])

    def _prune_history(self) -> None:
        try:
            self.history.prune(self.store.get()["history"]["keep_days"])
        except Exception:
            log.exception("Не удалось удалить старые записи истории")

    def _next_preset(self) -> None:
        cfg = self.store.get()["presets"]
        names = [""] + [p["name"] for p in cfg["items"]]
        current = names.index(cfg["active"]) if cfg["active"] in names else 0
        self._set_setting("presets", "active", names[(current + 1) % len(names)])

    # ---------- то, что не хранится в файле ----------

    def set_listening(self, enabled: bool) -> None:
        self.runtime["listening"] = enabled
        self.pipeline.set_paused(not enabled)
        self._tray_rebuild.start()

    def set_overlay_visible(self, visible: bool) -> None:
        self.runtime["overlay_visible"] = visible
        self.overlay.setVisible(visible)
        self._tray_rebuild.start()

    def set_move_mode(self, enabled: bool) -> None:
        self.runtime["move_mode"] = enabled
        if enabled and not self.runtime["overlay_visible"]:
            self.set_overlay_visible(True)
        self._update_move_hint()
        self._tray_rebuild.start()

    def _update_move_hint(self) -> None:
        combo = self.store.get()["hotkeys"]["move"]
        hint = msg("overlay.move_hint", hotkey=pretty_hotkey(combo)) if combo else msg("overlay.move_hint_tray")
        self.overlay.set_move_mode(self.runtime["move_mode"], hint)

    @Slot(str, bool)
    def _on_control(self, name: str, value: bool) -> None:
        {"listening": self.set_listening, "overlay_visible": self.set_overlay_visible, "move_mode": self.set_move_mode}[
            name
        ](value)

    def _status_snapshot(self) -> dict:
        text, kind = self.status
        return {"text": text, "kind": kind, "version": self.store.version, "runtime": dict(self.runtime)}

    # ---------- трей, горячие клавиши ----------

    def _build_tray_menu(self) -> None:
        """Меню собирается заново при каждом изменении — так галочки и язык всегда актуальны."""
        cfg = self.store.get()
        menu = QMenu()
        # Через lambda: иначе Qt передаст в open_window флаг checked вместо адреса страницы
        menu.addAction(msg("tray.settings"), lambda: self.server.open_window())
        menu.addAction(msg("tray.history"), lambda: self.server.open_window("/history"))
        menu.addSeparator()
        self._add_check(menu, "tray.listen", self.runtime["listening"], self.set_listening)
        self._add_check(menu, "tray.auto_answer", cfg["detector"]["auto_answer"],
                        lambda on: self._set_setting("detector", "auto_answer", on))
        self._add_check(menu, "tray.screenshots", cfg["screenshot"]["enabled"],
                        lambda on: self._set_setting("screenshot", "enabled", on))
        self._add_check(menu, "tray.llm_check", cfg["detector"]["use_llm_check"],
                        lambda on: self._set_setting("detector", "use_llm_check", on))

        presets = menu.addMenu(msg("tray.preset"))
        group = QActionGroup(presets)
        for name, label in [("", msg("tray.no_preset"))] + [(p["name"], p["name"]) for p in cfg["presets"]["items"]]:
            action = presets.addAction(label)
            action.setCheckable(True)
            action.setChecked(name == cfg["presets"]["active"])
            action.setActionGroup(group)
            action.triggered.connect(lambda _checked=False, n=name: self._set_setting("presets", "active", n))
        menu.addSeparator()

        self._add_check(menu, "tray.show_window", self.runtime["overlay_visible"], self.set_overlay_visible)
        self._add_check(menu, "tray.click_through", cfg["overlay"]["click_through"],
                        lambda on: self._set_setting("overlay", "click_through", on))
        self._add_check(menu, "tray.move", self.runtime["move_mode"], self.set_move_mode)
        menu.addAction(msg("tray.ask"), lambda: self.pipeline.ask_now())
        menu.addAction(msg("tray.clear"), self.overlay.clear_answer)
        menu.addSeparator()
        menu.addAction(msg("tray.quit"), self._app.quit)
        self._tray.setContextMenu(menu)
        self._menu = menu  # иначе меню соберёт сборщик мусора

    @staticmethod
    def _add_check(menu: QMenu, key: str, checked: bool, handler) -> None:
        action = menu.addAction(msg(key))
        action.setCheckable(True)
        action.setChecked(checked)
        action.triggered.connect(handler)

    @Slot(str)
    def _on_hotkey(self, name: str) -> None:
        actions = {
            "toggle": lambda: self.set_overlay_visible(not self.runtime["overlay_visible"]),
            "ask": self.pipeline.ask_now,
            "clear": self.overlay.clear_answer,
            "pause": lambda: self.set_listening(not self.runtime["listening"]),
            "move": lambda: self.set_move_mode(not self.runtime["move_mode"]),
            "next_preset": self._next_preset,
            "scroll_up": lambda: self.overlay.scroll(-1),
            "scroll_down": lambda: self.overlay.scroll(1),
            "settings": self.server.open_window,
            "quit": self._app.quit,
        }
        if name in actions:
            actions[name]()

    @Slot(str, str)
    def _on_status(self, text: str, kind: str) -> None:
        self.status = (text, kind)
        self.overlay.set_status(text, kind)
        self._tray.setToolTip(f"Smart Overlay — {text}"[:120])

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
            self._hotkeys = HotkeyListener(dict(self.store.get()["hotkeys"]), self.events.hotkey.emit)
            self._hotkeys.start()

    # ---------- прочее ----------

    def _run_demo(self) -> None:
        """Пример вопроса и «печатающийся» ответ — проверить вид окна без звука и ключа."""
        self.events.status.emit(msg("status.listening"), "ok")
        self.events.transcript.emit(msg("demo.transcript"))
        self.events.question.emit(0, msg("demo.question"))
        words = iter(msg("demo.answer").split(" "))
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
