"""Keep the process on the Mac's native architecture.

The venv's wheels are compiled for the chip this machine actually has. But the
bundled Python is a universal binary, so a terminal running under Rosetta (an
x86_64 shell on an Apple Silicon Mac) silently launches the x86_64 slice, and
every native wheel then fails with "incompatible architecture (have 'arm64',
need 'x86_64')". The user did nothing wrong and the message blames the wrong
thing, so: detect translation and re-exec the same command natively.
"""

from __future__ import annotations

import ctypes
import os
import sys


def _running_under_rosetta() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        # sysctl.proc_translated == 1 means this process is x86_64-under-Rosetta.
        if libc.sysctlbyname(
            b"sysctl.proc_translated", ctypes.byref(value), ctypes.byref(size), None, 0
        ) != 0:
            return False
        return value.value == 1
    except Exception:
        return False


def ensure_native(module: str) -> None:
    """Re-exec `python -m <module> <argv>` natively if we're under Rosetta."""
    if os.environ.get("PROSPECTOR_ARCH_REEXEC"):
        return  # switched once already; a second try means arch itself failed
    if not _running_under_rosetta():
        return
    os.environ["PROSPECTOR_ARCH_REEXEC"] = "1"
    try:
        os.execv(
            "/usr/bin/arch",
            ["arch", "-arm64", sys.executable, "-m", module, *sys.argv[1:]],
        )
    except OSError:
        # arch missing or exec refused: carry on and let the import error show.
        return
