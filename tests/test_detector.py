import pytest

from smart_overlay.detector import looks_like_question


@pytest.mark.parametrize("text", [
    "Что выведет эта функция?",
    "расскажите, пожалуйста, чем процесс отличается от потока.",
    "А как вы решали такие задачи",
    "Знаете ли вы Python",
    "Объясните разницу между TCP и UDP.",
    "What is a closure in JavaScript",
    "Can you walk me through your last project",
])
def test_questions(text):
    assert looks_like_question(text)


@pytest.mark.parametrize("text", [
    "Здравствуйте, давайте начнём наше собеседование.",
    "Я сегодня утром выпил кофе и пришёл на работу.",
    "This is a great example.",
    "...",
])
def test_not_questions(text):
    assert not looks_like_question(text)
