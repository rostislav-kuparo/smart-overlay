"""Остановка ответа: поток выходит, соединение рвётся, в истории остаётся написанная часть."""

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from smart_overlay import pipeline
from smart_overlay.config import DEFAULTS
from smart_overlay.gemini_client import GeminiClient
from smart_overlay.history import HistoryStore
from smart_overlay.i18n import set_language
from smart_overlay.pipeline import Events, Pipeline, cancel_answer


class FakeGemini:
    def __init__(self, on_chunk=lambda index: None):
        self.closed = False
        self._on_chunk = on_chunk

    def answer(self, request, screenshot):
        try:
            for index in range(5):
                self._on_chunk(index)
                yield f"кусок{index} "
        finally:
            self.closed = True


@pytest.fixture
def setup(tmp_path):
    set_language("ru")
    cfg = copy.deepcopy(DEFAULTS)
    cfg["screenshot"]["enabled"] = False
    history = HistoryStore(tmp_path / "history.sqlite3", tmp_path / "shots")
    runner = Pipeline(cfg, Events(), history)
    yield runner, history
    runner.stop()
    history.close()


def test_stop_ends_the_stream_and_keeps_the_written_part(setup):
    runner, history = setup
    answer_ids = []
    runner._events.question.connect(lambda answer_id, question: answer_ids.append(answer_id))
    chunks = []
    runner._events.answer_chunk.connect(lambda answer_id, chunk: chunks.append(chunk))
    fake = FakeGemini(lambda index: index == 2 and cancel_answer(answer_ids[0]))
    runner._gemini = fake

    runner._answer("Вопрос?", kind="manual")

    assert fake.closed
    assert chunks == ["кусок0 ", "кусок1 "]
    event = history.events()[0]
    assert event["status"] == "stopped" and event["answer"] == "кусок0 кусок1 "
    assert pipeline._running == {}


def test_finished_answer_is_not_marked_stopped(setup):
    runner, history = setup
    runner._gemini = FakeGemini()
    runner._answer("Вопрос?", kind="manual")
    assert history.events()[0]["status"] == "answered"
    cancel_answer(10**9)  # уже закрытое окно: ничего не должно случиться


def test_moving_a_window_keeps_the_gemini_client(setup):
    runner, _ = setup
    client = runner._gemini = object()
    moved = copy.deepcopy(runner._cfg)
    moved["answers"]["x"] = 100
    runner.update_config(moved)
    assert runner._gemini is client

    changed = copy.deepcopy(moved)
    changed["gemini"]["answer_thinking"] = "high"
    runner.update_config(changed)
    assert runner._gemini is not client


class Server(ThreadingHTTPServer):
    daemon_threads = True
    sent = 0
    dropped = threading.Event()


class StreamHandler(BaseHTTPRequestHandler):
    """Как Gemini: пишет события SSE, пока клиент не закроет соединение."""

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        body = {"candidates": [{"content": {"parts": [{"text": "слово "}], "role": "model"}}]}
        try:
            for _ in range(400):
                self.wfile.write(b"data: " + json.dumps(body).encode() + b"\r\n\r\n")
                self.wfile.flush()
                self.server.sent += 1
                time.sleep(0.01)
        except OSError:
            self.server.dropped.set()

    def log_message(self, *args):
        pass


def test_closing_the_stream_drops_the_connection(monkeypatch):
    server = Server(("127.0.0.1", 0), StreamHandler)
    server.dropped = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    cfg = copy.deepcopy(DEFAULTS)
    cfg["gemini"]["api_key"] = "test"
    try:
        stream = GeminiClient(cfg).answer("вопрос", None)
        assert next(stream) == "слово "
        stream.close()
        assert server.dropped.wait(3), "сервер продолжил писать ответ после остановки"
        assert server.sent < 400
    finally:
        server.shutdown()
