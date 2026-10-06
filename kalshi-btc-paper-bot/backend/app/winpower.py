"""Windows-only helpers that keep a long-running paper engine alive. No-ops elsewhere.

* ``keep_awake(True)`` asks Windows not to SLEEP while trading is enabled (the display may
  still turn off). Without it the default power plan sleeps the PC after ~15-30 idle minutes
  and the engine silently stops.
* ``disable_quick_edit()`` turns off the console's QuickEdit mode: otherwise a single click in
  the start.ps1 window starts a text selection that blocks the next console write, freezing the
  engine and the dashboard API until a key is pressed.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
STD_INPUT_HANDLE = -10

_awake: bool | None = None


def keep_awake(enabled: bool) -> None:
    global _awake
    if os.name != "nt" or _awake == enabled:
        return
    try:
        import ctypes

        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
        _awake = enabled
        log.info("Windows sleep prevention %s", "ON (trading enabled)" if enabled else "off")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not change Windows sleep setting: %s", exc)


def disable_quick_edit() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return  # stdin is not a console (redirected) - nothing to do
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not disable console QuickEdit mode: %s", exc)
