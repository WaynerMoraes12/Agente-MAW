"""Escuta OutputDebugString (protocolo DBWIN) para pegar jassert do build Debug.

Só um ouvinte DBWIN existe por sessão; se outro (DebugView) já estiver ativo,
`.erro` diz isso e nada é capturado — sem fingir que não houve asserção.
"""
from __future__ import annotations
import ctypes
import ctypes.wintypes as wt
import struct
import threading

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateEventW.restype = wt.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_k32.CreateFileMappingW.restype = wt.HANDLE
_k32.CreateFileMappingW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
_k32.MapViewOfFile.restype = ctypes.c_void_p
_k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
_k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.SetEvent.argtypes = [wt.HANDLE]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_INVALID = wt.HANDLE(-1)
_PAGE_READWRITE, _FILE_MAP_READ, _WAIT_OBJECT_0, _ERROR_ALREADY_EXISTS = 0x04, 0x0004, 0, 183


class CapturaDepuracao:
    def __init__(self) -> None:
        self.mensagens: list[tuple[int, str]] = []
        self.erro: str | None = None
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._pronto = self._dados = self._map = None
        self._view = None

    def __enter__(self) -> "CapturaDepuracao":
        self._pronto = _k32.CreateEventW(None, False, False, "DBWIN_BUFFER_READY")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            self.erro = "outro ouvinte de OutputDebugString já está ativo; jassert não capturado"
            return self
        self._dados = _k32.CreateEventW(None, False, False, "DBWIN_DATA_READY")
        self._map = _k32.CreateFileMappingW(_INVALID, None, _PAGE_READWRITE, 0, 4096, "DBWIN_BUFFER")
        self._view = _k32.MapViewOfFile(self._map, _FILE_MAP_READ, 0, 0, 4096)
        if not self._view:
            self.erro = "não foi possível mapear DBWIN_BUFFER"
            return self
        self._thread = threading.Thread(target=self._laco, daemon=True)
        self._thread.start()
        return self

    def _laco(self) -> None:
        _k32.SetEvent(self._pronto)
        while not self._parar.is_set():
            if _k32.WaitForSingleObject(self._dados, 100) == _WAIT_OBJECT_0:
                bruto = ctypes.string_at(self._view, 4096)
                pid = struct.unpack("<I", bruto[:4])[0]
                texto = bruto[4:].split(b"\0", 1)[0].decode("mbcs", errors="replace").rstrip()
                self.mensagens.append((pid, texto))
                _k32.SetEvent(self._pronto)

    def __exit__(self, *exc) -> None:
        self._parar.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._view:
            _k32.UnmapViewOfFile(self._view)
        for h in (self._map, self._dados, self._pronto):
            if h:
                _k32.CloseHandle(h)


def filtrar(mensagens: list[tuple[int, str]], pid: int | None = None) -> list[str]:
    return [t for p, t in mensagens if pid is None or p == pid]


def assercoes(mensagens: list[str]) -> list[str]:
    return [m.strip() for m in mensagens if "JUCE Assertion failure" in m]
