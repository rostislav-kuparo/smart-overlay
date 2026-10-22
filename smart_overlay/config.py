"""Настройки: схема (из неё строятся значения по умолчанию, WebUI и config.toml), чтение, проверка и запись."""

import copy
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

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
Если это вопрос — в поле question верни его полную самодостаточную формулировку с учётом контекста, \
на языке оригинала. Иначе is_question = false и пустой question.
"""

THINKING_LEVELS = (
    ("", "По умолчанию модели"),
    ("minimal", "Минимальная — быстрее всего"),
    ("low", "Низкая"),
    ("medium", "Средняя"),
    ("high", "Высокая — точнее, но медленнее"),
)


@dataclass(frozen=True)
class Field:
    key: str
    default: object
    label: str
    help: str = ""
    # text | secret | int | float | bool | select | textarea | hotkey | audio_device | monitor
    kind: str = ""
    options: tuple = ()  # для select — допустимые (значение, подпись); для text — подсказки
    min: float | None = None
    max: float | None = None
    step: float | None = None
    save_default: bool = True  # False — значение по умолчанию не пишется в файл, чтобы получать его обновления

    @property
    def type(self) -> str:
        if self.kind:
            return self.kind
        return {bool: "bool", int: "int", float: "float"}.get(type(self.default), "text")


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    fields: tuple[Field, ...]
    restart: bool = False  # изменение перезапускает распознавание (перезагрузка модели, ~10 с)
    description: str = ""


def _hotkey(key: str, default: str, label: str) -> Field:
    return Field(key, default, label, kind="hotkey")


SCHEMA: tuple[Section, ...] = (
    Section("gemini", "Gemini", (
        Field("api_key", "", "Ключ API", "Бесплатно: aistudio.google.com/apikey. Хранится только в файле настроек "
              "на этом ПК. Если пусто — берётся из переменной окружения GEMINI_API_KEY.", kind="secret"),
        Field("answer_model", "gemini-3.8-flash", "Модель для ответа",
              "Получает вопрос, контекст разговора и скриншот.",
              options=(("gemini-3.8-flash", ""), ("gemini-3.6-flash", ""), ("gemini-3.1-pro-preview", ""))),
        Field("answer_thinking", "low", "Глубина размышлений при ответе",
              "Больше — точнее, но медленнее. У gemini-3.8-flash минимальный уровень — «Низкая».",
              kind="select", options=THINKING_LEVELS),
        Field("check_model", "gemini-3.5-flash-lite", "Модель для проверки «это вопрос?»",
              "Быстрая дешёвая модель, вызывается на каждую фразу, похожую на вопрос.",
              options=(("gemini-3.5-flash-lite", ""), ("gemini-3.1-flash-lite", ""))),
        Field("check_thinking", "minimal", "Глубина размышлений при проверке", kind="select", options=THINKING_LEVELS),
    )),
    Section("audio", "Звук", (
        Field("device", "", "Устройство вывода",
              "Распознаётся то, что играет на этом устройстве. По умолчанию — устройство, выбранное в Windows.",
              kind="audio_device"),
        Field("vad_threshold", 0.5, "Чувствительность к речи",
              "Выше — меньше реакции на шум и музыку, но тихая речь может пропускаться.", min=0.1, max=0.9, step=0.05),
        Field("min_silence_ms", 700, "Пауза в конце фразы, мс",
              "Меньше — ответ приходит быстрее, но вопрос чаще режется на части.", min=300, max=2000, step=50),
        Field("min_speech_ms", 400, "Минимальная длина фразы, мс", "Более короткие звуки игнорируются.",
              min=100, max=2000, step=50),
        Field("max_phrase_s", 25, "Максимальная длина фразы, с", "Длинный монолог режется на куски такой длины.",
              min=5, max=60, step=1),
    ), restart=True),
    Section("whisper", "Распознавание речи", (
        Field("model", "large-v3-turbo", "Модель Whisper",
              "Скачивается при первом запуске. Модели меньше работают быстрее, но ошибаются чаще.",
              kind="select", options=(
                  ("large-v3-turbo", "large-v3-turbo — лучший баланс (1,6 ГБ)"),
                  ("large-v3", "large-v3 — точнее, медленнее (3 ГБ)"),
                  ("medium", "medium (1,5 ГБ)"),
                  ("small", "small — быстрая (0,5 ГБ)"),
                  ("base", "base — очень быстрая, неточная"),
              )),
        Field("device", "cuda", "Где распознавать", kind="select",
              options=(("cuda", "Видеокарта NVIDIA (CUDA)"), ("cpu", "Процессор — медленно"))),
        Field("compute_type", "float16", "Точность вычислений", "Для процессора выберите int8.", kind="select",
              options=(("float16", "float16 — для видеокарты"), ("int8_float16", "int8_float16 — меньше видеопамяти"),
                       ("int8", "int8 — для процессора"))),
        Field("language", "ru", "Язык речи",
              "Автоопределение на коротких фразах иногда ошибается — лучше указать язык явно.", kind="select",
              options=(("ru", "Русский"), ("en", "Английский"), ("uk", "Украинский"), ("de", "Немецкий"),
                       ("es", "Испанский"), ("fr", "Французский"), ("auto", "Определять автоматически"))),
        Field("beam_size", 5, "Ширина поиска (beam size)", "1 — чуть быстрее, 5 — чуть точнее.", min=1, max=10, step=1),
    ), restart=True),
    Section("detector", "Определение вопросов", (
        Field("use_llm_check", True, "Проверять вопрос через Gemini",
              "Если выключить, программа отвечает на всё, что похоже на вопрос: быстрее на ~0,5 с, "
              "но ложных срабатываний больше."),
        Field("context_seconds", 120, "Контекст разговора, с",
              "Сколько последней речи отправлять вместе с вопросом.", min=15, max=600, step=15),
    )),
    Section("screenshot", "Скриншот", (
        Field("enabled", True, "Прикладывать скриншот к вопросу",
              "Позволяет спрашивать про то, что на экране: код, задачу, таблицу."),
        Field("monitor", 1, "Монитор", kind="monitor"),
        Field("max_side", 1600, "Максимальный размер, px", "Снимок уменьшается до этого размера по длинной стороне.",
              min=640, max=3840, step=80),
        Field("jpeg_quality", 80, "Качество JPEG", min=30, max=95, step=5),
    )),
    Section("overlay", "Окно ответа", (
        Field("position", "top-right", "Положение", kind="select", options=(
            ("top-right", "Справа сверху"), ("top-left", "Слева сверху"),
            ("bottom-right", "Справа снизу"), ("bottom-left", "Слева снизу"),
        )),
        Field("width", 520, "Ширина, px", min=300, max=1200, step=10),
        Field("max_height_ratio", 0.6, "Максимальная высота, доля экрана", min=0.2, max=0.95, step=0.05),
        Field("margin", 24, "Отступ от края экрана, px", min=0, max=200, step=4),
        Field("font_size", 11, "Размер шрифта, pt", min=7, max=24, step=1),
        Field("opacity", 0.94, "Непрозрачность", min=0.3, max=1.0, step=0.02),
        Field("show_transcript", True, "Показывать последнюю распознанную фразу"),
        Field("exclude_from_capture", True, "Скрывать окно от захвата экрана",
              "Окно не попадает на скриншоты, поэтому Gemini не видит свои прошлые ответы. "
              "Побочный эффект: окно не видно и при записи или демонстрации экрана."),
    )),
    Section("hotkeys", "Горячие клавиши", (
        _hotkey("toggle", "ctrl+alt+h", "Показать / скрыть окно"),
        _hotkey("ask", "ctrl+alt+a", "Спросить вручную (скриншот + последние реплики)"),
        _hotkey("clear", "ctrl+alt+x", "Убрать текущий ответ"),
        _hotkey("pause", "ctrl+alt+p", "Пауза / продолжить"),
        _hotkey("scroll_up", "ctrl+alt+up", "Прокрутить ответ вверх"),
        _hotkey("scroll_down", "ctrl+alt+down", "Прокрутить ответ вниз"),
        _hotkey("settings", "ctrl+alt+s", "Открыть настройки"),
        _hotkey("quit", "ctrl+alt+q", "Выход"),
    ), description="Нажмите на поле и затем сочетание клавиш. Backspace — отключить, Esc — закончить запись."),
    Section("prompts", "Инструкции для модели", (
        Field("answer", ANSWER_PROMPT, "Как отвечать", kind="textarea", save_default=False),
        Field("check", CHECK_PROMPT, "Как определять вопрос", kind="textarea", save_default=False),
    )),
)

DEFAULTS = {s.key: {f.key: f.default for f in s.fields} for s in SCHEMA}


def _merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        with path.open("rb") as f:
            _merge(cfg, tomllib.load(f))
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
    lines = ["# Smart Overlay — настройки. Удобнее менять через окно настроек (значок в трее → «Настройки»).", ""]
    for section in SCHEMA:
        lines.append(f"[{section.key}]")
        for field in section.fields:
            value = cfg[section.key][field.key]
            lines.append(f"# {field.label}" + (f". {field.help}" if field.help else ""))
            if not field.save_default and value == field.default:
                lines.append(f"# {field.key} = (по умолчанию)")
            else:
                lines.append(f"{field.key} = {_toml_value(value)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def validate(values: dict, current: dict) -> tuple[dict, dict[str, str]]:
    """Проверяет значения из WebUI. Возвращает (новый конфиг, ошибки вида {"section.key": "текст"})."""
    from .hotkeys import parse_hotkey

    cfg = copy.deepcopy(current)
    errors: dict[str, str] = {}
    for section in SCHEMA:
        incoming = values.get(section.key, {})
        for field in section.fields:
            if field.key in incoming:
                try:
                    cfg[section.key][field.key] = _coerce(field, incoming[field.key], current[section.key][field.key])
                except ValueError as e:
                    errors[f"{section.key}.{field.key}"] = str(e)

    # Сочетания проверяем по итоговому набору: конфликт возможен и с клавишей, которую сейчас не меняли.
    hotkeys = next(s for s in SCHEMA if s.key == "hotkeys")
    changed = values.get("hotkeys", {})
    seen: dict[tuple[int, int], Field] = {}
    for field in hotkeys.fields:
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
            errors[f"hotkeys.{blamed.key}"] = f"то же сочетание, что у «{owner.label}»"
        seen[combo] = field
    return cfg, errors


def _coerce(field: Field, raw, current):
    kind = field.type
    if kind == "bool":
        return bool(raw)
    if kind in ("int", "float", "monitor"):
        try:
            number = int(raw) if kind != "float" else float(raw)
        except (TypeError, ValueError):
            raise ValueError("нужно число") from None
        if field.min is not None and number < field.min or field.max is not None and number > field.max:
            raise ValueError(f"допустимо от {field.min:g} до {field.max:g}")
        return number
    if kind == "secret" and raw is None:
        return current  # поле не трогали — ключ остаётся прежним
    text = "" if raw is None else str(raw)
    if kind == "select" and text not in {value for value, _ in field.options}:
        raise ValueError("недопустимое значение")
    if kind in ("hotkey", "text", "secret", "audio_device"):
        text = text.strip()
    if kind == "hotkey":
        text = text.lower().replace(" ", "")
    return text


def schema_json() -> list[dict]:
    return [
        {
            "key": s.key, "title": s.title, "restart": s.restart, "description": s.description,
            "fields": [
                {
                    "key": f.key, "label": f.label, "help": f.help, "type": f.type, "default": f.default,
                    "options": [list(o) for o in f.options], "min": f.min, "max": f.max, "step": f.step,
                }
                for f in s.fields
            ],
        }
        for s in SCHEMA
    ]


def restart_needed(old: dict, new: dict) -> bool:
    return any(old[s.key] != new[s.key] for s in SCHEMA if s.restart)
