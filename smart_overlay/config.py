"""Настройки: схема (из неё строятся значения по умолчанию, WebUI и config.toml), чтение, проверка и запись."""

import copy
import json
import os
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .i18n import INTERFACE_LANGUAGES, T, msg, set_language, tr

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "SmartOverlay"
LOCAL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SmartOverlay"
CONFIG_PATH = APP_DIR / "config.toml"
LOG_PATH = APP_DIR / "smart_overlay.log"
INSTANCE_PATH = APP_DIR / "instance.json"

ANSWER_PROMPT = """\
Ты — ассистент, который в реальном времени помогает пользователю во время разговора, созвона или занятия.
Тебе передают:
- расшифровку недавней речи (распознана автоматически и может содержать ошибки — учитывай похожие по звучанию слова);
- вопрос, на который нужно ответить;
- скриншот экрана пользователя (если приложен).

Правила:
- Отвечай сразу, без вступлений и без повторения вопроса.
- Первая строка — суть ответа в 1–2 предложениях, выделенная жирным.
- Затем, если нужно, 2–5 коротких пунктов с ключевыми деталями.
- Если вопрос касается того, что видно на экране (код, задача, текст, таблица), опирайся на скриншот.
- Если нужен код — дай короткий рабочий фрагмент.
- Отвечай на языке вопроса.
"""

CHECK_PROMPT = """\
Тебе дают автоматическую расшифровку разговора (в ней бывают ошибки распознавания). \
Последняя реплика отмечена «>>>».
Определи, есть ли в ПОСЛЕДНЕЙ реплике вопрос или просьба, на которые слушателю стоит ответить \
(например: «как работает…», «расскажите про…», «объясните…», «сколько будет…», «what is…»).
Не считаются вопросом: риторические вопросы, явно оборванные на середине фразы, вопросы, на которые говорящий \
тут же отвечает сам, реплики из рекламы или фонового видео.
Также НЕ считаются вопросом организационные и технические реплики, на которые не нужен ответ по существу:
— проверка связи: «слышно меня?», «видно экран?», «звук/видео идёт?», «чути мене?», «видно презентацію?», \
«йде звук?», «can you hear me?»;
— перекличка и организация: «все подключились?», «кто отсутствует?», «можно начинать?», «готовы?», \
«есть вопросы?», «всем понятно?»;
— просьбы к группе: «включите микрофон», «напишите в чат плюс», «откройте презентацию».
Отвечай is_question = true только на вопросы по теме занятия или разговора, на которые нужен содержательный ответ.
Если это вопрос — в поле question верни его полную самодостаточную формулировку с учётом контекста, \
на языке оригинала. Иначе is_question = false и пустой question.
"""

LANGUAGES = (
    ("ru", T("Русский", "Російська", "Russian")),
    ("uk", T("Украинский", "Українська", "Ukrainian")),
    ("en", T("Английский", "Англійська", "English")),
    ("de", T("Немецкий", "Німецька", "German")),
    ("es", T("Испанский", "Іспанська", "Spanish")),
    ("fr", T("Французский", "Французька", "French")),
    ("pl", T("Польский", "Польська", "Polish")),
)
# Название языка для инструкции модели (инструкции у нас на русском)
MODEL_LANGUAGE_NAMES = {code: name.ru.lower() for code, name in LANGUAGES}

THINKING_LEVELS = (
    ("", T("По умолчанию модели", "За замовчуванням моделі", "Model default")),
    ("minimal", T("Минимальная — быстрее всего", "Мінімальна — найшвидше", "Minimal — fastest")),
    ("low", T("Низкая", "Низька", "Low")),
    ("medium", T("Средняя", "Середня", "Medium")),
    ("high", T("Высокая — точнее, но медленнее", "Висока — точніше, але повільніше", "High — more accurate, slower")),
)


@dataclass(frozen=True)
class Field:
    key: str
    default: object
    label: T
    help: T | None = None
    # text | secret | int | float | bool | select | textarea | hotkey | audio_device | monitor | hidden
    # | preset_active | preset_list
    kind: str = ""
    options: tuple = ()  # для select — допустимые (значение, подпись); для text — подсказки (значение, "")
    min: float | None = None
    max: float | None = None
    step: float | None = None
    save_default: bool = True  # False — значение по умолчанию не пишется в файл, чтобы получать его обновления
    quick: bool = False  # переключатель на панели «Управление»: применяется сразу, без кнопки «Сохранить»
    restart: bool | None = None  # перезапуск распознавания при изменении; None — как у раздела

    @property
    def type(self) -> str:
        if self.kind:
            return self.kind
        return {bool: "bool", int: "int", float: "float"}.get(type(self.default), "text")


