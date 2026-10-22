import copy
import string

import pytest

from smart_overlay.config import (
    DEFAULTS, SCHEMA, ConfigStore, active_preset, load_config, restart_needed, save_config, validate,
)
from smart_overlay.gemini_client import build_instructions
from smart_overlay.i18n import MESSAGES, T, set_language


@pytest.fixture(autouse=True)
def russian():
    set_language("ru")
    yield
    set_language("ru")


def test_defaults_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    save_config(DEFAULTS, path)
    assert load_config(path) == DEFAULTS
    assert "# answer = …" in path.read_text(encoding="utf-8")  # стандартный промпт не сохраняется


def test_tricky_strings_and_presets_round_trip(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["prompts"]["answer"] = 'Кавычки " и """ тройные,\nобратный слэш \\ и C:\\path\\n, конец "'
    cfg["gemini"]["api_key"] = 'ab"c\\d'
    cfg["presets"]["items"] = [
        {"name": "Созвон", "context": 'Проект "X"\nстек: Python'},
        {"name": "Лекція", "context": ""},
    ]
    cfg["presets"]["active"] = "Созвон"
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    assert load_config(path) == cfg


def test_old_whisper_language_is_migrated(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[whisper]\nlanguage = "uk"\nbeam_size = 2\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg["language"]["speech"] == "uk"
    assert "language" not in cfg["whisper"]
    assert cfg["whisper"]["beam_size"] == 2


def test_store_update_saves_and_switches_language(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")
    old, new = store.update(lambda cfg: cfg["language"].__setitem__("interface", "uk"))
    assert old["language"]["interface"] == "ru" and new["language"]["interface"] == "uk"
    assert store.version == 1
    assert "Мова інтерфейсу" in (tmp_path / "config.toml").read_text(encoding="utf-8")  # комментарии на новом языке
    assert load_config(tmp_path / "config.toml") == new


def test_validate_keeps_secret_when_untouched():
    current = copy.deepcopy(DEFAULTS)
    current["gemini"]["api_key"] = "secret"
    cfg, errors = validate({"gemini": {"api_key": None, "answer_model": " gemini-x "}}, current)
    assert not errors
    assert cfg["gemini"]["api_key"] == "secret"
    assert cfg["gemini"]["answer_model"] == "gemini-x"


def test_validate_errors():
    values = {
        "audio": {"min_silence_ms": 10},
        "language": {"speech": "klingon"},
        "overlay": {"opacity": "abc"},
        "hotkeys": {"toggle": "ctrl+alt+a", "ask": "ctrl+alt+a", "clear": "ctrl+nope"},
    }
    _, errors = validate(values, DEFAULTS)
    assert set(errors) == {
        "audio.min_silence_ms", "language.speech", "overlay.opacity", "hotkeys.ask", "hotkeys.clear",
    }


def test_hotkey_conflict_with_unchanged_key_blames_changed_one():
    _, errors = validate({"hotkeys": {"toggle": "ctrl+alt+a"}}, DEFAULTS)  # ask = ctrl+alt+a по умолчанию
    assert set(errors) == {"hotkeys.toggle"}


def test_presets_validation():
    items = [{"name": " Созвон ", "context": " текст "}, {"name": "созвон", "context": ""}, {"name": "", "context": "x"}]
    cfg, errors = validate({"presets": {"items": items}}, DEFAULTS)
    assert set(errors) == {"presets.items.1.name", "presets.items.2.name"}
    assert cfg["presets"]["items"][0] == {"name": "Созвон", "context": "текст"}

    _, errors = validate({"presets": {"active": "нет такого"}}, DEFAULTS)
    assert set(errors) == {"presets.active"}


def test_renaming_active_preset_keeps_it_active():
    current = copy.deepcopy(DEFAULTS)
    current["presets"] = {"active": "Б", "items": [{"name": "А", "context": ""}, {"name": "Б", "context": ""}]}
    cfg, errors = validate({"presets": {"items": [{"name": "А", "context": ""}, {"name": "Б2", "context": ""}]}}, current)
    assert not errors and cfg["presets"]["active"] == "Б2"
    cfg, _ = validate({"presets": {"items": [{"name": "А", "context": ""}]}}, current)
    assert cfg["presets"]["active"] == ""


def test_instructions_include_preset_and_answer_language():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["presets"] = {"active": "Созвон", "items": [{"name": "Созвон", "context": "Проект Альфа на Go"}]}
    cfg["language"]["answer"] = "uk"
    answer, check = build_instructions(cfg)
    assert active_preset(cfg)["name"] == "Созвон"
    assert "Проект Альфа на Go" in answer and "Проект Альфа на Go" in check
    assert "украинский" in answer and "украинский" not in check

    cfg["presets"]["active"] = ""
    cfg["language"]["answer"] = "auto"
    answer, check = build_instructions(cfg)
    assert answer == cfg["prompts"]["answer"] and check == cfg["prompts"]["check"]


def test_restart_needed_only_for_recognition_settings():
    changed = copy.deepcopy(DEFAULTS)
    changed["overlay"]["width"] = 600
    changed["gemini"]["answer_model"] = "other"
    changed["language"]["answer"] = "en"
    changed["presets"]["active"] = ""
    assert not restart_needed(DEFAULTS, changed)
    changed["language"]["speech"] = "uk"
    assert restart_needed(DEFAULTS, changed)


def _texts():
    for key, text in MESSAGES.items():
        yield key, text
    for section in SCHEMA:
        yield section.key, section.title
        if section.description:
            yield section.key, section.description
        for field in section.fields:
            yield f"{section.key}.{field.key}", field.label
            if field.help:
                yield f"{section.key}.{field.key}", field.help
            for value, label in field.options:
                if isinstance(label, T):
                    yield f"{section.key}.{field.key}.{value}", label


def test_every_text_is_translated_with_same_placeholders():
    def placeholders(text):
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}

    for key, text in _texts():
        assert all(text), f"пустой перевод: {key}"
        assert placeholders(text.uk) == placeholders(text.ru) == placeholders(text.en), key
