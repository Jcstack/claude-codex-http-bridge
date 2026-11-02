"""Identify a local process without exposing its executable path or command line."""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
from pathlib import Path
import platform
import re


def capture(pid: int) -> tuple[str, str]:
    if type(pid) is not int or pid < 1:
        raise RuntimeError("Invalid local process identity")
    system = platform.system()
    try:
        if system == "Darwin":
            library = ctypes.CDLL("/usr/lib/libproc.dylib")
            library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            library.proc_pidinfo.restype = ctypes.c_int
            library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            library.proc_pidpath.restype = ctypes.c_int
            info = ctypes.create_string_buffer(136)
            path = ctypes.create_string_buffer(4096)
            if library.proc_pidinfo(pid, 3, 0, info, len(info)) != len(info) or library.proc_pidpath(pid, path, len(path)) <= 0:
                raise RuntimeError("Local process identity is unavailable")
            birth, executable = info.raw[120:136].hex(), os.fsdecode(path.value)
        elif system == "Linux":
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat[stat.rindex(")") + 1:].split()
            if not re.fullmatch(r"[a-fA-F0-9-]{36}", boot) or len(fields) <= 19 or not fields[19].isdigit():
                raise RuntimeError("Local process identity is unavailable")
            birth = boot.lower() + ":" + fields[19]
            executable = os.readlink(f"/proc/{pid}/exe").removesuffix(" (deleted)")
        elif system == "Windows":
            import ctypes.wintypes as W
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
            kernel.OpenProcess.restype = W.HANDLE
            kernel.GetProcessTimes.argtypes = [W.HANDLE, *([ctypes.POINTER(W.FILETIME)] * 4)]
            kernel.GetProcessTimes.restype = W.BOOL
            kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
            kernel.QueryFullProcessImageNameW.restype = W.BOOL
            kernel.CloseHandle.argtypes = [W.HANDLE]
            process = kernel.OpenProcess(0x1000, False, pid)
            if not process: raise RuntimeError("Local process identity is unavailable")
            try:
                times = [W.FILETIME() for _ in range(4)]
                path = ctypes.create_unicode_buffer(32768)
                size = W.DWORD(len(path))
                if (not kernel.GetProcessTimes(process, *(ctypes.byref(value) for value in times)) or
                        not kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size))):
                    raise RuntimeError("Local process identity is unavailable")
                birth = str(times[0].dwLowDateTime | times[0].dwHighDateTime << 32)
                executable = path.value
            finally:
                kernel.CloseHandle(process)
        else:
            raise RuntimeError("Local process identity is not supported on this platform")
        if not birth or not executable:
            raise RuntimeError("Local process identity is unavailable")
        return birth, executable
    except (OSError, ValueError, IndexError, AttributeError):
        raise RuntimeError("Local process identity is unavailable") from None


def stamp(pid: int, identity: tuple[str, str] | None = None) -> str:
    birth, executable = identity or capture(pid)
    material = "\0".join((platform.system(), str(pid), birth, os.path.normcase(executable)))
    return hashlib.sha256(material.encode("utf-8", errors="surrogateescape")).hexdigest()


def matches(pid: int, expected: str) -> bool:
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        return False
    try:
        return hmac.compare_digest(stamp(pid), expected)
    except RuntimeError:
        return False


def executable_matches(identity: tuple[str, str], expected: Path) -> bool:
    actual = identity[1]
    try:
        return os.path.samefile(actual, expected)
    except OSError:
        return os.path.normcase(os.path.realpath(actual)) == os.path.normcase(os.path.realpath(expected))
