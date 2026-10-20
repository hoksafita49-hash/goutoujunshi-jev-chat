"""Current Windows user's DPAPI vault. No plaintext fallback."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path


def _dpapi(raw: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("此设备没有 Windows DPAPI，不能保存登录凭据")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("Windows 凭据保护失败，请重新登录")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class SessionStore:
    def __init__(self, root: Path, *, protect=None, unprotect=None):
        self.root = Path(root)
        self.protect = protect or _dpapi
        self.unprotect = unprotect or (lambda raw: _dpapi(raw, True))

    def _path(self, server):
        return self.root / (hashlib.sha256(server.encode()).hexdigest() + ".bin")

    def save(self, server, session):
        data = self.protect(json.dumps(session, ensure_ascii=False).encode("utf-8"))
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(server)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    def load(self, server):
        try:
            data = json.loads(self.unprotect(self._path(server).read_bytes()).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, RuntimeError):
            return None

    def clear(self, server):
        self._path(server).unlink(missing_ok=True)
