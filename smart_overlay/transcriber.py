"""Распознавание речи faster-whisper (CTranslate2) на видеокарте."""

import logging
import os
import re
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE

log = logging.getLogger(__name__)

# Типичные «галлюцинации» Whisper на шуме и музыке — такие фразы выбрасываем.
HALLUCINATIONS = (
    "субтитры сделал",
    "субтитры создавал",
    "редактор субтитров",
    "продолжение следует",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "thanks for watching",
    "thank you for watching",
    "subtitles by",
    "amara.org",
)
_HAS_LETTERS = re.compile(r"\w", re.UNICODE)


def add_cuda_dll_dirs() -> None:
    """cuBLAS из pip-пакета nvidia-cublas-cu12 лежит в site-packages/nvidia/*/bin — показываем его загрузчику DLL."""
    try:
        import nvidia
    except ImportError:
        return
    for base in nvidia.__path__:
        for bin_dir in Path(base).glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


class Transcriber:
    def __init__(self, model: str, device: str, compute_type: str, language: str, beam_size: int):
        add_cuda_dll_dirs()
        from faster_whisper import WhisperModel

        log.info("Загрузка Whisper %s на %s (%s)…", model, device, compute_type)
        self._model = WhisperModel(model, device=device, compute_type=compute_type)
        self._language = None if language in ("", "auto") else language
        self._beam_size = beam_size
        # Прогрев: первый вызов на GPU заметно медленнее, а заодно сразу видно, что CUDA работает.
        self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=self._beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if not _HAS_LETTERS.search(text) or any(h in text.lower() for h in HALLUCINATIONS):
            return ""
        return text
