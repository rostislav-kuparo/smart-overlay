"""Быстрый локальный фильтр «похоже на вопрос». Окончательно решает LLM (если включена проверка)."""

import re

RU_INTERROGATIVES = {
    "как", "что", "чем", "чего", "почему", "зачем", "отчего", "когда", "где", "куда", "откуда", "кто", "кого",
    "кому", "какой", "какая", "какое", "какие", "каким", "какую", "каков", "какова", "сколько", "чей", "чья",
    "чьё", "чьи", "разве", "неужели", "можно", "можете", "можешь",
}
RU_REQUESTS = {
    "расскажи", "расскажите", "объясни", "объясните", "опиши", "опишите", "назови", "назовите", "приведи",
    "приведите", "поясни", "поясните", "сравни", "сравните", "перечисли", "перечислите", "напиши", "напишите",
    "реши", "решите", "посчитай", "посчитайте", "подскажи", "подскажите", "скажи", "скажите",
}
EN_WH = {"what", "why", "how", "when", "where", "who", "whom", "whose", "which"}
EN_AUX = {
    "can", "could", "would", "should", "will", "do", "does", "did", "is", "are", "was", "were", "have", "has",
    "tell", "explain", "describe", "walk", "give", "name", "compare", "list",
}

_WORD = re.compile(r"[\w'-]+", re.UNICODE)


def looks_like_question(text: str) -> bool:
    if "?" in text:
        return True
    words = [w.lower() for w in _WORD.findall(text)]
    if not words:
        return False
    head = words[:4]
    return (
        any(w in RU_INTERROGATIVES or w in EN_WH for w in head)
        or words[0] in EN_AUX
        or "ли" in words
        or any(w in RU_REQUESTS for w in words)
    )
