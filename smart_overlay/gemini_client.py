"""Запросы к Gemini: проверка «это вопрос?» и потоковый ответ со скриншотом."""

import os
from collections.abc import Iterator

from google import genai
from google.genai import types
from pydantic import BaseModel


class QuestionCheck(BaseModel):
    is_question: bool
    question: str


def _thinking(level: str) -> types.ThinkingConfig | None:
    return types.ThinkingConfig(thinking_level=level.upper()) if level else None


def resolve_api_key(cfg: dict) -> str:
    return cfg["api_key"] or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def ping(api_key: str, model: str) -> str:
    """Короткий запрос для проверки ключа и названия модели из окна настроек."""
    response = genai.Client(api_key=api_key).models.generate_content(model=model, contents="Ответь одним словом: ок")
    return (response.text or "").strip()


class GeminiClient:
    def __init__(self, cfg: dict, prompts: dict):
        api_key = resolve_api_key(cfg)
        if not api_key:
            raise RuntimeError("нет ключа API — откройте настройки (Ctrl+Alt+S или значок в трее)")
        self._client = genai.Client(api_key=api_key)
        self._check_model = cfg["check_model"]
        self._check_config = types.GenerateContentConfig(
            system_instruction=prompts["check"],
            response_mime_type="application/json",
            response_schema=QuestionCheck,
            thinking_config=_thinking(cfg["check_thinking"]),
        )
        self._answer_model = cfg["answer_model"]
        self._answer_config = types.GenerateContentConfig(
            system_instruction=prompts["answer"],
            thinking_config=_thinking(cfg["answer_thinking"]),
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
