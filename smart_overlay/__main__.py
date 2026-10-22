"""Точка входа: smart-overlay [--settings] [--demo] [--list-devices]

Повторный запуск, когда программа уже работает, просто открывает окно настроек.
"""

import argparse
import json
import logging
import os
import signal
import sys
import urllib.request
from logging.handlers import RotatingFileHandler

from .config import APP_DIR, INSTANCE_PATH, LOG_PATH


def signal_running_instance() -> bool:
    """Если программа уже запущена, просит её открыть настройки и возвращает True.

    Вызывается до импорта тяжёлых библиотек, чтобы повторный запуск срабатывал мгновенно.
    """
    try:
        info = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}/api/open-settings",
            data=b"{}",
            method="POST",
            headers={"X-Token": info["token"], "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # мимо системного прокси
        with opener.open(request, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _setup_logging() -> None:
    if sys.stderr is None:  # запуск без консоли (pythonw / ярлык): stdout и stderr нет вовсе
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
        ],
    )
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "huggingface_hub", "google_genai", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(prog="smart-overlay", description=__doc__)
    parser.add_argument("--settings", action="store_true", help="сразу открыть окно настроек")
    parser.add_argument("--demo", action="store_true", help="показать оверлей с примером ответа, без звука и Gemini")
    parser.add_argument("--list-devices", action="store_true", help="список устройств вывода звука и выйти")
    args = parser.parse_args()
    _setup_logging()

    if args.list_devices:
        from .audio import list_loopback_devices

        for device in list_loopback_devices():
            print(device["name"])
        return

    if not args.demo and signal_running_instance():
        logging.info("Smart Overlay уже запущен — открываю настройки")
        return

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .app import ICON_PATH, Controller

    app = QApplication(sys.argv)
    app.setApplicationName("Smart Overlay")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setQuitOnLastWindowClosed(False)
    controller = Controller(app, open_settings=args.settings, demo=args.demo)  # noqa: F841 — живёт до выхода

    # Qt не отдаёт управление Python, пока ждёт событий, — таймер даёт шанс обработать Ctrl+C в консоли.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(200)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