@dataclass(frozen=True)
class Section:
    key: str
    title: T
    fields: tuple[Field, ...]
    restart: bool = False  # изменение перезапускает распознавание (перезагрузка модели, ~10 с)
    description: T | None = None

    def restarts(self, field: Field) -> bool:
        return self.restart if field.restart is None else field.restart


def _hotkey(key: str, default: str, label: T) -> Field:
    return Field(key, default, label, kind="hotkey")


SCHEMA: tuple[Section, ...] = (
    Section("presets", T("Пресеты", "Пресети", "Presets"), (
        Field("active", "", T("Активный пресет", "Активний пресет", "Active preset"), kind="preset_active"),
        Field("items", [], T("Пресеты", "Пресети", "Presets"), kind="preset_list"),
    ), description=T(
        "Контекст задачи, который добавляется к каждому запросу: тема, ваша роль, стек, термины. Модели проще "
        "сориентироваться — ответы точнее и по делу. Переключать пресеты можно в «Управлении», в трее и горячей клавишей.",
        "Контекст завдання, який додається до кожного запиту: тема, ваша роль, стек, терміни. Моделі легше "
        "зорієнтуватися — відповіді точніші й по суті. Перемикати пресети можна в «Керуванні», у треї та гарячою клавішею.",
        "Task context added to every request: topic, your role, tech stack, terms. It helps the model get oriented, "
        "so answers are more precise. Switch presets in Controls, in the tray menu or with a hotkey.",
    )),
    Section("language", T("Язык", "Мова", "Language"), (
        Field("interface", "ru", T("Язык интерфейса", "Мова інтерфейсу", "Interface language"),
              T("Окно настроек, меню в трее и надписи в окне ответа.",
                "Вікно налаштувань, меню в треї та написи у вікні відповіді.",
                "Settings window, tray menu and labels in the answer window."),
              kind="select", options=tuple((code, T(name, name, name)) for code, name in INTERFACE_LANGUAGES)),
        Field("speech", "ru", T("Язык речи", "Мова розмови", "Speech language"),
              T("На каком языке говорят в звуке, который распознаётся. Автоопределение на коротких фразах иногда "
                "ошибается — лучше указать язык явно.",
                "Якою мовою говорять у звуці, що розпізнається. Автовизначення на коротких фразах іноді помиляється — "
                "краще вказати мову явно.",
                "The language spoken in the audio. Auto-detection sometimes fails on short phrases, so it's better "
                "to set it explicitly."),
              kind="select", restart=True,
              options=LANGUAGES + (("auto", T("Определять автоматически", "Визначати автоматично", "Detect automatically")),)),
        Field("answer", "auto", T("Язык ответов", "Мова відповідей", "Answer language"),
              T("На каком языке Gemini пишет ответы.", "Якою мовою Gemini пише відповіді.",
                "The language Gemini answers in."),
              kind="select", options=(("auto", T("Как в вопросе", "Як у питанні", "Same as the question")),) + LANGUAGES),
    )),
    Section("gemini", T("Gemini", "Gemini", "Gemini"), (
        Field("api_key", "", T("Ключ API", "Ключ API", "API key"),
              T("Бесплатно: aistudio.google.com/apikey. Хранится только в файле настроек на этом ПК. Если пусто — "
                "берётся из переменной окружения GEMINI_API_KEY.",
                "Безкоштовно: aistudio.google.com/apikey. Зберігається лише у файлі налаштувань на цьому ПК. Якщо "
                "порожньо — береться зі змінної середовища GEMINI_API_KEY.",
                "Free at aistudio.google.com/apikey. Stored only in the settings file on this PC. If empty, the "
                "GEMINI_API_KEY environment variable is used."),
              kind="secret"),
        Field("answer_model", "gemini-3.8-flash", T("Модель для ответа", "Модель для відповіді", "Answer model"),
              T("Получает вопрос, контекст разговора и скриншот.", "Отримує питання, контекст розмови та скриншот.",
                "Receives the question, conversation context and screenshot."),
              options=(("gemini-3.8-flash", ""), ("gemini-3.6-flash", ""), ("gemini-3.1-pro-preview", ""))),
        Field("answer_thinking", "low",
              T("Глубина размышлений при ответе", "Глибина міркувань під час відповіді", "Answer thinking level"),
              T("Больше — точнее, но медленнее. У gemini-3.8-flash минимальный уровень — «Низкая».",
                "Більше — точніше, але повільніше. У gemini-3.8-flash мінімальний рівень — «Низька».",
                "Higher is more accurate but slower. The lowest level for gemini-3.8-flash is “Low”."),
              kind="select", options=THINKING_LEVELS),
        Field("check_model", "gemini-3.5-flash-lite",
              T("Модель для проверки «это вопрос?»", "Модель для перевірки «це питання?»",
                "Model for the “is it a question?” check"),
              T("Быстрая дешёвая модель, вызывается на каждую фразу, похожую на вопрос.",
                "Швидка дешева модель, викликається на кожну фразу, схожу на питання.",
                "A fast, cheap model called for every phrase that looks like a question."),
              options=(("gemini-3.5-flash-lite", ""), ("gemini-3.1-flash-lite", ""))),
        Field("check_thinking", "minimal",
              T("Глубина размышлений при проверке", "Глибина міркувань під час перевірки", "Check thinking level"),
              kind="select", options=THINKING_LEVELS),
    )),
    Section("audio", T("Звук", "Звук", "Audio"), (
        Field("device", "", T("Устройство вывода", "Пристрій виведення", "Output device"),
              T("Распознаётся то, что играет на этом устройстве. По умолчанию — устройство, выбранное в Windows.",
                "Розпізнається те, що грає на цьому пристрої. За замовчуванням — пристрій, вибраний у Windows.",
                "Audio playing on this device is transcribed. By default, the device selected in Windows."),
              kind="audio_device"),
        Field("vad_threshold", 0.5, T("Чувствительность к речи", "Чутливість до мовлення", "Speech sensitivity"),
              T("Выше — меньше реакции на шум и музыку, но тихая речь может пропускаться.",
                "Вище — менше реакції на шум і музику, але тихе мовлення може пропускатися.",
                "Higher reacts less to noise and music, but quiet speech may be missed."),
              min=0.1, max=0.9, step=0.05),
        Field("min_silence_ms", 700, T("Пауза в конце фразы, мс", "Пауза в кінці фрази, мс", "End-of-phrase pause, ms"),
              T("Меньше — ответ приходит быстрее, но вопрос чаще режется на части.",
                "Менше — відповідь приходить швидше, але питання частіше ріжеться на частини.",
                "Lower answers sooner but splits questions more often."),
              min=300, max=2000, step=50),
        Field("min_speech_ms", 400, T("Минимальная длина фразы, мс", "Мінімальна довжина фрази, мс",
                                      "Minimum phrase length, ms"),
              T("Более короткие звуки игнорируются.", "Коротші звуки ігноруються.", "Shorter sounds are ignored."),
              min=100, max=2000, step=50),
        Field("max_phrase_s", 25, T("Максимальная длина фразы, с", "Максимальна довжина фрази, с",
                                    "Maximum phrase length, s"),
              T("Длинный монолог режется на куски такой длины.", "Довгий монолог ріжеться на шматки такої довжини.",
                "Long monologues are cut into chunks of this length."),
              min=5, max=60, step=1),
    ), restart=True),
    Section("whisper", T("Распознавание речи", "Розпізнавання мовлення", "Speech recognition"), (
        Field("model", "large-v3-turbo", T("Модель Whisper", "Модель Whisper", "Whisper model"),
              T("Скачивается при первом запуске. Модели меньше работают быстрее, но ошибаются чаще.",
                "Завантажується під час першого запуску. Менші моделі працюють швидше, але частіше помиляються.",
                "Downloaded on first run. Smaller models are faster but make more mistakes."),
              kind="select", options=(
                  ("large-v3-turbo", T("large-v3-turbo — лучший баланс (1,6 ГБ)", "large-v3-turbo — найкращий баланс (1,6 ГБ)",
                                       "large-v3-turbo — best balance (1.6 GB)")),
                  ("large-v3", T("large-v3 — точнее, медленнее (3 ГБ)", "large-v3 — точніша, повільніша (3 ГБ)",
                                 "large-v3 — more accurate, slower (3 GB)")),
                  ("medium", T("medium (1,5 ГБ)", "medium (1,5 ГБ)", "medium (1.5 GB)")),
                  ("small", T("small — быстрая (0,5 ГБ)", "small — швидка (0,5 ГБ)", "small — fast (0.5 GB)")),
                  ("base", T("base — очень быстрая, неточная", "base — дуже швидка, неточна", "base — very fast, inaccurate")),
              )),
        Field("device", "cuda", T("Где распознавать", "Де розпізнавати", "Run on"), kind="select", options=(
            ("cuda", T("Видеокарта NVIDIA (CUDA)", "Відеокарта NVIDIA (CUDA)", "NVIDIA GPU (CUDA)")),
            ("cpu", T("Процессор — медленно", "Процесор — повільно", "CPU — slow")),
        )),
        Field("compute_type", "float16", T("Точность вычислений", "Точність обчислень", "Compute precision"),
              T("Для процессора выберите int8.", "Для процесора виберіть int8.", "Choose int8 for the CPU."),
              kind="select", options=(
                  ("float16", T("float16 — для видеокарты", "float16 — для відеокарти", "float16 — for GPU")),
                  ("int8_float16", T("int8_float16 — меньше видеопамяти", "int8_float16 — менше відеопам'яті",
                                     "int8_float16 — less VRAM")),
                  ("int8", T("int8 — для процессора", "int8 — для процесора", "int8 — for CPU")),
              )),
        Field("beam_size", 5, T("Ширина поиска (beam size)", "Ширина пошуку (beam size)", "Beam size"),
              T("1 — чуть быстрее, 5 — чуть точнее.", "1 — трохи швидше, 5 — трохи точніше.",
                "1 is slightly faster, 5 slightly more accurate."),
              min=1, max=10, step=1),
    ), restart=True),
    Section("detector", T("Определение вопросов", "Визначення питань", "Question detection"), (
        Field("auto_answer", True, T("Отвечать автоматически", "Відповідати автоматично", "Answer automatically"),
              T("Если выключить, программа только слушает и копит контекст, а отвечает по команде «Спросить вручную».",
                "Якщо вимкнути, програма лише слухає та накопичує контекст, а відповідає за командою «Запитати вручну».",
                "When off, the app only listens and keeps context; it answers on the “Ask manually” command."),
              quick=True),
        Field("use_llm_check", True, T("Проверять вопрос через Gemini", "Перевіряти питання через Gemini",
                                       "Confirm questions with Gemini"),
              T("Если выключить, ответ приходит на ~0,5 с быстрее, но ложных срабатываний больше.",
                "Якщо вимкнути, відповідь приходить на ~0,5 с швидше, але хибних спрацювань більше.",
                "When off, answers arrive ~0.5 s sooner but with more false triggers."),
              quick=True),
        Field("context_seconds", 120, T("Контекст разговора, с", "Контекст розмови, с", "Conversation context, s"),
              T("Сколько последней речи отправлять вместе с вопросом.", "Скільки останнього мовлення надсилати разом із питанням.",
                "How much recent speech to send with the question."),
              min=15, max=600, step=15),
    )),
    Section("screenshot", T("Скриншот", "Скриншот", "Screenshot"), (
        Field("enabled", True, T("Прикладывать скриншот", "Додавати скриншот", "Attach a screenshot"),
              T("Позволяет спрашивать про то, что на экране: код, задачу, таблицу.",
                "Дає змогу питати про те, що на екрані: код, задачу, таблицю.",
                "Lets you ask about what's on screen: code, a task, a table."),
              quick=True),
        Field("monitor", 1, T("Монитор", "Монітор", "Monitor"), kind="monitor"),
        Field("max_side", 1600, T("Максимальный размер, px", "Максимальний розмір, px", "Maximum size, px"),
              T("Снимок уменьшается до этого размера по длинной стороне.",
                "Знімок зменшується до цього розміру по довшій стороні.",
                "The screenshot is scaled down to this size on its longer side."),
              min=640, max=3840, step=80),
        Field("jpeg_quality", 80, T("Качество JPEG", "Якість JPEG", "JPEG quality"), min=30, max=95, step=5),
    )),
    Section("overlay", T("Строка состояния и вид окон", "Рядок стану та вигляд вікон", "Status window and look"), (
        Field("position", "top-right", T("Положение строки состояния", "Розташування рядка стану",
                                         "Status window position"),
              T("Строку можно просто перетащить мышью — тогда здесь будет «Своё место».",
                "Рядок можна просто перетягнути мишею — тоді тут буде «Своє місце».",
                "You can simply drag the window with the mouse — this then becomes “Custom”."),
              kind="select", options=(
                  ("top-right", T("Справа сверху", "Праворуч угорі", "Top right")),
                  ("top-left", T("Слева сверху", "Ліворуч угорі", "Top left")),
                  ("bottom-right", T("Справа снизу", "Праворуч унизу", "Bottom right")),
                  ("bottom-left", T("Слева снизу", "Ліворуч унизу", "Bottom left")),
                  ("custom", T("Своё место (перетащено мышью)", "Своє місце (перетягнуто мишею)",
                               "Custom (dragged with the mouse)")),
              )),
        Field("x", 0, T("X", "X", "X"), kind="hidden"),
        Field("y", 0, T("Y", "Y", "Y"), kind="hidden"),
        Field("width", 520, T("Ширина строки, px", "Ширина рядка, px", "Width, px"),
              T("Удобнее растянуть строку мышью за левый или правый край.",
                "Зручніше розтягнути рядок мишею за лівий або правий край.",
                "It's easier to stretch the window by its left or right edge."),
              min=280, max=3000, step=10),
        Field("height", 0, T("Высота строки, px", "Висота рядка, px", "Height, px"),
              T("0 — по содержимому.", "0 — за вмістом.", "0 means it fits the content."),
              min=0, max=3000, step=10),
        Field("margin", 24, T("Отступ от края экрана, px", "Відступ від краю екрана, px", "Margin from the screen edge, px"),
              min=0, max=200, step=4),
        Field("font_size", 11, T("Размер шрифта, pt", "Розмір шрифту, pt", "Font size, pt"),
              T("Для строки состояния и окон ответов.", "Для рядка стану та вікон відповідей.",
                "For the status window and answer windows."),
              min=7, max=24, step=1),
        Field("opacity", 0.94, T("Непрозрачность", "Непрозорість", "Opacity"),
              T("Для строки состояния и окон ответов.", "Для рядка стану та вікон відповідей.",
                "For the status window and answer windows."),
              min=0.3, max=1.0, step=0.02),
        Field("click_through", False, T("Пропускать клики сквозь строку состояния", "Пропускати кліки крізь рядок стану",
                                        "Click-through status window"),
              T("Выключено — строку можно таскать мышью и растягивать за края. Включено — мышь проходит сквозь неё; "
                "двигать её тогда можно в режиме «Двигать и растягивать». Окна ответов всегда можно двигать и закрыть.",
                "Вимкнено — рядок можна перетягувати мишею й розтягувати за краї. Увімкнено — миша проходить крізь "
                "нього; рухати його тоді можна в режимі «Рухати й розтягувати». Вікна відповідей завжди можна рухати "
                "й закрити.",
                "Off: drag the status window and stretch it by its edges. On: the mouse passes through it; use "
                "“Move and resize” mode to reposition it. Answer windows can always be moved and closed."),
              quick=True),
        Field("show_transcript", True, T("Показывать последнюю распознанную фразу", "Показувати останню розпізнану фразу",
                                         "Show the last transcribed phrase"), quick=True),
        Field("exclude_from_capture", True, T("Скрывать окна от захвата экрана", "Приховувати вікна від захоплення екрана",
                                              "Hide windows from screen capture"),
              T("Окна программы не попадают на скриншоты, поэтому Gemini не видит свои прошлые ответы. Побочный эффект: "
                "их не видно и при записи или демонстрации экрана.",
                "Вікна програми не потрапляють на скриншоти, тому Gemini не бачить своїх попередніх відповідей. "
                "Побічний ефект: їх не видно й під час запису чи демонстрації екрана.",
                "The app's windows are left out of screenshots, so Gemini doesn't see its own previous answers. "
                "Side effect: they are also invisible in screen recordings and screen sharing.")),
    )),
    Section("answers", T("Окна ответов", "Вікна відповідей", "Answer windows"), (
        Field("position", "below", T("Где открывать новое окно", "Де відкривати нове вікно", "Where new windows open"),
              T("Окна открываются лесенкой, если открыто несколько. Если перетащить окно ответа мышью, новые будут "
                "открываться там же.",
                "Вікна відкриваються драбинкою, якщо відкрито кілька. Якщо перетягнути вікно відповіді мишею, нові "
                "відкриватимуться там само.",
                "Several windows open in a cascade. Drag an answer window and new ones will open in the same place."),
              kind="select", options=(
                  ("below", T("Под строкой состояния", "Під рядком стану", "Below the status window")),
                  ("custom", T("Там, куда перетащено последнее окно", "Там, куди перетягнуто останнє вікно",
                               "Where the last window was dragged")),
              )),
        Field("x", 0, T("X", "X", "X"), kind="hidden"),
        Field("y", 0, T("Y", "Y", "Y"), kind="hidden"),
        Field("width", 0, T("Ширина нового окна, px", "Ширина нового вікна, px", "New window width, px"),
              T("0 — как у строки состояния. Проще растянуть любое окно ответа мышью — новые откроются такими же.",
                "0 — як у рядка стану. Простіше розтягнути будь-яке вікно відповіді мишею — нові відкриються такими ж.",
                "0 means the status window width. Easier: stretch any answer window and new ones open the same size."),
              min=0, max=3000, step=10),
        Field("height", 0, T("Высота нового окна, px", "Висота нового вікна, px", "New window height, px"),
              T("0 — окно растёт вместе с ответом, до максимальной высоты.",
                "0 — вікно росте разом із відповіддю, до максимальної висоти.",
                "0 means the window grows with the answer, up to the maximum height."),
              min=0, max=3000, step=10),
        Field("max_height_ratio", 0.6, T("Максимальная высота, доля экрана", "Максимальна висота, частка екрана",
                                         "Maximum height, share of the screen"),
              T("Когда высота подстраивается под ответ.", "Коли висота підлаштовується під відповідь.",
                "When the height fits the answer."),
              min=0.2, max=0.95, step=0.05),
    ), description=T(
        "Каждый ответ открывается в своём окне и висит, пока вы его не закроете (✕). Закрыть последнее — горячей "
        "клавишей, все сразу — в трее.",
        "Кожна відповідь відкривається у своєму вікні й висить, доки ви його не закриєте (✕). Закрити останнє — "
        "гарячою клавішею, усі одразу — у треї.",
        "Each answer opens in its own window and stays until you close it (✕). Close the latest with a hotkey, "
        "all of them from the tray.",
    )),
    Section("history", T("История", "Історія", "History"), (
        Field("enabled", True, T("Вести историю", "Вести історію", "Keep history"),
              T("Расшифровка, найденные вопросы и ответы сохраняются только на этом ПК.",
                "Розшифровка, знайдені питання й відповіді зберігаються лише на цьому ПК.",
                "The transcript, detected questions and answers are stored on this PC only.")),
        Field("save_screenshots", True, T("Сохранять скриншоты", "Зберігати скриншоти", "Save screenshots"),
              T("Скриншот, отправленный вместе с вопросом, — чтобы потом видеть, что было на экране.",
                "Скриншот, надісланий разом із питанням, — щоб потім бачити, що було на екрані.",
                "The screenshot sent with a question, so you can later see what was on screen.")),
        Field("keep_days", 30, T("Хранить, дней", "Зберігати, днів", "Keep for, days"),
              T("Более старые записи и скриншоты удаляются автоматически.",
                "Старіші записи та скриншоти видаляються автоматично.",
                "Older entries and screenshots are deleted automatically."),
              min=1, max=365, step=1),
    ), description=T(
        "Всё, что заметил алгоритм: каждая фраза, каждое срабатывание детектора и его итог — ответ, «не вопрос», "
        "повтор или ошибка. Открыть — кнопкой «История» вверху или в трее.",
        "Усе, що помітив алгоритм: кожна фраза, кожне спрацювання детектора та його підсумок — відповідь, "
        "«не питання», повтор чи помилка. Відкрити — кнопкою «Історія» вгорі або в треї.",
        "Everything the algorithm noticed: every phrase, every detector trigger and its outcome — answer, "
        "“not a question”, repeat or error. Open it with the History button at the top or from the tray.",
    )),
    Section("hotkeys", T("Горячие клавиши", "Гарячі клавіші", "Hotkeys"), (
        _hotkey("toggle", "ctrl+alt+h", T("Показать / скрыть строку состояния", "Показати / сховати рядок стану",
                                          "Show / hide the status window")),
        _hotkey("ask", "ctrl+alt+a", T("Спросить вручную (скриншот + последние реплики)",
                                       "Запитати вручну (скриншот + останні репліки)",
                                       "Ask manually (screenshot + recent speech)")),
        _hotkey("clear", "ctrl+alt+x", T("Закрыть последнее окно ответа", "Закрити останнє вікно відповіді",
                                         "Close the latest answer window")),
        _hotkey("pause", "ctrl+alt+p", T("Пауза / продолжить", "Пауза / продовжити", "Pause / resume")),
        _hotkey("move", "ctrl+alt+m", T("Двигать и растягивать окно (вкл / выкл)", "Рухати й розтягувати вікно (увімк / вимк)",
                                        "Move and resize the window (on / off)")),
        _hotkey("next_preset", "ctrl+alt+n", T("Следующий пресет", "Наступний пресет", "Next preset")),
        _hotkey("scroll_up", "ctrl+alt+up", T("Прокрутить последний ответ вверх", "Прокрутити останню відповідь угору",
                                              "Scroll the latest answer up")),
        _hotkey("scroll_down", "ctrl+alt+down", T("Прокрутить последний ответ вниз", "Прокрутити останню відповідь униз",
                                                  "Scroll the latest answer down")),
        _hotkey("settings", "ctrl+alt+s", T("Открыть настройки", "Відкрити налаштування", "Open settings")),
        _hotkey("quit", "ctrl+alt+q", T("Выход", "Вихід", "Quit")),
    ), description=T(
        "Нажмите на поле и затем сочетание клавиш. Backspace — отключить, Esc — закончить запись.",
        "Натисніть на поле, а потім сполучення клавіш. Backspace — вимкнути, Esc — завершити запис.",
        "Click a field, then press the key combination. Backspace disables it, Esc stops recording.",
    )),
    Section("prompts", T("Инструкции для модели", "Інструкції для моделі", "Model instructions"), (
        Field("answer", ANSWER_PROMPT, T("Как отвечать", "Як відповідати", "How to answer"), kind="textarea",
              save_default=False),
        Field("check", CHECK_PROMPT, T("Как определять вопрос", "Як визначати питання", "How to detect a question"),
              kind="textarea", save_default=False),
    )),
)

