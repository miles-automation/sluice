import ctypes
import os
import sys

_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

if sys.platform == "darwin":
    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    _RUSAGE_INFO_V2 = 2

    def sample(pid: int) -> tuple[int, int] | None:
        buf = (ctypes.c_uint64 * 64)()
        if _libproc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(_RUSAGE_INFO_V2), buf) != 0:
            return None
        return int(buf[2 + 6]), int(buf[2 + 7])

else:

    def sample(pid: int) -> tuple[int, int] | None:
        try:
            with open(f"/proc/{pid}/statm") as handle:
                parts = handle.read().split()
        except OSError:
            return None
        rss = int(parts[1]) * _PAGE
        return rss, rss


def self_sample() -> tuple[int, int]:
    value = sample(os.getpid())
    return value if value is not None else (0, 0)


def release_allocator() -> str:
    if sys.platform == "darwin":
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
        libc.malloc_zone_pressure_relief.restype = ctypes.c_size_t
        freed = libc.malloc_zone_pressure_relief(None, ctypes.c_size_t(0))
        return f"malloc_zone_pressure_relief freed {freed}"
    try:
        libc = ctypes.CDLL("libc.so.6")
    except OSError:
        return "no glibc"
    return f"malloc_trim returned {libc.malloc_trim(0)}"
