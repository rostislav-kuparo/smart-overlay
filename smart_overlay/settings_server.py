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

from .config import CONFIG_PATH, LOCAL_DIR, LOG_PATH, restart_needed, save_config, schema_json, validate

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
STATIC_FILES = {
    "/": (PACKAGE_DIR / "web" / "settings.html", "text/html; charset=utf-8"),
    "/icon.png": (PACKAGE_DIR / "assets" / "icon.png", "image/png"),
}


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
            "--window-size=980,900",
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
        get_config: Callable[[], dict],
        on_saved: Callable[[dict], None],
        get_status: Callable[[], tuple[str, str]],
        set_hotkeys_enabled: Callable[[bool], None],
    ):
        self._get_config = get_config
        self._on_saved = on_saved
        self._get_status = get_status
        self._set_hotkeys_enabled = set_hotkeys_enabled
        self._save_lock = threading.Lock()
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
        cfg = self._get_config()
        values = copy.deepcopy(cfg)
        values["gemini"]["api_key"] = None  # сам ключ в браузер не отдаём
        return {
            "schema": schema_json(),
            "values": values,
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
        text, kind = self._get_status()
        return {"text": text, "kind": kind}

    def save_settings(self, body: dict) -> tuple[int, dict]:
        with self._save_lock:
            current = self._get_config()
            new, errors = validate(body.get("values") or {}, current)
            if errors:
                return 400, {"ok": False, "errors": errors}
            save_config(new)
            restart = restart_needed(current, new)
            self._on_saved(new)
        log.info("Настройки сохранены%s", " — перезапуск распознавания" if restart else "")
        return 200, {"ok": True, "restart": restart}

    def test_gemini(self, body: dict) -> dict:
        from .gemini_client import ping

        cfg = self._get_config()["gemini"]
        key = body.get("api_key") or cfg["api_key"] or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        model = body.get("model") or cfg["answer_model"]
        if not key:
            return {"ok": False, "message": "Ключ не задан"}
        started = time.monotonic()
        try:
            ping(key, model)
        except Exception as e:
            return {"ok": False, "message": str(e)[:300]}
        return {"ok": True, "message": f"Ключ работает: {model} ответила за {time.monotonic() - started:.1f} с"}

    def set_hotkeys(self, body: dict) -> dict:
        # Пока пользователь записывает сочетание, наши глобальные клавиши отключены — иначе Windows
        # отдаст нажатие программе, а не странице настроек.
        self._set_hotkeys_enabled(bool(body.get("enabled", True)))
        return {"ok": True}

    def open_settings(self, body: dict) -> dict:
        self.open_window()
        return {"ok": True}


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
                message = "Откройте настройки через значок Smart Overlay в трее."
                return self._send(403, message.encode(), "text/plain; charset=utf-8")
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
        if url.path == "/api/settings":
            return self._json(*self.owner.save_settings(body))
        routes = {
            "/api/test-gemini": self.owner.test_gemini,
            "/api/hotkeys": self.owner.set_hotkeys,
            "/api/open-settings": self.owner.open_settings,
        }
        if url.path not in routes:
            return self._json(404, {"ok": False})
        self._json(200, routes[url.path](body))
