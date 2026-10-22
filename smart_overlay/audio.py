"""Захват системного звука (WASAPI loopback) и нарезка потока на фразы с помощью Silero VAD."""

import logging
import queue
import time
from collections import deque
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pyaudiowpatch as pyaudio
import soxr

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME = 512  # окно Silero VAD при 16 кГц — 32 мс
CONTEXT = 64  # Silero v5/v6 перед каждым окном ждёт 64 последних сэмпла предыдущего
FRAME_MS = FRAME * 1000 // SAMPLE_RATE


def list_loopback_devices() -> list[dict]:
    pa = pyaudio.PyAudio()
    try:
        return list(pa.get_loopback_device_info_generator())
    finally:
        pa.terminate()


class LoopbackCapture:
    """Пишет то, что играет на ПК, и отдаёт моно-звук 16 кГц float32."""

    def __init__(self, device_hint: str = ""):
        self._pa = pyaudio.PyAudio()
        self.device = self._find_device(device_hint)
        self._channels = self.device["maxInputChannels"]
        rate = int(self.device["defaultSampleRate"])
        self._resampler = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32")
        self._chunks: queue.Queue[bytes] = queue.Queue()
        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._channels,
            rate=rate,
            input=True,
            input_device_index=self.device["index"],
            frames_per_buffer=rate // 20,
            stream_callback=self._on_audio,
            start=False,
        )
        log.info("Захват звука: %s (%d Гц, %d кан.)", self.device["name"], rate, self._channels)

    def _find_device(self, hint: str) -> dict:
        if not hint:
            return self._pa.get_default_wasapi_loopback()
        for device in self._pa.get_loopback_device_info_generator():
            if hint.lower() in device["name"].lower():
                return device
        raise RuntimeError(
            f"Не найдено устройство, в имени которого есть «{hint}». "
            "Список устройств: python -m smart_overlay --list-devices"
        )

    def _on_audio(self, data, frame_count, time_info, status):
        self._chunks.put(data)
        return None, pyaudio.paContinue

    def start(self) -> None:
        self._stream.start_stream()

    def read(self, timeout: float) -> np.ndarray | None:
        """Следующий кусок звука или None, если за timeout ничего не пришло (на ПК ничего не играет)."""
        try:
            data = self._chunks.get(timeout=timeout)
        except queue.Empty:
            return None
        audio = np.frombuffer(data, dtype=np.float32).reshape(-1, self._channels).mean(axis=1)
        return self._resampler.resample_chunk(audio)

    def close(self) -> None:
        self._stream.stop_stream()
        self._stream.close()
        self._pa.terminate()


class SileroVAD:
    """Потоковая обёртка над Silero VAD v6 — модель уже лежит внутри пакета faster-whisper."""

    def __init__(self):
        import faster_whisper

        path = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, frames: np.ndarray) -> np.ndarray:
        """frames: (n, 512) → вероятность речи для каждого окна, shape (n,)."""
        contexts = np.vstack([self._context, frames[:-1, -CONTEXT:]])
        batch = np.hstack([contexts, frames]).astype(np.float32, copy=False)
        probs, self._h, self._c = self._session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self._context = frames[-1, -CONTEXT:].copy()
        return probs.reshape(-1)


class PhraseSegmenter:
    """Собирает окна с речью во фразы; фраза заканчивается после паузы длиной min_silence_ms."""

    def __init__(
        self,
        vad: SileroVAD,
        threshold: float = 0.5,
        min_silence_ms: int = 700,
        min_speech_ms: int = 400,
        max_phrase_s: float = 25,
        pre_roll_ms: int = 300,
        tail_ms: int = 200,
    ):
        self._vad = vad
        self._on = threshold
        self._off = max(threshold - 0.15, 0.01)
        self._min_silence = max(1, min_silence_ms // FRAME_MS)
        self._min_silence_s = min_silence_ms / 1000
        self._min_speech = max(1, min_speech_ms // FRAME_MS)
        self._max_frames = int(max_phrase_s * 1000 // FRAME_MS)
        self._tail = tail_ms // FRAME_MS
        self._pre_roll: deque[np.ndarray] = deque(maxlen=max(1, pre_roll_ms // FRAME_MS))
        self._pending = np.zeros(0, dtype=np.float32)
        self._last_audio = time.monotonic()
        self._clear_phrase()

    def _clear_phrase(self) -> None:
        self._phrase: list[np.ndarray] = []
        self._speech_frames = 0
        self._silence_run = 0

    def reset(self) -> None:
        self._pre_roll.clear()
        self._pending = np.zeros(0, dtype=np.float32)
        self._clear_phrase()

    def feed(self, audio: np.ndarray) -> list[np.ndarray]:
        """Принимает кусок звука 16 кГц, возвращает законченные фразы (обычно 0 или 1)."""
        self._last_audio = time.monotonic()
        self._pending = np.concatenate([self._pending, audio])
        n = len(self._pending) // FRAME
        if not n:
            return []
        frames = self._pending[: n * FRAME].reshape(n, FRAME)
        self._pending = self._pending[n * FRAME :]
        phrases = []
        for frame, prob in zip(frames, self._vad(frames)):
            phrase = self._step(frame, prob)
            if phrase is not None:
                phrases.append(phrase)
        return phrases

    def flush_if_idle(self) -> np.ndarray | None:
        """WASAPI не присылает данные, пока на ПК тишина, — тогда закрываем фразу по таймеру."""
        if self._phrase and time.monotonic() - self._last_audio >= self._min_silence_s:
            return self._finish(trailing_silence=0)
        return None

    def _step(self, frame: np.ndarray, prob: float) -> np.ndarray | None:
        if not self._phrase:
            self._pre_roll.append(frame)
            if prob >= self._on:
                self._phrase = list(self._pre_roll)
                self._pre_roll.clear()
                self._speech_frames = 1
            return None

        self._phrase.append(frame)
        if prob >= self._off:
            self._speech_frames += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
        if self._silence_run >= self._min_silence or len(self._phrase) >= self._max_frames:
            return self._finish(self._silence_run)
        return None

    def _finish(self, trailing_silence: int) -> np.ndarray | None:
        frames = self._phrase
        cut = trailing_silence - self._tail
        if cut > 0:
            frames = frames[:-cut]
        enough_speech = self._speech_frames >= self._min_speech
        self._clear_phrase()
        return np.concatenate(frames) if enough_speech else None
