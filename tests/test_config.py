import copy

from smart_overlay.config import DEFAULTS, load_config, restart_needed, save_config, validate


def test_defaults_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    save_config(DEFAULTS, path)
    assert load_config(path) == DEFAULTS
    assert "# answer = (по умолчанию)" in path.read_text(encoding="utf-8")  # стандартный промпт не сохраняется


def test_tricky_strings_round_trip(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["prompts"]["answer"] = 'Кавычки " и """ тройные,\nобратный слэш \\ и C:\\path\\n, конец "'
    cfg["gemini"]["api_key"] = 'ab"c\\d'
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    assert load_config(path) == cfg


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
        "whisper": {"language": "klingon"},
        "overlay": {"opacity": "abc"},
        "hotkeys": {"toggle": "ctrl+alt+a", "ask": "ctrl+alt+a", "clear": "ctrl+nope"},
    }
    _, errors = validate(values, DEFAULTS)
    assert set(errors) == {
        "audio.min_silence_ms", "whisper.language", "overlay.opacity", "hotkeys.ask", "hotkeys.clear",
    }


def test_hotkey_conflict_with_unchanged_key_blames_changed_one():
    _, errors = validate({"hotkeys": {"toggle": "ctrl+alt+a"}}, DEFAULTS)  # ask = ctrl+alt+a по умолчанию
    assert set(errors) == {"hotkeys.toggle"}


def test_restart_needed_only_for_audio_and_whisper():
    changed = copy.deepcopy(DEFAULTS)
    changed["overlay"]["width"] = 600
    changed["gemini"]["answer_model"] = "other"
    assert not restart_needed(DEFAULTS, changed)
    changed["whisper"]["beam_size"] = 1
    assert restart_needed(DEFAULTS, changed)
