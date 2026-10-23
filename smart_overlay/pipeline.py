"""Конвейер: звук → фразы → текст → вопрос → скриншот → Gemini → сигналы для оверлея.

Потоки:
- audio  — захват звука и нарезка на фразы (Silero VAD);
- asr    — распознавание фраз на GPU, поиск кандидатов в вопросы;
- ask-*  — проверка вопроса и потоковый ответ Gemini (несколько штук параллельно).

Все фразы и все срабатывания детектора (с итогом: ответ, «не вопрос», повтор, ошибка) пишутся в историю.
"""

import itertools
import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from difflib import SequenceMatcher

from PySide6.QtCore import QObject, Signal

from .audio import LoopbackCapture, PhraseSegmenter, SileroVAD
from .config import active_preset
from .detector import looks_like_question
from .gemini_client import GeminiClient
from .history import HistoryStore
from .i18n import msg
from .screenshot import grab_screen
from .transcriber import Transcriber

log = logging.getLogger(__name__)

REPEAT_WINDOW_S = 60
REPEAT_SIMILARITY = 0.85

# Номера ответов общие для всех конвейеров: после перезапуска (смена настроек) хвост старого ответа
# не перепутается с новым.
_answer_ids = itertools.count(1)


def _ms_since(moment: float) -> int:
    return int((time.monotonic() - moment) * 1000)


class Events(QObject):
    """Сигналы из рабочих потоков в GUI-поток (Qt сам ставит их в очередь главного потока)."""

    status = Signal(str, str)  # текст, вид: ok | busy | idle | error
    transcript = Signal(str)
    question = Signal(int, str)  # номер ответа, вопрос — открывается новое окно ответа
    answer_chunk = Signal(int, str)
    answer_done = Signal(int)
    answer_failed = Signal(int, str)  # номер ответа, текст ошибки
    hotkey = Signal(str)