DEFAULTS = {s.key: {f.key: copy.deepcopy(f.default) for f in s.fields} for s in SCHEMA}


def active_preset(cfg: dict) -> dict | None:
    name = cfg["presets"]["active"]
    return next((p for p in cfg["presets"]["items"] if p["name"] == name), None) if name else None


# ---------- чтение и запись ----------

def _merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _migrate(data: dict) -> None:
    """Переносит ключи из старых версий config.toml."""
    old_speech = data.get("whisper", {}).pop("language", None)
    if old_speech is not None:
        data.setdefault("language", {}).setdefault("speech", old_speech)
    # Ответы переехали в отдельные окна: высоту старого общего окна (её растягивали ради ответов)
    # и его максимальную высоту получают окна ответов, а строка состояния снова подстраивается под текст.
    overlay = data.get("overlay", {})
    if "answers" not in data and "max_height_ratio" in overlay:
        data["answers"] = {"height": overlay.pop("height", 0), "max_height_ratio": overlay.pop("max_height_ratio")}


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
        _migrate(data)
        _merge(cfg, data)
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if "\n" in text:
        return '"""\n' + text.replace("\\", "\\\\").replace('"', '\\"') + '"""'
    return json.dumps(text, ensure_ascii=False)  # строка JSON — допустимая базовая строка TOML


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    lines = [f"# {msg('config.header')}", ""]
    for section in SCHEMA:
        lines.append(f"[{section.key}]")
        tables = []
        for field in section.fields:
            value = cfg[section.key][field.key]
            if field.type == "preset_list":
                tables.append((field, value))  # массив таблиц пишется после простых ключей раздела
                continue
            lines.append(f"# {tr(field.label)}" + (f". {tr(field.help)}" if field.help else ""))
            if not field.save_default and value == field.default:
                lines.append(f"# {field.key} = …")
            else:
                lines.append(f"{field.key} = {_toml_value(value)}")
        lines.append("")
        for field, items in tables:
            for item in items:
                lines += [f"[[{section.key}.{field.key}]]", f"name = {_toml_value(item['name'])}",
                          f"context = {_toml_value(item['context'])}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


class ConfigStore:
    """Текущие настройки, общие для всех потоков.

    Словарь из get() никогда не меняется на месте: update() собирает новый, сохраняет его в файл и подменяет
    целиком. Поэтому читать можно из любого потока, а изменения из трея и из окна настроек не затирают друг друга.
    """

    def __init__(self, path: Path = CONFIG_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._cfg = load_config(path)
        self.version = 0
        set_language(self._cfg["language"]["interface"])
        if not path.exists():
            save_config(self._cfg, path)

    def get(self) -> dict:
        return self._cfg

    def update(self, change: Callable[[dict], dict | None]) -> tuple[dict, dict]:
        """change получает копию настроек и меняет её на месте (или возвращает новую). Возвращает (было, стало)."""
        with self._lock:
            old = self._cfg
            new = copy.deepcopy(old)
            new = change(new) or new
            set_language(new["language"]["interface"])
            save_config(new, self._path)
            self._cfg = new
            self.version += 1
            return old, new


# ---------- проверка значений из окна настроек ----------

def validate(values: dict, current: dict) -> tuple[dict, dict[str, str]]:
    """Накладывает изменённые в WebUI поля на current. Возвращает (новый конфиг, ошибки {"section.key": текст})."""
    cfg = copy.deepcopy(current)
    errors: dict[str, str] = {}
    for section in SCHEMA:
        incoming = values.get(section.key, {})
        for field in section.fields:
            if field.key in incoming and not field.type.startswith("preset_"):
                try:
                    cfg[section.key][field.key] = _coerce(field, incoming[field.key], current[section.key][field.key])
                except ValueError as e:
                    errors[f"{section.key}.{field.key}"] = str(e)
    _validate_presets(values.get("presets", {}), current, cfg, errors)
    _validate_hotkeys(values.get("hotkeys", {}), cfg, errors)
    return cfg, errors


def _coerce(field: Field, raw, current):
    kind = field.type
    if kind == "bool":
        return bool(raw)
    if kind in ("int", "float", "monitor", "hidden"):
        try:
            number = float(raw) if kind == "float" else int(raw)
        except (TypeError, ValueError):
            raise ValueError(msg("error.number")) from None
        if field.min is not None and number < field.min or field.max is not None and number > field.max:
            raise ValueError(msg("error.range", min=f"{field.min:g}", max=f"{field.max:g}"))
        return number
    if kind == "secret" and raw is None:
        return current  # поле не трогали — ключ остаётся прежним
    text = "" if raw is None else str(raw)
    if kind == "select" and text not in {value for value, _ in field.options}:
        raise ValueError(msg("error.invalid"))
    if kind in ("hotkey", "text", "secret", "audio_device"):
        text = text.strip()
    if kind == "hotkey":
        text = text.lower().replace(" ", "")
    return text


def _validate_presets(incoming: dict, current: dict, cfg: dict, errors: dict[str, str]) -> None:
    if "items" in incoming:
        raw = incoming["items"]
        if not isinstance(raw, list):
            errors["presets.items"] = msg("error.invalid")
            return
        items, seen = [], set()
        for index, item in enumerate(raw):
            item = item if isinstance(item, dict) else {}
            name = str(item.get("name") or "").strip()[:60]
            context = str(item.get("context") or "").strip()
            if not name:
                errors[f"presets.items.{index}.name"] = msg("error.preset_name_empty")
            elif name.casefold() in seen:
                errors[f"presets.items.{index}.name"] = msg("error.preset_name_taken", name=name)
            seen.add(name.casefold())
            items.append({"name": name, "context": context})
        cfg["presets"]["items"] = items
        # Активный пресет переименовали — следуем за ним по позиции в списке.
        names = [p["name"] for p in items]
        active = current["presets"]["active"]
        if active and active not in names:
            old_names = [p["name"] for p in current["presets"]["items"]]
            index = old_names.index(active) if active in old_names else len(names)
            cfg["presets"]["active"] = names[index] if index < len(names) else ""
    if "active" in incoming:
        active = str(incoming["active"] or "")
        if active and active not in [p["name"] for p in cfg["presets"]["items"]]:
            errors["presets.active"] = msg("error.no_preset", name=active)
        else:
            cfg["presets"]["active"] = active


def _validate_hotkeys(changed: dict, cfg: dict, errors: dict[str, str]) -> None:
    """Сочетания проверяем по итоговому набору: конфликт возможен и с клавишей, которую сейчас не меняли."""
    from .hotkeys import parse_hotkey

    section = next(s for s in SCHEMA if s.key == "hotkeys")
    seen: dict[tuple[int, int], Field] = {}
    for field in section.fields:
        name = f"hotkeys.{field.key}"
        if name in errors or not cfg["hotkeys"][field.key]:
            continue
        try:
            combo = parse_hotkey(cfg["hotkeys"][field.key])
        except ValueError as e:
            errors[name] = str(e)
            continue
        if combo in seen:
            other = seen[combo]
            blamed, owner = (other, field) if other.key in changed and field.key not in changed else (field, other)
            errors[f"hotkeys.{blamed.key}"] = msg("error.hotkey_taken", other=tr(owner.label))
        seen[combo] = field


# ---------- для окна настроек ----------

def schema_json() -> list[dict]:
    return [
        {
            "key": s.key,
            "title": tr(s.title),
            "description": tr(s.description) if s.description else "",
            "restart": s.restart,
            "fields": [
                {
                    "key": f.key, "label": tr(f.label), "help": tr(f.help) if f.help else "", "type": f.type,
                    "default": f.default, "options": [[value, tr(label) or value] for value, label in f.options],
                    "min": f.min, "max": f.max, "step": f.step, "quick": f.quick, "restart": s.restarts(f),
                }
                for f in s.fields
            ],
        }
        for s in SCHEMA
    ]


def restart_needed(old: dict, new: dict) -> bool:
    return any(
        old[s.key][f.key] != new[s.key][f.key] for s in SCHEMA for f in s.fields if s.restarts(f)
    )
