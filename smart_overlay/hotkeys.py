"""Глобальные горячие клавиши через WinAPI RegisterHotKey — работают, даже когда фокус в другой программе."""

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000
MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
KEYS = {
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "space": 0x20, "enter": 0x0D, "esc": 0x1B,
    "tab": 0x09, "pageup": 0x21, "pagedown": 0x22, "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}


def parse_hotkey(combo: str) -> tuple[int, int]:
    *mods, key = [part.strip().lower() for part in combo.split("+")]
    modifiers = 0
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f"неизвестный модификатор «{mod}» в «{combo}»")
        modifiers |= MODIFIERS[mod]
    if key in KEYS:
        return modifiers, KEYS[key]
    if len(key) == 1 and key.isascii() and key.isalnum():
        return modifiers, ord(key.upper())
    raise ValueError(f"неизвестная клавиша «{key}» в «{combo}»")


class HotkeyListener(threading.Thread):
    """Регистрирует сочетания и вызывает on_hotkey(имя) из своего потока."""

    def __init__(self, bindings: dict[str, str], on_hotkey: Callable[[str], None]):
        super().__init__(name="hotkeys", daemon=True)
        self._bindings = bindings
        self._on_hotkey = on_hotkey
        self._thread_id: int | None = None

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        names: dict[int, str] = {}
        for hotkey_id, (name, combo) in enumerate(self._bindings.items(), start=1):
            if not combo:
                continue
            try:
                modifiers, vk = parse_hotkey(combo)
            except ValueError as e:
                log.warning("Горячая клавиша %s: %s", name, e)
                continue
            if not name.startswith("scroll"):  # прокрутка пусть повторяется при удержании
                modifiers |= MOD_NOREPEAT
            if user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
                names[hotkey_id] = name
            else:
                log.warning("Сочетание %s (%s) уже занято другой программой", combo, name)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in names:
                self._on_hotkey(names[msg.wParam])
        for hotkey_id in names:
            user32.UnregisterHotKey(None, hotkey_id)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