class Pipeline:
    def __init__(self, cfg: dict, events: Events, history: HistoryStore):
        self._cfg = cfg
        self._events = events
        self._history = history
        self._phrases: queue.Queue = queue.Queue()
        self._recent: deque[tuple[float, str]] = deque()
        self._recent_lock = threading.Lock()
        self._workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ask")
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._answering = 0  # сколько ответов сейчас пишется (каждый в своём окне)
        self._answering_lock = threading.Lock()
        self._last_question = ("", 0.0)
        self._gemini: GeminiClient | None = None
        self._gemini_error = ""
        self._audio_thread: threading.Thread | None = None
        self._listening = False  # модель загружена и звук захватывается

    # ---------- управление ----------

    def start(self) -> None:
        threading.Thread(target=self._run_asr, name="asr", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._workers.shutdown(wait=False, cancel_futures=True)
        if self._audio_thread is not None:
            self._audio_thread.join(timeout=2)  # даём закрыть аудиопоток до выхода интерпретатора

    def update_config(self, cfg: dict) -> None:
        """Применяет настройки, не требующие перезагрузки модели (Gemini, пресет, скриншот, детектор)."""
        self._cfg = cfg
        self._gemini = None

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
        self.refresh_status()

    def refresh_status(self) -> None:
        """Показывает обычное состояние (слушаю / пауза) — например, после смены языка или режима."""
        if self._listening:
            self._status(*self._idle_status())

    def _idle_status(self) -> tuple[str, str]:
        if self._paused.is_set():
            return msg("status.paused"), "idle"
        if not self._cfg["detector"]["auto_answer"]:
            return msg("status.listening_manual"), "ok"
        return msg("status.listening"), "ok"

    def ask_now(self) -> None:
        """Ручной запрос по горячей клавише: последний вопрос из разговора или то, что на экране."""
        self._workers.submit(self._answer, None, kind="manual")

    def ask_again(self, event_id: int) -> Future | None:
        """Повторяет вопрос из истории — с тем же контекстом разговора и тем же скриншотом, если он сохранён."""
        event = self._history.event(event_id)
        if event is None:
            return None
        return self._workers.submit(
            self._answer, event["question"] or event["heard"] or None, kind="reask", heard=event["heard"],
            context=event["context"], screenshot=self._history.screenshot(event_id), grab=False,
        )

    def answer_phrase(self, phrase_id: int) -> Future | None:
        """Отвечает на фразу из расшифровки, которую детектор пропустил, — с контекстом того момента."""
        phrase = self._history.phrase(phrase_id)
        if phrase is None:
            return None
        context = self._history.context_before(phrase, self._cfg["detector"]["context_seconds"])
        return self._workers.submit(
            self._answer, phrase["text"], kind="reask", heard=phrase["text"], context=context,
            phrase_id=phrase_id, grab=False,
        )

    # ---------- потоки ----------

    def _run_asr(self) -> None:
        self._status(msg("status.loading_model"), "busy")
        transcriber = self._load_transcriber()
        if transcriber is None or self._stop.is_set():
            return
        self._audio_thread = threading.Thread(target=self._run_audio, name="audio", daemon=True)
        self._audio_thread.start()

        while not self._stop.is_set():
            try:
                audio, ended_at = self._phrases.get(timeout=0.5)
            except queue.Empty:
                continue
            started = time.monotonic()
            try:
                text = transcriber.transcribe(audio)
            except Exception:
                log.exception("Ошибка распознавания")
                continue
            if not text:
                continue
            log.info("Фраза (%.1f c звука, распознано за %.2f c): %s", len(audio) / 16000, time.monotonic() - started, text)
            lines = self._remember(text)
            phrase_id = self._log_phrase(text)
            self._events.transcript.emit(text)
            if self._cfg["detector"]["auto_answer"] and looks_like_question(text):
                self._workers.submit(self._on_candidate, lines, ended_at, phrase_id)

    def _load_transcriber(self) -> Transcriber | None:
        w = self._cfg["whisper"]
        language = self._cfg["language"]["speech"]
        try:
            return Transcriber(w["model"], w["device"], w["compute_type"], language, w["beam_size"])
        except Exception as e:
            log.exception("Whisper не запустился на %s", w["device"])
            if w["device"] == "cpu":
                self._status(msg("status.whisper_failed", error=e), "error")
                return None
        self._status(msg("status.cpu_fallback"), "busy")
        try:
            return Transcriber(w["model"], "cpu", "int8", language, w["beam_size"])
        except Exception as e:
            log.exception("Whisper не запустился и на CPU")
            self._status(msg("status.whisper_failed", error=e), "error")
            return None

    def _run_audio(self) -> None:
        a = self._cfg["audio"]
        try:
            capture = LoopbackCapture(a["device"])
            segmenter = PhraseSegmenter(
                SileroVAD(), a["vad_threshold"], a["min_silence_ms"], a["min_speech_ms"], a["max_phrase_s"]
            )
            capture.start()
        except Exception as e:
            log.exception("Не удалось начать захват звука")
            self._status(msg("status.no_audio", error=e), "error")
            return
        self._listening = True
        self.refresh_status()
        try:
            while not self._stop.is_set():
                chunk = capture.read(timeout=0.1)
                if self._paused.is_set():
                    segmenter.reset()
                    continue
                phrases = segmenter.feed(chunk) if chunk is not None else [segmenter.flush_if_idle()]
                for phrase in phrases:
                    if phrase is not None:
                        self._phrases.put((phrase, time.monotonic()))
        finally:
            capture.close()

    # ---------- вопросы и ответы ----------

    def _on_candidate(self, lines: list[str], ended_at: float, phrase_id: int | None) -> None:
        heard = lines[-1]
        question = heard
        check_ms = None
        record = {"kind": "auto", "heard": heard, "context": lines, "phrase_id": phrase_id}
        if self._cfg["detector"]["use_llm_check"]:
            gemini = self._get_gemini()
            if gemini is None:
                self._log_event(status="error", error=self._gemini_error, **record)
                return
            check_model = self._cfg["gemini"]["check_model"]
            transcript = "\n".join([f"— {line}" for line in lines[:-1]] + [f">>> {lines[-1]}"])
            started = time.monotonic()
            try:
                question = gemini.check_question(transcript)
            except Exception as e:
                log.exception("Ошибка проверки вопроса")
                self._status(msg("status.gemini_error", error=e), "error")
                self._log_event(status="error", error=str(e), model=check_model, **record)
                return
            check_ms = _ms_since(started)
            log.info("Проверка вопроса за %.2f c → %s", check_ms / 1000, question or "не вопрос")
            if not question:
                self._log_event(status="not_question", check_ms=check_ms, model=check_model, **record)
                return
        if self._is_repeat(question):
            log.info("Повтор недавнего вопроса — пропускаю")
            self._log_event(status="repeat", question=question, check_ms=check_ms, **record)
            return
        # Контекст для ответа берём свежий: пока шла проверка, могло прийти продолжение вопроса.
        self._answer(question, ended_at, kind="auto", heard=heard, check_ms=check_ms, phrase_id=phrase_id)

    def _is_repeat(self, question: str) -> bool:
        previous, asked_at = self._last_question
        now = time.monotonic()
        self._last_question = (question, now)
        return (
            now - asked_at < REPEAT_WINDOW_S
            and SequenceMatcher(None, previous.lower(), question.lower()).ratio() >= REPEAT_SIMILARITY
        )

    def _answer(
        self,
        question: str | None,
        ended_at: float | None = None,
        *,
        kind: str,
        heard: str = "",
        context: list[str] | None = None,
        check_ms: int | None = None,
        phrase_id: int | None = None,
        screenshot: bytes | None = None,
        grab: bool = True,
    ) -> None:
        lines = context if context is not None else self._recent_lines()
        record = {"kind": kind, "heard": heard, "question": question or "", "context": lines,
                  "check_ms": check_ms, "phrase_id": phrase_id}
        gemini = self._get_gemini()
        if gemini is None:
            self._log_event(status="error", error=self._gemini_error, **record)
            return
        answer_id = next(_answer_ids)

        shot_cfg = self._cfg["screenshot"]
        if screenshot is None and grab and shot_cfg["enabled"]:
            try:
                screenshot = grab_screen(shot_cfg["monitor"], shot_cfg["max_side"], shot_cfg["jpeg_quality"])
            except Exception:
                log.exception("Скриншот не удался — спрашиваю без него")
        event_id = self._log_event(status="answering", model=self._cfg["gemini"]["answer_model"], **record)
        if event_id is not None and screenshot and self._cfg["history"]["save_screenshots"]:
            try:
                self._history.save_screenshot(event_id, screenshot)
            except Exception:
                log.exception("Не удалось сохранить скриншот в историю")

        # Каждый ответ пишется в своё окно до конца — новый вопрос старый ответ не прерывает.
        self._events.question.emit(answer_id, question or msg("overlay.manual_question"))
        self._status(msg("status.thinking"), "busy")
        with self._answering_lock:
            self._answering += 1
        started = time.monotonic()
        wait_ms = None
        parts: list[str] = []
        failed = False
        try:
            for chunk in gemini.answer(self._build_request(question, lines), screenshot):
                if wait_ms is None:
                    wait_ms = _ms_since(ended_at if ended_at is not None else started)
                    if ended_at is not None:
                        log.info("Первые слова ответа через %.2f c после конца фразы", wait_ms / 1000)
                parts.append(chunk)
                self._events.answer_chunk.emit(answer_id, chunk)
        except Exception as e:
            failed = True
            log.exception("Ошибка ответа Gemini")
            self._status(msg("status.gemini_error", error=e), "error")
            self._events.answer_failed.emit(answer_id, msg("status.gemini_error", error=e))
            self._log_update(event_id, status="error", error=str(e), answer="".join(parts), answer_ms=_ms_since(started))
        else:
            self._events.answer_done.emit(answer_id)
            self._log_update(event_id, status="answered", answer="".join(parts), wait_ms=wait_ms,
                             answer_ms=_ms_since(started))
        finally:
            with self._answering_lock:
                self._answering -= 1
                still_answering = self._answering
        if not failed and not still_answering:
            self.refresh_status()

    def _build_request(self, question: str | None, lines: list[str]) -> str:
        transcript = "\n".join(f"— {line}" for line in lines) or "(пока ничего не распознано)"
        if question:
            task = f"Вопрос: {question}"
        else:
            task = (
                "Явный вопрос не выделен. Ответь на последний вопрос из разговора, "
                "а если его нет — помоги с тем, что видно на скриншоте."
            )
        return f"Расшифровка последних реплик:\n{transcript}\n\n{task}"

    def _get_gemini(self) -> GeminiClient | None:
        if self._gemini is None:
            try:
                self._gemini = GeminiClient(self._cfg)
            except Exception as e:
                log.error("Gemini: %s", e)
                self._gemini_error = str(e)
                self._status(msg("status.gemini_error", error=e), "error")
                return None
        return self._gemini

    # ---------- история (запись) ----------

    def _log_phrase(self, text: str) -> int | None:
        if not self._cfg["history"]["enabled"]:
            return None
        try:
            return self._history.add_phrase(text)
        except Exception:
            log.exception("Не удалось записать фразу в историю")
            return None

    def _log_event(self, **fields) -> int | None:
        if not self._cfg["history"]["enabled"]:
            return None
        preset = active_preset(self._cfg)
        try:
            return self._history.add_event(preset=preset["name"] if preset else "", **fields)
        except Exception:
            log.exception("Не удалось записать событие в историю")
            return None

    def _log_update(self, event_id: int | None, **fields) -> None:
        if event_id is None:
            return
        try:
            self._history.update_event(event_id, **fields)
        except Exception:
            log.exception("Не удалось обновить событие в истории")

    # ---------- контекст разговора ----------

    def _remember(self, text: str) -> list[str]:
        """Добавляет фразу в контекст и возвращает его (последняя строка — эта фраза)."""
        with self._recent_lock:
            self._recent.append((time.monotonic(), text))
        return self._recent_lines()

    def _recent_lines(self) -> list[str]:
        horizon = time.monotonic() - self._cfg["detector"]["context_seconds"]
        with self._recent_lock:
            while self._recent and self._recent[0][0] < horizon:
                self._recent.popleft()
            return [text for _, text in self._recent]

    def _status(self, text: str, kind: str) -> None:
        self._events.status.emit(text, kind)
