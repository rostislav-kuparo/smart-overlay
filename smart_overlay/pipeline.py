"""Конвейер: звук → фразы → текст → вопрос → скриншот → Gemini → сигналы для оверлея.

Потоки:
- audio  — захват звука и нарезка на фразы (Silero VAD);
- asr    — распознавание фраз на GPU, поиск кандидатов в вопросы;
- ask-*  — проверка вопроса и потоковый ответ Gemini (несколько штук параллельно).
"""

import itertools
import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

from PySide6.QtCore import QObject, Signal

from .audio import LoopbackCapture, PhraseSegmenter, SileroVAD
from .detector import looks_like_question
from .gemini_client import GeminiClient
from .screenshot import grab_screen
from .transcriber import Transcriber

log = logging.getLogger(__name__)

REPEAT_WINDOW_S = 60
REPEAT_SIMILARITY = 0.85

# Номера ответов общие для всех конвейеров: после перезапуска (смена настроек) хвост старого ответа
# не перепутается с новым.
_answer_ids = itertools.count(1)


class Events(QObject):
    """Сигналы из рабочих потоков в GUI-поток (Qt сам ставит их в очередь главного потока)."""

    status = Signal(str, str)  # текст, вид: ok | busy | idle | error
    transcript = Signal(str)
    question = Signal(int, str)  # номер ответа, вопрос
    answer_chunk = Signal(int, str)
    hotkey = Signal(str)


class Pipeline:
    def __init__(self, cfg: dict, events: Events):
        self._cfg = cfg
        self._events = events
        self._phrases: queue.Queue = queue.Queue()
        self._history: deque[tuple[float, str]] = deque()
        self._history_lock = threading.Lock()
        self._workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ask")
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._answer_id = 0
        self._last_question = ("", 0.0)
        self._gemini: GeminiClient | None = None
        self._audio_thread: threading.Thread | None = None

    # ---------- управление ----------

    def start(self) -> None:
        threading.Thread(target=self._run_asr, name="asr", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._workers.shutdown(wait=False, cancel_futures=True)
        if self._audio_thread is not None:
            self._audio_thread.join(timeout=2)  # даём закрыть аудиопоток до выхода интерпретатора

    def update_config(self, cfg: dict) -> None:
        """Применяет настройки, не требующие перезагрузки модели (Gemini, промпты, скриншот, детектор)."""
        self._cfg = cfg
        self._gemini = None

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def toggle_pause(self) -> None:
        if self._paused.is_set():
            self._paused.clear()
            self._status("Слушаю", "ok")
        else:
            self._paused.set()
            self._status("Пауза — звук не обрабатывается", "idle")

    def ask_now(self) -> None:
        """Ручной запрос по горячей клавише: последний вопрос из разговора или то, что на экране."""
        self._workers.submit(self._answer, None)

    # ---------- потоки ----------

    def _run_asr(self) -> None:
        self._status("Загрузка модели распознавания (при первом запуске — скачивание, несколько минут)…", "busy")
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
            self._events.transcript.emit(text)
            if looks_like_question(text):
                self._workers.submit(self._on_candidate, lines, ended_at)

    def _load_transcriber(self) -> Transcriber | None:
        w = self._cfg["whisper"]
        try:
            return Transcriber(w["model"], w["device"], w["compute_type"], w["language"], w["beam_size"])
        except Exception as e:
            log.exception("Whisper не запустился на %s", w["device"])
            if w["device"] == "cpu":
                self._status(f"Whisper не запустился: {e}", "error")
                return None
        self._status("GPU недоступен — распознаю на процессоре (медленнее)…", "busy")
        try:
            return Transcriber(w["model"], "cpu", "int8", w["language"], w["beam_size"])
        except Exception as e:
            log.exception("Whisper не запустился и на CPU")
            self._status(f"Whisper не запустился: {e}", "error")
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
            self._status(f"Нет доступа к звуку: {e}", "error")
            return
        self._status("Слушаю", "ok")
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

    def _on_candidate(self, lines: list[str], ended_at: float) -> None:
        question = lines[-1]
        if self._cfg["detector"]["use_llm_check"]:
            gemini = self._get_gemini()
            if gemini is None:
                return
            transcript = "\n".join([f"— {line}" for line in lines[:-1]] + [f">>> {lines[-1]}"])
            started = time.monotonic()
            try:
                question = gemini.check_question(transcript)
            except Exception as e:
                log.exception("Ошибка проверки вопроса")
                self._status(f"Gemini: {e}", "error")
                return
            log.info("Проверка вопроса за %.2f c → %s", time.monotonic() - started, question or "не вопрос")
            if not question:
                return
        if self._is_repeat(question):
            log.info("Повтор недавнего вопроса — пропускаю")
            return
        self._answer(question, ended_at)

    def _is_repeat(self, question: str) -> bool:
        previous, asked_at = self._last_question
        now = time.monotonic()
        self._last_question = (question, now)
        return (
            now - asked_at < REPEAT_WINDOW_S
            and SequenceMatcher(None, previous.lower(), question.lower()).ratio() >= REPEAT_SIMILARITY
        )

    def _answer(self, question: str | None, ended_at: float | None = None) -> None:
        gemini = self._get_gemini()
        if gemini is None:
            return
        answer_id = self._answer_id = next(_answer_ids)

        screenshot = None
        shot_cfg = self._cfg["screenshot"]
        if shot_cfg["enabled"]:
            try:
                screenshot = grab_screen(shot_cfg["monitor"], shot_cfg["max_side"], shot_cfg["jpeg_quality"])
            except Exception:
                log.exception("Скриншот не удался — спрашиваю без него")

        self._events.question.emit(answer_id, question or "Ручной запрос: последний вопрос или то, что на экране")
        self._status("Думаю…", "busy")
        first_chunk = True
        try:
            for chunk in gemini.answer(self._build_request(question), screenshot):
                if answer_id != self._answer_id:
                    return  # пришёл новый вопрос — этот ответ больше не нужен
                if first_chunk and ended_at is not None:
                    log.info("Первые слова ответа через %.2f c после конца фразы", time.monotonic() - ended_at)
                first_chunk = False
                self._events.answer_chunk.emit(answer_id, chunk)
        except Exception as e:
            log.exception("Ошибка ответа Gemini")
            self._status(f"Gemini: {e}", "error")
            return
        if answer_id == self._answer_id:
            self._status(*(("Пауза — звук не обрабатывается", "idle") if self._paused.is_set() else ("Слушаю", "ok")))

    def _build_request(self, question: str | None) -> str:
        lines = self._recent_lines()
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
                self._gemini = GeminiClient(self._cfg["gemini"], self._cfg["prompts"])
            except Exception as e:
                log.error("Gemini: %s", e)
                self._status(f"Gemini: {e}", "error")
                return None
        return self._gemini

    # ---------- история ----------

    def _remember(self, text: str) -> list[str]:
        """Добавляет фразу в историю и возвращает свежий контекст (последняя строка — эта фраза)."""
        with self._history_lock:
            self._history.append((time.monotonic(), text))
        return self._recent_lines()

    def _recent_lines(self) -> list[str]:
        horizon = time.monotonic() - self._cfg["detector"]["context_seconds"]
        with self._history_lock:
            while self._history and self._history[0][0] < horizon:
                self._history.popleft()
            return [text for _, text in self._history]

    def _status(self, text: str, kind: str) -> None:
        self._events.status.emit(text, kind)
