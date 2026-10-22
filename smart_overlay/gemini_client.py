"""Запросы к Gemini: проверка «это вопрос?» и потоковый ответ со скриншотом."""

import os
from collections.abc import Iterator

from google import genai
from google.genai import types
from pydantic import BaseModel

from .config import MODEL_LANGUAGE_NAMES, active_preset
from .i18n import msg


class QuestionCheck(BaseModel):
    is_question: bool
    question: str


def _thinking(level: str) -> types.ThinkingConfig | None:
    return types.ThinkingConfig(thinking_level=level.upper()) if level else None


def resolve_api_key(gemini_cfg: dict) -> str:
    return gemini_cfg["api_key"] or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def ping(api_key: str, model: str) -> str:
    """Короткий запрос для проверки ключа и названия модели из окна настроек."""
    response = genai.Client(api_key=api_key).models.generate_content(model=model, contents="Ответь одним словом: ок")
    return (response.text or "").strip()


def build_instructions(cfg: dict) -> tuple[str, str]:
    """Системные инструкции (ответ, проверка) с учётом активного пресета и языка ответа."""
    context = ""
    preset = active_preset(cfg)
    if preset and preset["context"]:
        context = (
            f"\n\nКонтекст задачи от пользователя (пресет «{preset['name']}»). "
            f"Учитывай его, чтобы быстрее понять, о чём идёт речь:\n{preset['context']}"
        )
    answer = cfg["prompts"]["answer"] + context
    language = cfg["language"]["answer"]
    if language in MODEL_LANGUAGE_NAMES:
        answer += (
            f"\n\nВАЖНО: всегда отвечай на языке «{MODEL_LANGUAGE_NAMES[language]}» — "
            "независимо от языка вопроса и расшифровки."
        )
    return answer, cfg["prompts"]["check"] + context


class GeminiClient:
    def __init__(self, cfg: dict):
        gemini = cfg["gemini"]
        api_key = resolve_api_key(gemini)
        if not api_key:
            raise RuntimeError(msg("error.no_key"))
        self._client = genai.Client(api_key=api_key)
        answer_instruction, check_instruction = build_instructions(cfg)
        self._check_model = gemini["check_model"]
        self._check_config = types.GenerateContentConfig(
            system_instruction=check_instruction,
            response_mime_type="application/json",
            response_schema=QuestionCheck,
            thinking_config=_thinking(gemini["check_thinking"]),
        )
        self._answer_model = gemini["answer_model"]
        self._answer_config = types.GenerateContentConfig(
            system_instruction=answer_instruction,
            thinking_config=_thinking(gemini["answer_thinking"]),
        )

    def check_question(self, transcript: str) -> str | None:
        """Возвращает полную формулировку вопроса или None, если последняя реплика — не вопрос."""
        response = self._client.models.generate_content(
            model=self._check_model, contents=transcript, config=self._check_config
        )
        result = response.parsed
        if not isinstance(result, QuestionCheck) or not result.is_question:
            return None
        return result.question.strip() or None

    def answer(self, request: str, screenshot_jpeg: bytes | None) -> Iterator[str]:
        contents: list = [request]
        if screenshot_jpeg:
            contents.insert(0, types.Part.from_bytes(data=screenshot_jpeg, mime_type="image/jpeg"))
        for chunk in self._client.models.generate_content_stream(
            model=self._answer_model, contents=contents, config=self._answer_config
        ):
            if chunk.text:
                yield chunk.text
