import copy
import json
import time

import pytest

from smart_overlay.config import DEFAULTS
from smart_overlay.history import HistoryStore
from smart_overlay.i18n import set_language
from smart_overlay.pipeline import Events, Pipeline


@pytest.fixture
def store(tmp_path):
    set_language("ru")
    history = HistoryStore(tmp_path / "history.sqlite3", tmp_path / "shots")
    yield history
    history.close()


def test_events_filters_search_and_days(store):
    phrase = store.add_phrase("Что выведет эта функция?")
    store.add_event(kind="auto", status="answered", heard="Что выведет эта функция?", answer="Выведет 42",
                    context=["Привет", "Что выведет эта функция?"], phrase_id=phrase)
    store.add_event(kind="auto", status="not_question", heard="Я выпил кофе?")
    store.add_event(kind="manual", status="error", error="нет ключа")

    assert [e["status"] for e in store.events()] == ["error", "not_question", "answered"]  # новые сверху
    assert [e["status"] for e in store.events(filter="answered")] == ["answered"]
    assert [e["status"] for e in store.events(filter="rejected")] == ["not_question"]
    assert [e["status"] for e in store.events(filter="manual")] == ["error"]
    assert [e["status"] for e in store.events(query="ФУНКЦИЯ")] == ["answered"]  # кириллица без учёта регистра
    assert store.events(filter="answered")[0]["context"] == ["Привет", "Что выведет эта функция?"]

    today = time.strftime("%Y-%m-%d")
    assert store.days() == [{"day": today, "events": 3, "phrases": 1}]
    assert store.events(day="2000-01-01") == []
    phrases = store.phrases(today)
    assert phrases[0]["text"] == "Что выведет эта функция?" and phrases[0]["event_status"] == "answered"


def test_context_screenshots_delete_prune_clear(store, tmp_path):
    first = store.add_phrase("Раз")
    second = store.add_phrase("Два")
    assert store.context_before(store.phrase(second), 60) == ["Раз", "Два"]
    assert store.context_before(store.phrase(first), 60) == ["Раз"]

    event = store.add_event(kind="auto", status="answered")
    store.save_screenshot(event, b"jpeg")
    assert store.event(event)["screenshot"] == 1 and store.screenshot(event) == b"jpeg"
    store.delete_event(event)
    assert store.event(event) is None and store.screenshot(event) is None

    old = store.add_event(kind="auto", status="answered", ts=time.time() - 40 * 86400)
    store.save_screenshot(old, b"old")
    store.prune(30)
    assert store.event(old) is None and not (tmp_path / "shots" / f"{old}.jpg").exists()
    assert len(store.phrases(time.strftime("%Y-%m-%d"))) == 2

    store.clear()
    assert store.events() == [] and store.days() == []


def test_export(store):
    store.add_event(kind="auto", status="answered", heard="Как дела?", question="Как у вас дела?", answer="**Хорошо**",
                    context=["Как дела?"])
    body, content_type, filename = store.export("md")
    text = body.decode()
    assert content_type.startswith("text/markdown") and filename.endswith(".md")
    assert "Как у вас дела?" in text and "**Хорошо**" in text and "> Как дела?" in text
    body, content_type, filename = store.export("json")
    data = json.loads(body)
    assert data["events"][0]["answer"] == "**Хорошо**" and filename.endswith(".json")


class FakeGemini:
    def __init__(self, check=None, fail=False):
        self.check = check or {}
        self.fail = fail
        self.requests = []

    def check_question(self, transcript):
        last = transcript.splitlines()[-1].removeprefix(">>> ")
        return self.check.get(last, last)

    def answer(self, request, screenshot):
        self.requests.append((request, screenshot))
        if self.fail:
            raise RuntimeError("квота исчерпана")
        yield from ["**Ответ** ", "готов."]


def make_pipeline(store, gemini):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["screenshot"]["enabled"] = False
    cfg["presets"] = {"active": "Созвон", "items": [{"name": "Созвон", "context": "проект"}]}
    pipeline = Pipeline(cfg, Events(), store)
    pipeline._gemini = gemini
    return pipeline


def test_pipeline_records_every_outcome(store):
    gemini = FakeGemini(check={"Я выпил кофе?": None})
    pipeline = make_pipeline(store, gemini)

    def hear(text):
        lines = pipeline._remember(text)
        pipeline._on_candidate(lines, time.monotonic(), pipeline._log_phrase(text))

    hear("Расскажите про потоки")
    hear("Я выпил кофе?")
    hear("Расскажите про потоки")  # повтор
    pipeline._answer(None, kind="manual")

    by_status = {e["status"]: e for e in store.events()}
    answered = [e for e in store.events() if e["status"] == "answered"]
    assert [e["kind"] for e in answered] == ["manual", "auto"]
    auto = answered[1]
    assert auto["answer"] == "**Ответ** готов." and auto["preset"] == "Созвон" and auto["phrase_id"]
    assert auto["check_ms"] is not None and auto["wait_ms"] is not None and auto["answer_ms"] is not None
    assert by_status["not_question"]["heard"] == "Я выпил кофе?"
    assert by_status["repeat"]["question"] == "Расскажите про потоки"
    assert len(store.phrases(time.strftime("%Y-%m-%d"))) == 3

    gemini.fail = True
    pipeline._answer("Ещё вопрос", kind="manual")
    error = store.events(filter="errors")[0]
    assert error["error"] == "квота исчерпана" and error["question"] == "Ещё вопрос"


def test_ask_again_and_answer_phrase_use_saved_context(store):
    gemini = FakeGemini()
    pipeline = make_pipeline(store, gemini)
    store.add_phrase("Начнём")
    phrase = store.add_phrase("А что такое GIL")
    event = store.add_event(kind="auto", status="answered", heard="А что такое GIL", question="Что такое GIL в Python?",
                            context=["Начнём", "А что такое GIL"])
    store.save_screenshot(event, b"shot")

    pipeline.ask_again(event).result()
    pipeline.answer_phrase(phrase).result()

    request, screenshot = gemini.requests[0]
    assert "Вопрос: Что такое GIL в Python?" in request and "— Начнём" in request and screenshot == b"shot"
    request, screenshot = gemini.requests[1]
    assert "Вопрос: А что такое GIL" in request and "— Начнём" in request and screenshot is None
    reasked = [e for e in store.events() if e["kind"] == "reask"]
    assert len(reasked) == 2 and all(e["status"] == "answered" for e in reasked)


def test_history_disabled_records_nothing(store):
    pipeline = make_pipeline(store, FakeGemini())
    pipeline._cfg["history"]["enabled"] = False
    lines = pipeline._remember("Как дела?")
    pipeline._on_candidate(lines, time.monotonic(), pipeline._log_phrase("Как дела?"))
    assert store.events() == [] and store.days() == []
