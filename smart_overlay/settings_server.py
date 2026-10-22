"""Локальный веб-сервер окна настроек и запуск этого окна (Edge в режиме приложения).

Сервер слушает только 127.0.0.1 на случайном порту. Каждый запрос проверяется по токену, а заголовок Host —
по адресу сервера: так сторонние сайты в браузере не смогут ни прочитать, ни изменить настройки.
"""

import copy
import hmac
import json
import logging
import os
import secrets
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import CONFIG_PATH, LOCAL_DIR, LOG_PATH, ConfigStore, restart_needed, schema_json, validate
from .i18n import get_language, msg, web_messages

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
STATIC_FILES = {
    "/": (PACKAGE_DIR / "web" / "settings.html", "text/html; charset=utf-8"),
    "/icon.png": (PACKAGE_DIR / "assets" / "icon.png", "image/png"),
}
CONTROLS = ("listening", "overlay_visible", "move_mode")


class _Rejected(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("validation failed")
        self.errors = errors


def _find_edge() -> str | None:
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base and (path := Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe").exists():
            return str(path)
    return None


def open_app_window(url: str) -> None:
    """Открывает страницу отдельным окном без адресной строки; без Edge — в браузере по умолчанию."""
    edge = _find_edge()
    if edge is None:
        webbrowser.open(url)
        return
    subprocess.Popen(
        [
            edge,
            f"--app={url}",
            "--window-size=1000,920",
            f"--user-data-dir={LOCAL_DIR / 'webui'}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


class SettingsServer:
    def __init__(
        self,
        store: ConfigStore,
        on_changed: Callable[[dict, dict], None],
        get_status: Callable[[], dict],
        on_control: Callable[[str, bool], None],
        set_hotkeys_enabled: Callable[[bool], None],
    ):
        self._store = store
        self._on_changed = on_changed
        self._get_status = get_status
        self._on_control = on_control
        self._set_hotkeys_enabled = set_hotkeys_enabled
        self.token = secrets.token_urlsafe(18)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.owner = self
        self.port = self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> None:
        threading.Thread(target=self._httpd.serve_forever, name="settings-http", daemon=True).start()
        log.info("Настройки: %s", f"http://127.0.0.1:{self.port}/")

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def open_window(self) -> None:
        open_app_window(self.url)

    # ---------- API ----------

    def get_settings(self) -> dict:
        cfg = self._store.get()
        values = copy.deepcopy(cfg)
        values["gemini"]["api_key"] = None  # сам ключ в браузер не отдаём
        return {
            "schema": schema_json(),
            "values": values,
            "version": self._store.version,
            "language": get_language(),
            "messages": web_messages(),
            "api_key_set": bool(cfg["gemini"]["api_key"]),
            "api_key_env": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
            "config_path": str(CONFIG_PATH),
            "log_path": str(LOG_PATH),
        }

    def get_devices(self) -> dict:
        from .audio import list_loopback_devices
        from .screenshot import list_monitors

        try:
            audio = [d["name"] for d in list_loopback_devices()]
        except Exception:
            log.exception("Не удалось получить список аудиоустройств")
            audio = []
        return {"audio": audio, "monitors": list_monitors()}

    def get_status(self) -> dict:
        return self._get_status()

    def save_settings(self, body: dict) -> tuple[int, dict]:
        """Принимает только изменённые поля — так правки из трея и мыши не затираются старыми значениями."""

        def change(cfg: dict) -> dict:
            new, errors = validate(body.get("values") or {}, cfg)
            if errors:
                raise _Rejected(errors)
            return new

        try:
            old, new = self._store.update(change)
        except _Rejected as e:
            return 400, {"ok": False, "errors": e.errors}
        self._on_changed(old, new)
        restart = restart_needed(old, new)
        log.info("Настройки сохранены%s", " — перезапуск распознавания" if restart else "")
        return 200, {"ok": True, "restart": restart, "version": self._store.version}

    def control(self, body: dict) -> tuple[int, dict]:
        name = body.get("name")
        if name not in CONTROLS:
            return 400, {"ok": False}
        self._on_control(name, bool(body.get("value")))
        return 200, {"ok": True}

    def test_gemini(self, body: dict) -> tuple[int, dict]:
        from .gemini_client import ping, resolve_api_key

        cfg = self._store.get()["gemini"]
        key = body.get("api_key") or resolve_api_key(cfg)
        model = body.get("model") or cfg["answer_model"]
        if not key:
            return 200, {"ok": False, "message": msg("test.no_key")}
        started = time.monotonic()
        try:
            ping(key, model)
        except Exception as e:
            return 200, {"ok": False, "message": str(e)[:300]}
        return 200, {"ok": True, "message": msg("test.ok", model=model, seconds=f"{time.monotonic() - started:.1f}")}

    def set_hotkeys(self, body: dict) -> tuple[int, dict]:
        # Пока пользователь записывает сочетание, наши глобальные клавиши отключены — иначе Windows
        # отдаст нажатие программе, а не странице настроек.
        self._set_hotkeys_enabled(bool(body.get("enabled", True)))
        return 200, {"ok": True}

    def open_settings(self, body: dict) -> tuple[int, dict]:
        self.open_window()
        return 200, {"ok": True}


class _Handler(BaseHTTPRequestHandler):
    server_version = "SmartOverlay"

    def log_message(self, fmt, *args) -> None:
        log.debug("http: " + fmt, *args)

    @property
    def owner(self) -> SettingsServer:
        return self.server.owner

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        port = self.owner.port
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self, token: str | None) -> bool:
        return hmac.compare_digest(token or "", self.owner.token)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        if url.path in STATIC_FILES:
            if url.path == "/" and not self._token_ok(parse_qs(url.query).get("t", [""])[0]):
                return self._send(403, msg("server.open_from_tray").encode(), "text/plain; charset=utf-8")
            path, content_type = STATIC_FILES[url.path]
            return self._send(200, path.read_bytes(), content_type)
        if not self._token_ok(self.headers.get("X-Token")):
            return self._json(403, {"ok": False, "error": "bad token"})
        routes = {
            "/api/settings": self.owner.get_settings,
            "/api/devices": self.owner.get_devices,
            "/api/status": self.owner.get_status,
        }
        if url.path not in routes:
            return self._json(404, {"ok": False})
        self._json(200, routes[url.path]())

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        if not self._host_ok() or not self._token_ok(self.headers.get("X-Token")):
            return self._json(403, {"ok": False, "error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad json"})
        routes = {
            "/api/settings": self.owner.save_settings,
            "/api/control": self.owner.control,
            "/api/test-gemini": self.owner.test_gemini,
            "/api/hotkeys": self.owner.set_hotkeys,
            "/api/open-settings": self.owner.open_settings,
        }
        if url.path not in routes:
            return self._json(404, {"ok": False})
        self._json(*routes[url.path](body))
