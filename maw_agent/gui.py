"""Driver de GUI: abre um aplicativo Windows numa sessão com o %APPDATA% de teste e o dirige por
UI Automation (pywinauto, backend "uia") e mensagens de janela (PostMessage), sem roubar o teclado
nem o mouse do usuário. Teclado e mouse reais (SendInput, com a janela à frente) só com
`MAW_AGENTE_ENTRADA_REAL=1` (execução noturna).

Ciclo da `SessaoApp` (context manager):
  app do usuário aberta? → recusa (`AppJaAberta`, nada é tocado);
  restaurações pendentes (bandeira `appdata-sujo.json`) → restaura antes de tudo;
  backup da pasta de configuração → bandeira → pasta limpa + arquivo de configuração de teste →
  abre o exe → espera a janela principal → fecha os avisos de abertura (texto registrado) →
  ... cenário ... →
  fecha com graça (WM_CLOSE, "Descartar" na pergunta de salvar) ou mata a árvore de processos →
  restaura a pasta, confere o manifesto, apaga bandeira e backup.

Coordenadas de `clicar`/`arrastar` são de cliente, em pixels físicos (o processo é DPI-aware).
"""
from __future__ import annotations

import ctypes
import io
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from xml.sax.saxutils import quoteattr

# Pixels físicos em todas as chamadas (antes do pywinauto, que também pede isso ao ser importado).
try:
    ctypes.WinDLL("user32").SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except (AttributeError, OSError):
    pass

import psutil  # noqa: E402
from pywinauto import uia_defines  # noqa: E402
from pywinauto.controls.uiawrapper import UIAWrapper  # noqa: E402
from pywinauto.uia_element_info import UIAElementInfo  # noqa: E402

from . import config, redacao, sandbox, trava_appdata  # noqa: E402

ARQUIVO_SETTINGS = "MAW.settings"
# Janela principal: "MAW" ou "MAW - <projeto>" (a abertura mostra antes outra janela, que não casa).
TITULO_PADRAO = r"^MAW(?:\s+-\s+.*)?$"
VARIAVEL_ENTRADA_REAL = "MAW_AGENTE_ENTRADA_REAL"
# Respostas usadas ao fechar (pergunta de salvar) e para avisos de abertura, na ordem de preferência.
RESPOSTAS_FECHAR = ("Descartar", "Não salvar", "Nao salvar", "Não", "Nao", "Don't Save", "No")
RESPOSTAS_ABERTURA = ("OK", "Fechar", "Cancelar", "Não", "Nao", "No", "Descartar")
LIMITE_TRAVADA = 10.0  # segundos seguidos sem responder até `esperar` desistir com AppTravou
_SEM_DIALOGOS = 0x0001 | 0x0002  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX (herdado pelo filho)


# ---------------------------------------------------------------- erros

class ErroApp(Exception):
    """Erro do driver; `captura` é a imagem da janela no momento do erro, quando deu para tirar.
    Sem imagem, `sem_captura` diz por quê (um texto passado no lugar da captura)."""

    def __init__(self, mensagem: str, captura: Path | str | None = None):
        self.captura = captura if isinstance(captura, Path) else None
        self.sem_captura = captura if isinstance(captura, str) else None
        if self.captura is not None:
            mensagem = f"{mensagem} (captura: {self.captura})"
        elif self.sem_captura:
            mensagem = f"{mensagem} (sem captura: {self.sem_captura})"
        super().__init__(mensagem)


class CapturaVazia(ErroApp):
    """A captura da janela veio praticamente de uma cor só (sessão bloqueada, desktop sem pintura): não dá
    para julgar a tela por ela. É do lado do agente — nunca vira achado contra o aplicativo."""


class AppJaAberta(ErroApp):
    """O aplicativo já está aberto (a instância do usuário): nada foi tocado."""


class AppNaoAbriu(ErroApp):
    """O processo não mostrou a janela principal no tempo dado (ou terminou antes)."""


class AppTravou(ErroApp):
    """O aplicativo parou de responder ou terminou sozinho no meio da sessão."""

    def __init__(self, mensagem: str, captura: Path | None = None, motivo: str = "nao_responde"):
        super().__init__(mensagem, captura)
        self.motivo = motivo  # "nao_responde" | "encerrou"


class AmbienteSujo(ErroApp):
    """Há restauração pendente da pasta de configuração que não pôde ser concluída."""


class AmbienteNaoRestaurado(ErroApp):
    """A pasta de configuração não voltou ao estado do backup (o backup foi mantido)."""


class ControleNaoEncontrado(ErroApp, LookupError):
    """Botão, item de menu, aviso ou diálogo não encontrado."""


class EntradaRealNecessaria(ErroApp):
    """A ação só funciona com teclado/mouse reais (MAW_AGENTE_ENTRADA_REAL=1)."""


class SomNecessario(ErroApp):
    """A ação faz o Windows tocar som de sistema: só com MAW_AGENTE_SOM=1 (execução noturna)."""


class AlertaAberto(ErroApp):
    """Erro do agente: mexer na janela de trás com um aviso/diálogo aberto faz tocar o som de alerta."""


class ItemProibido(ErroApp):
    """Erro do agente: o item abre um programa fora do desktop oculto (lista privada de itens proibidos)."""


class AppDoUsuarioAberto(ErroApp):
    """O usuário abriu o aplicativo durante a sessão: ela foi encerrada antes do fim."""


# ---------------------------------------------------------------- Win32 (ctypes)

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_g32 = ctypes.WinDLL("gdi32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwm = ctypes.WinDLL("dwmapi")

ULONG_PTR = ctypes.c_size_t


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                ("hwndCaret", wt.HWND), ("rcCaret", RECT)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _UNIAO_ENTRADA(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _UNIAO_ENTRADA)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _decl(dll, nome: str, res, *args):
    f = getattr(dll, nome)
    f.restype = res
    f.argtypes = list(args)
    return f


EnumWindows = _decl(_u32, "EnumWindows", wt.BOOL, WNDENUMPROC, wt.LPARAM)
EnumChildWindows = _decl(_u32, "EnumChildWindows", wt.BOOL, wt.HWND, WNDENUMPROC, wt.LPARAM)
GetWindowThreadProcessId = _decl(_u32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
IsWindowVisible = _decl(_u32, "IsWindowVisible", wt.BOOL, wt.HWND)
IsWindow = _decl(_u32, "IsWindow", wt.BOOL, wt.HWND)
IsIconic = _decl(_u32, "IsIconic", wt.BOOL, wt.HWND)
IsChild = _decl(_u32, "IsChild", wt.BOOL, wt.HWND, wt.HWND)
GetWindowTextLengthW = _decl(_u32, "GetWindowTextLengthW", ctypes.c_int, wt.HWND)
GetWindowTextW = _decl(_u32, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
GetClassNameW = _decl(_u32, "GetClassNameW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
GetWindowRect = _decl(_u32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(RECT))
GetClientRect = _decl(_u32, "GetClientRect", wt.BOOL, wt.HWND, ctypes.POINTER(RECT))
ClientToScreen = _decl(_u32, "ClientToScreen", wt.BOOL, wt.HWND, ctypes.POINTER(wt.POINT))
ScreenToClient = _decl(_u32, "ScreenToClient", wt.BOOL, wt.HWND, ctypes.POINTER(wt.POINT))
MapWindowPoints = _decl(_u32, "MapWindowPoints", ctypes.c_int, wt.HWND, wt.HWND, ctypes.POINTER(wt.POINT), wt.UINT)
ChildWindowFromPointEx = _decl(_u32, "ChildWindowFromPointEx", wt.HWND, wt.HWND, wt.POINT, wt.UINT)
PostMessageW = _decl(_u32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
SendMessageTimeoutW = _decl(_u32, "SendMessageTimeoutW", wt.LPARAM, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
                            wt.UINT, wt.UINT, ctypes.POINTER(ULONG_PTR))
IsHungAppWindow = _decl(_u32, "IsHungAppWindow", wt.BOOL, wt.HWND)
PrintWindow = _decl(_u32, "PrintWindow", wt.BOOL, wt.HWND, wt.HDC, wt.UINT)
GetDC = _decl(_u32, "GetDC", wt.HDC, wt.HWND)
ReleaseDC = _decl(_u32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HDC)
GetForegroundWindow = _decl(_u32, "GetForegroundWindow", wt.HWND)
SetForegroundWindow = _decl(_u32, "SetForegroundWindow", wt.BOOL, wt.HWND)
BringWindowToTop = _decl(_u32, "BringWindowToTop", wt.BOOL, wt.HWND)
ShowWindow = _decl(_u32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
AttachThreadInput = _decl(_u32, "AttachThreadInput", wt.BOOL, wt.DWORD, wt.DWORD, wt.BOOL)
GetGUIThreadInfo = _decl(_u32, "GetGUIThreadInfo", wt.BOOL, wt.DWORD, ctypes.POINTER(GUITHREADINFO))
MapVirtualKeyW = _decl(_u32, "MapVirtualKeyW", wt.UINT, wt.UINT, wt.UINT)
VkKeyScanW = _decl(_u32, "VkKeyScanW", ctypes.c_short, wt.WCHAR)
SendInput = _decl(_u32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
SetCursorPos = _decl(_u32, "SetCursorPos", wt.BOOL, ctypes.c_int, ctypes.c_int)
GetCursorPos = _decl(_u32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
GetClassLongPtrW = _decl(_u32, "GetClassLongPtrW", ULONG_PTR, wt.HWND, ctypes.c_int)
GetDlgCtrlID = _decl(_u32, "GetDlgCtrlID", ctypes.c_int, wt.HWND)
GetWindow = _decl(_u32, "GetWindow", wt.HWND, wt.HWND, wt.UINT)
GetAncestor = _decl(_u32, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
WindowFromPoint = _decl(_u32, "WindowFromPoint", wt.HWND, wt.POINT)
BitBlt = _decl(_g32, "BitBlt", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC,
               ctypes.c_int, ctypes.c_int, wt.DWORD)
CreateCompatibleDC = _decl(_g32, "CreateCompatibleDC", wt.HDC, wt.HDC)
CreateCompatibleBitmap = _decl(_g32, "CreateCompatibleBitmap", wt.HBITMAP, wt.HDC, ctypes.c_int, ctypes.c_int)
SelectObject = _decl(_g32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
DeleteObject = _decl(_g32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
DeleteDC = _decl(_g32, "DeleteDC", wt.BOOL, wt.HDC)
GetDIBits = _decl(_g32, "GetDIBits", ctypes.c_int, wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                  ctypes.POINTER(BITMAPINFO), wt.UINT)
DwmGetWindowAttribute = _decl(_dwm, "DwmGetWindowAttribute", ctypes.c_long, wt.HWND, wt.DWORD, ctypes.c_void_p,
                              wt.DWORD)
GetCurrentThreadId = _decl(_k32, "GetCurrentThreadId", wt.DWORD)
GetErrorMode = _decl(_k32, "GetErrorMode", wt.UINT)
SetErrorMode = _decl(_k32, "SetErrorMode", wt.UINT, wt.UINT)

WM_NULL, WM_CLOSE, WM_SETTEXT, WM_COMMAND, BM_CLICK = 0x0000, 0x0010, 0x000C, 0x0111, 0x00F5
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE = 0x0200
SMTO_ABORTIFHUNG = 0x0002
PW_RENDERFULLCONTENT = 0x00000002
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT = 0x0001, 0x0004
GCL_STYLE, CS_DBLCLKS = -26, 0x0008
GW_OWNER = 4
GA_ROOT = 2
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
SW_RESTORE = 9
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
UIA_IS_DIALOG = 30174  # UIA_IsDialogPropertyId

# (mensagem de descer, de subir, de duplo clique, flag MK_ do wParam, SendInput descer, SendInput subir)
_BOTOES = {
    "esquerdo": (0x0201, 0x0202, 0x0203, 0x0001, 0x0002, 0x0004),
    "direito": (0x0204, 0x0205, 0x0206, 0x0002, 0x0008, 0x0010),
    "meio": (0x0207, 0x0208, 0x0209, 0x0010, 0x0020, 0x0040),
}


# ---------------------------------------------------------------- janelas

def _texto(hwnd: int) -> str:
    n = GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _classe(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid_de(hwnd: int) -> int:
    pid = wt.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _thread_de(hwnd: int) -> int:
    return GetWindowThreadProcessId(hwnd, None)


def _encoberta(hwnd: int) -> bool:
    v = wt.DWORD()
    return DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0 and v.value != 0


# Janelas do indicador de idioma/entrada que o Windows cria dentro de todo processo com janela: no
# desktop oculto elas ficam visíveis (medido com a janela de teste e com o aplicativo real).
CLASSES_DO_SISTEMA = frozenset({"UAC_InputIndicatorOverlayWnd", "UAC Input Indicator"})


def janelas_do_processo(pid: int, visiveis: bool = True) -> list[int]:
    """Janelas de topo do processo, da de cima para a de baixo (ordem Z). Com `visiveis`, sem as
    janelas do indicador de entrada do Windows (CLASSES_DO_SISTEMA)."""
    out: list[int] = []

    def cb(h, _):
        if _pid_de(h) == pid and (not visiveis or (IsWindowVisible(h) and not _encoberta(h)
                                                   and _classe(h) not in CLASSES_DO_SISTEMA)):
            out.append(int(h))
        return True

    EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _filhas(hwnd: int) -> list[int]:
    out: list[int] = []

    def cb(h, _):
        out.append(int(h))
        return True

    EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return out


def _retangulo(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _area(hwnd: int) -> int:
    l, t, r, b = _retangulo(hwnd)
    return max(0, r - l) * max(0, b - t)


def _com_timeout(funcao: Callable[[], Any], timeout: float) -> tuple[bool, Any]:
    """(terminou, valor). Para chamadas Win32 que podem bloquear numa janela travada."""
    caixa: dict = {}

    def alvo():
        try:
            caixa["v"] = funcao()
        except BaseException as ex:  # devolvida para quem chamou
            caixa["e"] = ex

    t = threading.Thread(target=alvo, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None
    if "e" in caixa:
        raise caixa["e"]
    return True, caixa.get("v")


def responde(hwnd: int, timeout_ms: int = 2000) -> bool:
    """A janela processa mensagens? (IsHungAppWindow e um WM_NULL com prazo)."""
    if not IsWindow(hwnd) or IsHungAppWindow(hwnd):
        return False
    res = ULONG_PTR()
    return SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(res)) != 0


def capturar_janela(hwnd: int):
    """Imagem (PIL, RGB) da janela por PrintWindow(PW_RENDERFULLCONTENT): funciona com a janela
    coberta por outras e com conteúdo desenhado por Direct2D; recorta a moldura invisível do DWM."""
    from PIL import Image

    l, t, r, b = _retangulo(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        raise ErroApp(f"janela {hwnd:#x} sem área para capturar")
    tela = GetDC(None)
    mem = CreateCompatibleDC(tela)
    bmp = CreateCompatibleBitmap(tela, w, h)
    antigo = SelectObject(mem, bmp)
    try:
        if not PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
            raise ErroApp(f"PrintWindow falhou para a janela {hwnd:#x}")
        SelectObject(mem, antigo)  # GetDIBits exige o bitmap fora do DC
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h  # de cima para baixo
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0) != h:
            raise ErroApp(f"GetDIBits falhou para a janela {hwnd:#x}")
    finally:
        SelectObject(mem, antigo)
        DeleteObject(bmp)
        DeleteDC(mem)
        ReleaseDC(None, tela)
    img = Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    ext = RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(ext), ctypes.sizeof(ext)) == 0:
        caixa = (max(0, ext.left - l), max(0, ext.top - t), min(w, ext.right - l), min(h, ext.bottom - t))
        if caixa[2] > caixa[0] and caixa[3] > caixa[1] and caixa != (0, 0, w, h):
            img = img.crop(caixa)
    return img


GW_HWNDPREV = 3


def _visivel_na_tela(hwnd: int) -> bool:
    """Nenhuma janela visível acima dela (na ordem Z) cruza o seu retângulo? Só então copiar a tela
    não expõe outras janelas. A única exceção é o "fantasma" que o Windows põe no lugar dela quando
    ela trava: classe Ghost, o mesmo retângulo e o título começando pelo dela (o de outro aplicativo
    travado nunca serve)."""
    if not IsWindow(hwnd) or IsIconic(hwnd):
        return False
    l, t, r, b = _retangulo(hwnd)
    if r - l < 4 or b - t < 4:
        return False
    titulo = _texto(hwnd)
    h = GetWindow(hwnd, GW_HWNDPREV)
    for _ in range(10000):
        if not h:
            return True
        if IsWindowVisible(h) and not _encoberta(h):
            hl, ht, hr, hb = _retangulo(h)
            if hl < r and hr > l and ht < b and hb > t:
                fantasma = (_classe(h) == "Ghost" and (hl, ht, hr, hb) == (l, t, r, b)
                            and bool(titulo) and _texto(h).startswith(titulo))
                if not fantasma:
                    return False
        h = GetWindow(h, GW_HWNDPREV)
    return False


def capturar_tela_da_janela(hwnd: int):
    """Cópia da tela no retângulo da janela (para janela travada, que não atende ao PrintWindow)."""
    from PIL import Image

    ext = RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(ext), ctypes.sizeof(ext)) == 0:
        l, t, r, b = ext.left, ext.top, ext.right, ext.bottom
    else:
        l, t, r, b = _retangulo(hwnd)
    w, h = r - l, b - t
    tela = GetDC(None)
    mem = CreateCompatibleDC(tela)
    bmp = CreateCompatibleBitmap(tela, w, h)
    antigo = SelectObject(mem, bmp)
    try:
        if not BitBlt(mem, 0, 0, w, h, tela, l, t, SRCCOPY | CAPTUREBLT):
            raise ErroApp("BitBlt da tela falhou")
        SelectObject(mem, antigo)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    finally:
        SelectObject(mem, antigo)
        DeleteObject(bmp)
        DeleteDC(mem)
        ReleaseDC(None, tela)
    return Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)


def fracao_cor_dominante(img) -> float:
    """Fração da imagem ocupada pela cor mais comum (amostra reduzida sem mistura de cores)."""
    from PIL import Image

    rgb = img.convert("RGB")
    w, h = rgb.size
    fator = max(1, max(w, h) // 256)
    amostra = rgb.resize((max(1, w // fator), max(1, h // fator)), Image.NEAREST)
    total = amostra.size[0] * amostra.size[1]
    cores = amostra.getcolors(total) or []
    return max((n for n, _ in cores), default=0) / total if total else 1.0


LIMITE_CAPTURA_VAZIA = 0.99


def _png(img) -> bytes:
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def _trazer_para_frente(hwnd: int, com_alt: bool = False) -> bool:
    """SetForegroundWindow contornando a trava de primeiro plano com AttachThreadInput (sem entrada
    real); com `com_alt`, um toque de Alt por SendInput (só com entrada real)."""
    if GetForegroundWindow() == hwnd:
        return True
    if IsIconic(hwnd):
        ShowWindow(hwnd, SW_RESTORE)
    frente = GetForegroundWindow()
    eu = GetCurrentThreadId()
    outro = _thread_de(frente) if frente else 0
    ligado = bool(outro and outro != eu and AttachThreadInput(eu, outro, True))
    try:
        BringWindowToTop(hwnd)
        SetForegroundWindow(hwnd)
    finally:
        if ligado:
            AttachThreadInput(eu, outro, False)
    if GetForegroundWindow() != hwnd and com_alt:
        _enviar([_tecla_input(0x12, False), _tecla_input(0x12, True)])
        SetForegroundWindow(hwnd)
    limite = time.monotonic() + 1.0
    while time.monotonic() < limite:
        if GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return False


def entrada_real() -> bool:
    """Teclado e mouse reais (SendInput) liberados: MAW_AGENTE_ENTRADA_REAL=1 e fora do desktop
    oculto (lá a entrada real não chega, e um SendInput iria para o desktop do usuário)."""
    return os.environ.get(VARIAVEL_ENTRADA_REAL) == "1" and not desktop_oculto()


# ---------------------------------------------------------------- desktop oculto (Task 3b)
# Medido numa thread do desktop oculto (`maw_agent.desktop_oculto`), com a janela de teste e com o
# aplicativo real: GetForegroundWindow() devolve NULL e SetForegroundWindow falha sem mexer no
# primeiro plano do usuário. A JUCE trata o NULL como "este processo está em primeiro plano", então
# os menus dela ficam abertos sem trazer nada para a frente. GetAsyncKeyState é sempre 0 e
# GetCursorPos falha: a entrada real do usuário não chega lá (e a JUCE, que lê Ctrl/Shift/Alt por
# GetAsyncKeyState, nunca vê modificador). PrintWindow(PW_RENDERFULLCONTENT) desenha a janela (sem a
# flag, a janela Direct2D sai preta). Copiar a tela (GetDC(NULL) + BitBlt) falha.

def desktop_oculto() -> bool:
    """O agente roda no desktop oculto (variável ligada pelo lançador e a thread fora da tela)."""
    from . import desktop_oculto as _do

    return _do.no_desktop_oculto()


GetKeyboardState = _decl(_u32, "GetKeyboardState", wt.BOOL, ctypes.c_void_p)
SetKeyboardState = _decl(_u32, "SetKeyboardState", wt.BOOL, ctypes.c_void_p)
PeekMessageW = _decl(_u32, "PeekMessageW", wt.BOOL, ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT, wt.UINT)
_VK_ESQUERDA = {0x10: 0xA0, 0x11: 0xA2, 0x12: 0xA4}  # VK_LSHIFT, VK_LCONTROL, VK_LMENU
_MK_MODIFICADOR = {0x10: 0x0004, 0x11: 0x0008}  # MK_SHIFT, MK_CONTROL (Alt não tem bit no wParam do mouse)


def _janela_juce(hwnd: int) -> bool:
    raiz = GetAncestor(hwnd, GA_ROOT) or hwnd
    return _classe(raiz).startswith("JUCE_")


def _com_modificadores(hwnd: int, vks: Sequence[int], acao: Callable[[], None], mensagens: int,
                       pid: int | None) -> None:
    """Liga Ctrl/Shift/Alt só no estado de teclado da thread da janela (AttachThreadInput +
    SetKeyboardState), posta as mensagens de `acao`, espera a janela processá-las e devolve o estado
    anterior. Não mexe no estado físico (GetAsyncKeyState) nem em outra thread: serve para quem lê os
    modificadores por GetKeyState (Win32, WinForms), não para a JUCE."""
    if not pid or _pid_de(hwnd) != pid:  # nunca se ligar à thread de uma janela de outro processo
        raise ErroApp(f"a janela {hwnd:#x} não é do aplicativo da sessão (pid {pid})")
    eu, ela = GetCurrentThreadId(), _thread_de(hwnd)
    msg = (ctypes.c_byte * 64)()
    PeekMessageW(msg, None, 0, 0, 0)  # garante a fila de mensagens desta thread
    if not ela or not AttachThreadInput(eu, ela, True):
        raise ErroApp(f"AttachThreadInput falhou para a janela {hwnd:#x} (erro {ctypes.get_last_error()})")
    try:
        antes = (ctypes.c_ubyte * 256)()
        if not GetKeyboardState(antes):
            raise ErroApp(f"GetKeyboardState falhou (erro {ctypes.get_last_error()})")
        novo = (ctypes.c_ubyte * 256)(*antes)
        for vk in vks:
            novo[vk] |= 0x80
            novo[_VK_ESQUERDA.get(vk, vk)] |= 0x80
        if not SetKeyboardState(novo):
            raise ErroApp(f"SetKeyboardState falhou (erro {ctypes.get_last_error()})")
        try:
            acao()
            # cada ida e volta de WM_NULL passa por uma chamada de GetMessage da janela, que entrega
            # uma mensagem postada (e o WM_CHAR que o TranslateMessage gera) de cada vez
            limite = time.monotonic() + 0.15
            voltas = 0
            while voltas < 2 * mensagens + 2 or time.monotonic() < limite:
                res = ULONG_PTR()
                if not SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, 1000, ctypes.byref(res)):
                    break
                voltas += 1
        finally:
            SetKeyboardState(antes)
    finally:
        AttachThreadInput(eu, ela, False)


def _recusar_modificador_juce(hwnd: int, oque: str) -> None:
    if _janela_juce(hwnd):
        raise EntradaRealNecessaria(
            f"{oque} tem modificador: a JUCE lê Ctrl/Shift/Alt do teclado físico (GetAsyncKeyState), que "
            f"mensagens postadas não mudam (e que no desktop oculto é sempre 0); use o item de menu "
            f"equivalente ou {VARIAVEL_ENTRADA_REAL}=1 fora do desktop oculto")


# ---------------------------------------------------------------- teclas

MODIFICADORES = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12}
_VK_NOMES = {
    "space": 0x20, "espaco": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "esquerda": 0x25, "cima": 0x26, "direita": 0x27, "baixo": 0x28,
}
_ESTENDIDAS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}


class Tecla:
    """Combinação interpretada: modificadores (VKs), a tecla (VK, 0 = só caractere) e o caractere."""

    __slots__ = ("modificadores", "vk", "caractere", "precisa_shift")

    def __init__(self, modificadores: tuple[int, ...], vk: int, caractere: str | None, precisa_shift: bool):
        self.modificadores, self.vk, self.caractere, self.precisa_shift = modificadores, vk, caractere, precisa_shift

    def __repr__(self) -> str:
        return f"Tecla(mod={self.modificadores}, vk={self.vk:#x}, car={self.caractere!r}, shift={self.precisa_shift})"


def interpretar_teclas(combinacao: str) -> Tecla:
    """'ctrl+z', 'ctrl+shift+s', 'k', 'space', 'f5', ',', '+', 'ctrl++' → Tecla."""
    s = combinacao.strip()
    if not s:
        raise ValueError("combinação de teclas vazia")
    if s == "+":
        mods_txt, base = [], "+"
    elif s.endswith("++"):
        mods_txt, base = s[:-2].split("+"), "+"
    else:
        *mods_txt, base = s.split("+")
    mods: list[int] = []
    for m in mods_txt:
        vk_m = MODIFICADORES.get(m.strip().lower())
        if vk_m is None:
            raise ValueError(f"modificador desconhecido em {combinacao!r}: {m!r}")
        if vk_m not in mods:
            mods.append(vk_m)
    if not base:
        raise ValueError(f"combinação sem tecla: {combinacao!r}")
    nome = base.lower()
    if nome in _VK_NOMES:
        return Tecla(tuple(mods), _VK_NOMES[nome], None, False)
    m = re.fullmatch(r"f(\d{1,2})", nome)
    if m and 1 <= int(m.group(1)) <= 24:
        return Tecla(tuple(mods), 0x6F + int(m.group(1)), None, False)
    if len(base) != 1:
        raise ValueError(f"tecla desconhecida em {combinacao!r}: {base!r}")
    if base.isascii() and base.isalnum():
        return Tecla(tuple(mods), ord(base.upper()), base.lower() if base.isalpha() else base, False)
    r = VkKeyScanW(base)
    if r == -1 or (r >> 8) & 0x06:  # sem tecla no layout, ou só com Ctrl/Alt (AltGr): vai como caractere
        return Tecla(tuple(mods), 0, base, False)
    return Tecla(tuple(mods), r & 0xFF, base, bool((r >> 8) & 0x01))


def _lparam_tecla(vk: int, subindo: bool) -> int:
    scan = MapVirtualKeyW(vk, 0)
    lp = 1 | (scan << 16) | ((1 << 24) if vk in _ESTENDIDAS else 0)
    if subindo:
        lp |= (1 << 30) | (1 << 31)
    return lp


def _postar_tecla(hwnd: int, t: Tecla) -> None:
    """PostMessage de uma tecla sem modificador. A fila do app gera o WM_CHAR (TranslateMessage);
    caractere que pede Shift ou que não tem tecla no layout vai direto como WM_CHAR."""
    if t.vk == 0 or t.precisa_shift:
        scan = MapVirtualKeyW(t.vk, 0) if t.vk else 0
        PostMessageW(hwnd, WM_CHAR, ord(t.caractere), 1 | (scan << 16))
        return
    PostMessageW(hwnd, WM_KEYDOWN, t.vk, _lparam_tecla(t.vk, False))
    PostMessageW(hwnd, WM_KEYUP, t.vk, _lparam_tecla(t.vk, True))


def _tecla_input(vk: int, subindo: bool, unicode: int | None = None) -> INPUT:
    i = INPUT()
    i.type = INPUT_KEYBOARD
    if unicode is not None:
        i.u.ki = KEYBDINPUT(0, unicode, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if subindo else 0), 0, 0)
    else:
        flags = (KEYEVENTF_EXTENDEDKEY if vk in _ESTENDIDAS else 0) | (KEYEVENTF_KEYUP if subindo else 0)
        i.u.ki = KEYBDINPUT(vk, MapVirtualKeyW(vk, 0), flags, 0, 0)
    return i


def _mouse_input(flags: int) -> INPUT:
    i = INPUT()
    i.type = INPUT_MOUSE
    i.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, 0)
    return i


def _enviar(entradas: list[INPUT]) -> None:
    arr = (INPUT * len(entradas))(*entradas)
    if SendInput(len(entradas), arr, ctypes.sizeof(INPUT)) != len(entradas):
        raise ErroApp(f"SendInput recusou a entrada (erro {ctypes.get_last_error()})")


def _enviar_tecla_real(t: Tecla) -> None:
    mods = list(t.modificadores)
    if t.precisa_shift and 0x10 not in mods:
        mods.append(0x10)
    seq = [_tecla_input(m, False) for m in mods]
    if t.vk:
        seq += [_tecla_input(t.vk, False), _tecla_input(t.vk, True)]
    else:
        seq += [_tecla_input(0, False, ord(t.caractere)), _tecla_input(0, True, ord(t.caractere))]
    seq += [_tecla_input(m, True) for m in reversed(mods)]
    _enviar(seq)


# ---------------------------------------------------------------- UI Automation

def _norm(s: str | None) -> str:
    s = (s or "").replace("&", "").replace("…", "...").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*\.\.\.$", "", s)
    return s.casefold()


def _casa(nome: str, alvo: str | re.Pattern, prefixo: bool = False) -> bool:
    if isinstance(alvo, re.Pattern):
        return alvo.search(nome or "") is not None
    n, a = _norm(nome), _norm(alvo)
    return n == a or (prefixo and bool(a) and n.startswith(a))


def _elemento(hwnd: int) -> UIAElementInfo:
    return UIAElementInfo(hwnd)


def _padrao(info: UIAElementInfo, nome: str):
    try:
        return uia_defines.get_elem_interface(info.element, nome)
    except uia_defines.NoPatternInterfaceError:
        return None


def _eh_dialogo_uia(info: UIAElementInfo) -> bool:
    try:
        return bool(info.element.GetCurrentPropertyValue(UIA_IS_DIALOG))
    except Exception:  # propriedade ausente em versões antigas do UIA
        return False


def _descendentes(hwnd: int, tipos: Iterable[str] | None = None) -> list[UIAElementInfo]:
    raiz = _elemento(hwnd)
    if tipos is None:
        return list(raiz.descendants())
    out: list[UIAElementInfo] = []
    for tipo in tipos:
        out += raiz.descendants(control_type=tipo)
    return out


TIPOS_BOTAO = ("Button", "CheckBox", "RadioButton", "MenuItem", "SplitButton", "Hyperlink", "TabItem")


class Controle:
    """Um elemento de UI Automation (botão, item de menu, slider, caixa de texto...)."""

    def __init__(self, info: UIAElementInfo, sessao: "SessaoApp | None" = None):
        self.info = info
        self._sessao = sessao

    def __repr__(self) -> str:
        return f"Controle({self.tipo} {self.nome!r})"

    @property
    def nome(self) -> str:
        return self.info.name or ""

    @property
    def tipo(self) -> str:
        return self.info.control_type or ""

    @property
    def habilitado(self) -> bool:
        return bool(self.info.enabled)

    @property
    def retangulo(self) -> tuple[int, int, int, int]:
        """(esquerda, topo, direita, base) em pixels físicos de tela."""
        r = self.info.rectangle
        return r.left, r.top, r.right, r.bottom

    @property
    def wrapper(self) -> "JanelaSegura":
        """O wrapper UIA do pywinauto, sem os métodos de entrada real (ver JanelaSegura)."""
        return JanelaSegura(UIAWrapper(self.info))

    def _conferir(self) -> None:
        """Numa sessão: viva, sem aviso aberto por trás (som) e, de dia, fora da lista de proibidos."""
        if self._sessao is None:
            return
        self._sessao._exigir_viva()
        _recusar_proibido(self.nome)
        self._sessao._recusar_se_modal(_raiz_do_elemento(self.info))

    def invocar(self) -> None:
        """Aciona: Invoke; senão Toggle, ExpandCollapse, SelectionItem ou a ação padrão (MSAA)."""
        self._conferir()
        p = _padrao(self.info, "Invoke")
        if p is not None:
            p.Invoke()
            return
        p = _padrao(self.info, "Toggle")
        if p is not None:
            p.Toggle()
            return
        p = _padrao(self.info, "ExpandCollapse")
        if p is not None:
            p.Expand()
            return
        p = _padrao(self.info, "SelectionItem")
        if p is not None:
            p.Select()
            return
        p = _padrao(self.info, "LegacyIAccessible")
        if p is not None:
            p.DoDefaultAction()
            return
        raise ControleNaoEncontrado(f"{self!r} não tem padrão de acionamento (Invoke/Toggle/...)")

    def alternar(self) -> None:
        self._conferir()
        p = _padrao(self.info, "Toggle")
        if p is not None:
            p.Toggle()
        else:
            self.invocar()

    def expandir(self) -> None:
        p = _padrao(self.info, "ExpandCollapse")
        if p is not None:
            p.Expand()
        else:
            self.invocar()

    def valor(self) -> bool | float | str:
        """Toggle → bool; RangeValue → float; Value → str; senão o nome."""
        p = _padrao(self.info, "Toggle")
        if p is not None:
            return p.CurrentToggleState == 1
        p = _padrao(self.info, "RangeValue")
        if p is not None:
            return float(p.CurrentValue)
        p = _padrao(self.info, "Value")
        if p is not None:
            return p.CurrentValue or ""
        return self.nome

    def definir(self, valor: float | str) -> None:
        """RangeValue.SetValue (número) ou Value.SetValue (texto)."""
        self._conferir()
        p = _padrao(self.info, "RangeValue")
        if p is not None and isinstance(valor, (int, float)):
            p.SetValue(float(valor))
            return
        p = _padrao(self.info, "Value")
        if p is not None:
            p.SetValue(str(valor))
            return
        raise ControleNaoEncontrado(f"{self!r} não aceita valor (sem RangeValue/Value)")

    def centro_tela(self) -> tuple[int, int]:
        l, t, r, b = self.retangulo
        return (l + r) // 2, (t + b) // 2

    def clicar(self, botao: str = "esquerdo", duplo: bool = False) -> None:
        """Clique (PostMessage, ou real com MAW_AGENTE_ENTRADA_REAL=1) no centro do elemento."""
        if self._sessao is None:
            raise ErroApp("clicar num Controle exige uma sessão")
        self._sessao._clicar_tela(self.centro_tela(), botao, duplo)


# ---------------------------------------------------------------- configuração de teste

def settings_xml(valores: dict[str, Any]) -> str:
    """Arquivo de propriedades no formato XML da JUCE (PropertiesFile::storeAsXML)."""
    linhas = ['<?xml version="1.0" encoding="UTF-8"?>', "", "<PROPERTIES>"]
    for k, v in valores.items():
        linhas.append(f"  <VALUE name={quoteattr(str(k))} val={quoteattr(str(v))}/>")
    linhas.append("</PROPERTIES>")
    return "\n".join(linhas) + "\n"


def _mesclar_settings(existente: str, valores: dict[str, Any]) -> str:
    import xml.etree.ElementTree as ET

    raiz = ET.fromstring(existente)
    for k, v in valores.items():
        for el in raiz.findall("VALUE"):
            if el.get("name") == str(k):
                el.set("val", str(v))
                break
        else:
            ET.SubElement(raiz, "VALUE", {"name": str(k), "val": str(v)})
    atuais = {el.get("name"): el.get("val", "") for el in raiz.findall("VALUE")}
    return settings_xml(atuais)


def app_aberta(exe: Path) -> bool:
    """Há um processo com o nome deste executável (ou a MAW do usuário) rodando?"""
    from . import suite

    nome = Path(exe).name.casefold()
    if suite.maw_aberta():
        return True
    return any((p.info.get("name") or "").casefold() == nome for p in psutil.process_iter(["name"]))


# ---------------------------------------------------------------- ambiente (%APPDATA%)

def outra_instancia(exe: Path, pid_proprio: int | Iterable[int] | None) -> int | None:
    """PID de outra instância do aplicativo (processo com o nome do exe, ou um MAW_APP.exe) além do(s)
    processo(s) `pid_proprio` desta sessão, ou None. (Fora da nossa árvore, é a do usuário.)"""
    nomes = {Path(exe).name.casefold(), "maw_app.exe"}
    nossos = {pid_proprio} if pid_proprio is None or isinstance(pid_proprio, int) else set(pid_proprio)
    for p in psutil.process_iter(["name"]):
        if p.pid not in nossos and (p.info.get("name") or "").casefold() in nomes:
            return p.pid
    return None


class _RegistroTolerante:
    """Recebe o registro da restauração para o estado da sprint sem deixar uma falha ao registrar
    atrapalhar a restauração, que vem antes (a falha fica em `erro`)."""

    def __init__(self, alvo, pasta: Path, erro: str | None = None):
        self._alvo = alvo
        self.pasta = Path(pasta)
        self.erro = erro

    def registrar_restauracao(self, registro: dict) -> None:
        if self._alvo is None:
            return
        try:
            self._alvo.registrar_restauracao(registro)
        except Exception as ex:  # a restauração já aconteceu; o registro é o que falhou
            self.erro = f"registro da restauração não gravado: {ex}"


class _RegistroAvulso:
    """Fora de uma sprint: a bandeira fica ao lado da pasta de backups e as restaurações ficam aqui
    (mesma interface que `fases._restaurar_ambiente` usa do Estado)."""

    def __init__(self, pasta: Path):
        self.pasta = Path(pasta)
        self.restauracoes: list[dict] = []

    def registrar_restauracao(self, registro: dict) -> None:
        self.restauracoes.append({"quando": time.strftime("%Y-%m-%dT%H:%M:%S"), **registro})


def _dentro(caminho: Path, pasta: Path) -> bool:
    try:
        Path(caminho).resolve().relative_to(Path(pasta).resolve())
        return True
    except ValueError:
        return False


def _pasta_avulsa() -> Path:
    return Path(config.BACKUPS).parent


def restaurar_pendencias(atual=None) -> list[dict]:
    """Restaura o que execuções que caíram deixaram sujo, olhando as bandeiras de todos os lugares
    (sprints, sessão avulsa deste driver, calibração): `fases.restaurar_pendencias_ambiente`, que
    restaura só a mais antiga, descarta as mais novas e adia tudo com o aplicativo aberto. Devolve os
    registros na ordem em que aconteceram."""
    from . import fases

    out = fases.restaurar_pendencias_ambiente("gui", atual)
    return [*out["restauradas"], *out["descartadas"], *out["adiadas"], *out["erros"]]


def pendencias() -> list[Path]:
    """Pastas com bandeira appdata-sujo.json ainda presente (`fases.pendencias_ambiente`)."""
    from . import fases

    return [Path(p["flag"]).parent for p in fases.pendencias_ambiente()]


# ---------------------------------------------------------------- guardas de segurança (Task 3b)
# A trava do %APPDATA%\MAW é a de `trava_appdata` (a mesma da suíte, da calibração e das restaurações).

VARIAVEL_SOM = "MAW_AGENTE_SOM"
ITENS_PROIBIDOS = ("bancada", "itens-proibidos.yaml")  # dentro de privado/
PASSO_ESTRANGEIRA = 0.5  # segundos entre as olhadas atrás da MAW do usuário durante a sessão
_METODOS_REAIS = frozenset({"click_input", "double_click_input", "right_click_input", "press_mouse_input",
                            "release_mouse_input", "move_mouse_input", "drag_mouse_input", "wheel_mouse_input",
                            "type_keys", "set_focus", "draw_outline", "capture_as_image"})


def som_liberado() -> bool:
    """Som de sistema permitido (MAW_AGENTE_SOM=1: execução noturna)."""
    return os.environ.get(VARIAVEL_SOM) == "1"


_proibidos: dict = {}


def itens_proibidos() -> list[dict]:
    """Rótulos de itens e botões que abrem programa fora do desktop oculto (Explorer, navegador), da lista
    privada `privado/bancada/itens-proibidos.yaml`. Sem o arquivo: lista vazia (avisado uma vez)."""
    caminho = Path(config.PRIVADO).joinpath(*ITENS_PROIBIDOS)
    try:
        mtime = caminho.stat().st_mtime
    except OSError:
        if not _proibidos.get("avisado"):
            _proibidos["avisado"] = True
            try:
                print(f"sem lista de itens proibidos ({caminho}): nenhum item é recusado", file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass
        return []
    if _proibidos.get("chave") != (str(caminho), mtime):
        import yaml

        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
        itens = [i for i in (dados.get("itens") or []) if isinstance(i, dict) and i.get("rotulo")]
        _proibidos.update(chave=(str(caminho), mtime), itens=itens)
    return _proibidos["itens"]


def item_proibido(nome: str | None) -> dict | None:
    """O item da lista que casa com `nome` (igual, ou `nome` começando pelo rótulo; sem caixa, & e ...)."""
    n = _norm(nome)
    if not n:
        return None
    for i in itens_proibidos():
        r = _norm(str(i["rotulo"]))
        if r and (n == r or n.startswith(r)):
            return i
    return None


def _recusar_proibido(nome: str | None) -> None:
    """De dia (sem entrada real) um item da lista de proibidos nunca é acionado."""
    if entrada_real():
        return
    i = item_proibido(nome)
    if i is not None:
        raise ItemProibido(f"{nome!r} está na lista de itens proibidos de dia: "
                           f"{i.get('motivo') or 'abre um programa fora do desktop oculto'}")


def _raiz_do_elemento(info) -> int:
    """Janela de topo de um elemento UIA (subindo até um elemento com HWND); 0 se não achar."""
    e = info
    for _ in range(64):
        if e is None:
            return 0
        try:
            h = e.handle
        except Exception:  # elemento sumindo
            h = None
        if h:
            return int(GetAncestor(h, GA_ROOT) or h)
        try:
            e = e.parent
        except Exception:
            return 0
    return 0


def _embrulhar(valor):
    from pywinauto.base_wrapper import BaseWrapper

    if isinstance(valor, BaseWrapper):
        return JanelaSegura(valor)
    if isinstance(valor, list):
        return [_embrulhar(v) for v in valor]
    return valor


class JanelaSegura:
    """Wrapper do pywinauto sem os métodos que usam teclado e mouse reais, o foco ou a tela do usuário
    (`click_input`, `type_keys`, `set_focus`, `draw_outline`, `capture_as_image`...): sem entrada real,
    eles levantam EntradaRealNecessaria. O resto passa direto; os wrappers que ele devolve (filhos,
    pai...) vêm embrulhados do mesmo jeito. Use os métodos da SessaoApp para clicar e digitar."""

    def __init__(self, wrapper):
        object.__setattr__(self, "_w", wrapper)

    def __getattr__(self, nome: str):
        if nome in _METODOS_REAIS and not entrada_real():
            raise EntradaRealNecessaria(f"{nome} usa teclado/mouse reais, o foco ou a tela do usuário: use os "
                                        f"métodos da SessaoApp (clicar, teclas, digitar, captura)")
        valor = getattr(self._w, nome)
        if callable(valor) and not isinstance(valor, type):
            def chamar(*a, **k):
                return _embrulhar(valor(*a, **k))
            chamar.__name__ = nome
            chamar.__doc__ = getattr(valor, "__doc__", None)
            return chamar
        return _embrulhar(valor)

    def __setattr__(self, nome, valor):
        setattr(self._w, nome, valor)

    def __repr__(self) -> str:
        return f"JanelaSegura({self._w!r})"


# ---------------------------------------------------------------- sessão

def esperar(condicao: Callable[[], Any], timeout: float, passo: float = 0.2):
    """Chama `condicao` até ela devolver algo verdadeiro (devolvido) ou o tempo acabar (devolve o
    último valor, falso)."""
    limite = time.monotonic() + timeout
    while True:
        v = condicao()
        if v or time.monotonic() >= limite:
            return v
        time.sleep(passo)


class SessaoApp:
    """Sessão com o aplicativo aberto sobre um %APPDATA% de teste. Use com `with`.

    Parâmetros além dos do plano (todos opcionais): `estado` (a sprint que recebe a bandeira e o
    registro da restauração; padrão: a sprint em andamento, ou avulso), `argumentos`, `titulo`
    (regex da janela principal), `limpar` (esvazia a pasta de configuração antes; padrão True),
    `preservar` (nomes que ficam quando `limpar`), `fechar_alertas_abertura`, `espera_alertas`,
    `env`, `timeout_fechar` e `espera_trava` (quanto esperar, em s, que outra sessão do agente na
    máquina solte o aplicativo)."""

    def __init__(self, exe: Path, pasta_evidencias: Path, settings_extra: dict | None = None,
                 timeout_abrir: float = 60, *, estado=None, argumentos: Sequence[str] = (),
                 titulo: str = TITULO_PADRAO, limpar: bool = True, preservar: Sequence[str] = (),
                 fechar_alertas_abertura: bool = True, espera_alertas: float = 2.0,
                 env: dict[str, str] | None = None, timeout_fechar: float = 20.0, espera_trava: float = 1800.0):
        self.exe = Path(exe)
        self.pasta_evidencias = Path(pasta_evidencias)
        self.settings_extra = dict(settings_extra or {})
        self.timeout_abrir = timeout_abrir
        self.argumentos = [str(a) for a in argumentos]
        self.titulo = re.compile(titulo)
        self.limpar = limpar
        self.preservar = {p.casefold() for p in preservar}
        self.fechar_alertas_abertura = fechar_alertas_abertura
        self.espera_alertas = espera_alertas
        self.env = env
        self.timeout_fechar = timeout_fechar
        self.espera_trava = espera_trava
        self._posse = None  # trava_appdata.Posse
        self._pediu_trava = False
        self.esperando_trava = False  # esperando outra sessão do agente soltar o aplicativo
        self.maw_estrangeira: dict | None = None  # a do usuário apareceu durante a sessão (e a encerrou)
        self._abrindo = False
        self._vigia: threading.Thread | None = None
        self._estado_dado = estado
        self._registro = None  # Estado da sprint ou _RegistroAvulso
        self._pasta_registro: Path | None = None
        self._backup: Path | None = None
        self._proc: subprocess.Popen | None = None
        self._ps: psutil.Process | None = None
        self._hwnd = 0
        self._trava = threading.RLock()
        self._aberta = False
        self._encerrada = False
        self._frente_anterior = 0
        self._expandido: UIAElementInfo | None = None
        self.alertas_abertura: list[str] = []
        self.fechamentos: list[dict] = []
        self.restauracao: dict | None = None

    # ---------- ciclo de vida

    def __enter__(self) -> "SessaoApp":
        if not desktop_oculto() and not entrada_real():
            raise EntradaRealNecessaria("a MAW só abre no desktop oculto ou na execução noturna")
        with self._trava:
            if self._aberta or self._encerrada or self._pediu_trava:
                raise ErroApp("SessaoApp já usada: crie outra")
            self._pediu_trava = True
        # espera fora de self._trava: um matar() de outra thread cancela a espera
        self.esperando_trava = True
        try:
            posse = trava_appdata.adquirir(self.espera_trava, cancelar=lambda: self._encerrada)
        finally:
            self.esperando_trava = False
        if posse is None:
            if self._encerrada:
                raise ErroApp("sessão encerrada enquanto esperava o aplicativo ficar livre")
            raise AppJaAberta(f"outra sessão do agente está usando a MAW há mais de {self.espera_trava:g} s "
                              "(o ambiente não foi tocado)")
        with self._trava:
            self._posse = posse
            try:
                if self._encerrada:
                    raise ErroApp("sessão encerrada enquanto esperava o aplicativo ficar livre")
                if app_aberta(self.exe):
                    raise AppJaAberta(f"{self.exe.name} já está aberto: feche-o antes (o ambiente não foi tocado)")
                self._preparar_ambiente()
            except BaseException:
                self._soltar_trava()
                raise
            self._aberta = True
            try:
                self._abrir()
            except BaseException:
                self.encerrar(forcar=True, _em_erro=True)
                raise
            self._vigia = threading.Thread(target=self._vigiar_estrangeira, daemon=True, name="vigia-app-do-usuario")
            self._vigia.start()
            return self

    def _soltar_trava(self) -> None:
        posse, self._posse = self._posse, None
        if posse is not None:
            posse.liberar()

    def _vigiar_estrangeira(self) -> None:
        """Olha a cada PASSO_ESTRANGEIRA s se apareceu um MAW_APP.exe fora da nossa árvore (o usuário
        abrindo a MAW: a instância única entregaria a abertura dele à nossa, no desktop oculto, ou ele
        leria a configuração de teste). Se aparecer, a sessão acaba na hora."""
        while not self._encerrada:
            time.sleep(PASSO_ESTRANGEIRA)
            if self._encerrada or self._abrindo or not self.viva():
                continue
            try:
                nossos = {self.pid, *(p.pid for p in self._filhos())}
                outra = outra_instancia(self.exe, nossos)
            except Exception:  # processo sumindo no meio da consulta
                continue
            if outra:
                self._app_do_usuario(outra)
                return

    def _app_do_usuario(self, pid: int) -> None:
        """Encerra a sessão porque o usuário abriu o aplicativo: mata a nossa árvore, dá uns segundos
        para a instância dele terminar (a instância única costuma encerrá-la) e restaura sob a trava
        (se ela continuar aberta, a restauração é adiada)."""
        self.maw_estrangeira = {"pid": int(pid), "quando": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._matar_sem_trava("o usuário abriu o aplicativo durante a sessão")
        esperar(lambda: not psutil.pid_exists(pid), 5.0, 0.25)
        try:
            self.encerrar(forcar=True, _em_erro=True)
        except Exception:  # o registro fica em self.restauracao
            pass

    def __exit__(self, tipo, exc, tb) -> None:
        self.encerrar(forcar=isinstance(exc, AppTravou), _em_erro=exc is not None)

    def _preparar_ambiente(self) -> None:
        from . import estado as est

        e = self._estado_dado if self._estado_dado is not None else est.em_andamento(config.RELATORIOS)
        restaurar_pendencias(e)
        sujas = pendencias()
        if sujas:
            raise AmbienteSujo("restauração pendente da pasta de configuração não concluída ("
                               + ", ".join(str(p) for p in sujas) + "): a sessão não abre sobre ambiente sujo")
        if e is not None:
            self._pasta_registro = Path(e.pasta)
            self._registro = e if self._estado_dado is not None else None  # recarregado na hora de registrar
        else:
            self._pasta_registro = _pasta_avulsa()
            self._registro = _RegistroAvulso(self._pasta_registro)
        pasta = Path(config.APPDATA_MAW)
        self._backup = sandbox.backup_pasta(pasta, config.BACKUPS)
        try:
            sandbox.escrever_json(self._pasta_registro / "appdata-sujo.json",
                                  {"backup": str(self._backup), "desde": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "quem": "gui"})
        except BaseException:
            sandbox.descartar_backup(self._backup)  # nada foi mexido ainda
            self._backup = None
            raise
        try:
            self._montar_pasta_de_teste(pasta)
        except BaseException:
            self._restaurar()
            raise

    def _montar_pasta_de_teste(self, pasta: Path) -> None:
        sandbox.criar_pasta(pasta)
        arquivo = pasta / ARQUIVO_SETTINGS
        if self.limpar:
            for item in pasta.iterdir():
                if item.name.casefold() not in self.preservar:
                    sandbox.remover(item)
            sandbox.escrever_texto(arquivo, settings_xml(self.settings_extra))
        elif self.settings_extra:
            if arquivo.exists():
                texto = _mesclar_settings(arquivo.read_text(encoding="utf-8"), self.settings_extra)
            else:
                texto = settings_xml(self.settings_extra)
            sandbox.escrever_texto(arquivo, texto)

    def _estado_para_registro(self):
        if self._registro is not None:
            return self._registro
        from . import estado as est

        return est.carregar(self._pasta_registro)  # a versão em disco agora: não apaga o que outros gravaram

    def _registro_tolerante(self) -> _RegistroTolerante:
        try:
            return _RegistroTolerante(self._estado_para_registro(), self._pasta_registro or _pasta_avulsa())
        except Exception as ex:  # estado ilegível: restaura do mesmo jeito, sem registrar nele
            return _RegistroTolerante(None, self._pasta_registro or _pasta_avulsa(), f"estado ilegível: {ex}")

    def _restaurar(self) -> dict | None:
        """Restaura primeiro; registrar no estado da sprint é o melhor esforço depois."""
        from . import fases

        if self._backup is None:
            return None
        registro = self._registro_tolerante()
        reg = fases._restaurar_ambiente(registro, self._backup, "gui")
        if registro.erro:
            reg["erro_ao_registrar"] = redacao.redigir(registro.erro)
        self.restauracao = reg
        return reg

    def _motivo_para_adiar(self) -> str | None:
        """Com o processo desta sessão ainda vivo, ou com outra instância do aplicativo aberta (a do
        usuário, que pode ter aberto enquanto a nossa fechava), restaurar agora estragaria a pasta
        de configuração dela (ela grava por cima ao fechar): a restauração fica adiada."""
        if self._backup is None:
            return None
        if self.viva():
            return "o processo da sessão não morreu"
        outra = outra_instancia(self.exe, self.pid)
        if outra:
            return f"outra instância de {self.exe.name} está aberta (pid {outra}; a do usuário?)"
        return None

    def _adiar_restauracao(self, motivo: str) -> dict:
        """Deixa bandeira e backup onde estão (a próxima sessão, ou `sprint iniciar`, restaura com o
        aplicativo fechado) e registra o adiamento."""
        from . import estado as est
        from . import fases

        reg = {"quem": "gui", "backup": str(self._backup), "verificado": False, "backup_apagado": False,
               "adiada": True, "erro": f"restauração do %APPDATA%\\MAW adiada: {motivo}; a bandeira e o backup "
                                       f"ficaram ({self._backup}) para a próxima retomada"}
        registro = self._registro_tolerante()
        registro.registrar_restauracao(reg)
        if isinstance(registro._alvo, est.Estado):
            try:
                fases._acrescentar_limitacoes(registro._alvo, "ambiente", [fases._MOTIVO_ADIADA])
            except Exception:  # a limitação é aviso; o adiamento já está registrado
                pass
        self.restauracao = reg
        return reg

    def encerrar(self, forcar: bool = False, _em_erro: bool = False) -> dict:
        """Fecha (com graça, ou mata se `forcar`/travada), restaura e confere o ambiente. Idempotente
        e segura de chamar de outra thread (ex.: tempo do cenário esgotado)."""
        with self._trava:
            if self._encerrada:
                return {"fechamentos": self.fechamentos, "restauracao": self.restauracao}
            self._encerrada = True
            try:
                if self.viva():
                    if forcar or not self.respondendo():
                        self.derrubar(motivo="forçado" if forcar else "não respondia")
                    else:
                        self.fechar()
            finally:
                try:
                    self._devolver_frente()
                    motivo = self._motivo_para_adiar()
                    reg = self._adiar_restauracao(motivo) if motivo else self._restaurar()
                finally:  # só depois da restauração: a próxima sessão acha o ambiente em ordem
                    self._soltar_trava()
            if reg is not None and not reg.get("verificado") and not _em_erro:
                if reg.get("adiada"):
                    raise AmbienteNaoRestaurado(reg["erro"])
                raise AmbienteNaoRestaurado(f"pasta de configuração não restaurada: {reg.get('erro')}; "
                                            f"backup mantido em {self._backup}")
            return {"fechamentos": self.fechamentos, "restauracao": self.restauracao}

    def matar(self) -> dict:
        """Encerramento forçado da sessão inteira: mata a árvore primeiro, sem esperar a trava interna
        (uma thread presa com ela não segura a morte do processo), depois restaura o ambiente."""
        self._matar_sem_trava("forçado")
        return self.encerrar(forcar=True, _em_erro=True)

    def _matar_sem_trava(self, motivo: str) -> None:
        ps, proc = self._ps, self._proc
        if proc is None or proc.poll() is not None:
            return
        alvos: list[psutil.Process] = []
        if ps is not None:
            try:
                alvos = [ps, *ps.children(recursive=True)]
            except psutil.Error:
                alvos = [ps]
        nomes = []
        for q in alvos:
            try:
                nomes.append(q.name())
                q.kill()
            except psutil.Error:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        if alvos:
            psutil.wait_procs(alvos, timeout=10)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        self.fechamentos.append({"modo": "nao_morreu" if proc.poll() is None else "morta", "motivo": motivo,
                                 "alertas": [], "filhos_mortos": nomes[1:], "codigo": proc.returncode})

    # ---------- processo

    def _abrir(self) -> None:
        env = dict(self.env if self.env is not None else os.environ)
        anterior = GetErrorMode()
        SetErrorMode(anterior | _SEM_DIALOGOS)
        try:
            self._proc = subprocess.Popen([str(self.exe), *self.argumentos], cwd=str(self.exe.parent), env=env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except OSError as ex:
            raise AppNaoAbriu(f"não consegui iniciar {self.exe}: {ex}") from ex
        finally:
            SetErrorMode(anterior)
        try:
            self._ps = psutil.Process(self._proc.pid)
        except psutil.Error:
            self._ps = None
        self._hwnd = 0
        limite = time.monotonic() + self.timeout_abrir
        while time.monotonic() < limite:
            if self._proc.poll() is not None:
                raise AppNaoAbriu(f"{self.exe.name} terminou antes de mostrar a janela principal "
                                  f"(código {self._proc.returncode})")
            h = self._achar_principal()
            if h:
                self._hwnd = h
                break
            time.sleep(0.25)
        else:
            vistas = [f"{_texto(h)!r}" for h in janelas_do_processo(self._proc.pid)]
            cap = None
            if vistas:
                cap = self._capturar_erro("nao-abriu", janelas_do_processo(self._proc.pid)[0])
            raise AppNaoAbriu(f"janela principal ({self.titulo.pattern}) não apareceu em {self.timeout_abrir:g}s; "
                              f"janelas do processo: {', '.join(vistas) or 'nenhuma'}", cap)
        if not esperar(lambda: responde(self._hwnd, 1000), min(30.0, self.timeout_abrir), 0.25):
            raise AppNaoAbriu("a janela principal apareceu mas não responde", self._capturar_erro("nao-responde"))
        if self.fechar_alertas_abertura:
            self._fechar_alertas_de_abertura()

    def _achar_principal(self) -> int:
        if self._proc is None:
            return 0
        candidatas = [h for h in janelas_do_processo(self._proc.pid) if self.titulo.search(_texto(h))]
        return max(candidatas, key=_area) if candidatas else 0

    def _fechar_alertas_de_abertura(self) -> None:
        """Fecha os avisos que aparecem logo depois da janela principal, guardando o texto em
        `alertas_abertura`. Um aviso que não fecha fica aberto (e visível em `alertas()`)."""
        inicio = time.monotonic()
        limite = inicio + self.espera_alertas
        teto = limite + 10.0
        apertados: dict[int, float] = {}
        while time.monotonic() < min(limite, teto):
            achados = self._alertas_detalhados()
            for hwnd, texto, botoes in achados:
                if texto not in self.alertas_abertura:
                    self.alertas_abertura.append(texto)
                if time.monotonic() - apertados.get(hwnd, -99.0) < 2.0:
                    continue
                apertados[hwnd] = time.monotonic()
                for resposta in RESPOSTAS_ABERTURA:
                    alvo = next((b for b in botoes if _casa(b[0], resposta, prefixo=True)), None)
                    if alvo is not None:
                        self._apertar(hwnd, alvo)
                        break
                else:
                    PostMessageW(hwnd, WM_CLOSE, 0, 0)
            if achados:
                limite = max(limite, time.monotonic() + 1.0)  # um aviso pode trazer outro
            time.sleep(0.2)

    def reabrir(self) -> None:
        """Abre o executável de novo no mesmo ambiente de teste (fecha antes se estiver aberto)."""
        with self._trava:
            self._exigir_sessao()
            self._abrindo = True
            try:
                if self.viva():
                    self.fechar()
                if self.viva() or outra_instancia(self.exe, self.pid):
                    raise AppJaAberta(f"não reabro {self.exe.name}: o processo da sessão não morreu ou outra "
                                      "instância abriu enquanto ela estava fechada (a restauração será adiada)")
                self._abrir()
            finally:
                self._abrindo = False

    def fechar(self, timeout: float | None = None) -> dict:
        """WM_CLOSE na janela principal; responde "Descartar" (e equivalentes) à pergunta de salvar;
        mata a árvore se não fechar no prazo. O ambiente de teste continua (use `reabrir`)."""
        with self._trava:
            info: dict = {"modo": "ja_encerrada", "alertas": [], "filhos_mortos": []}
            if not self.viva():
                info["codigo"] = self._proc.returncode if self._proc else None
                self.fechamentos.append(info)
                return info
            filhos = self._filhos()
            try:
                self.fechar_menus()
            except Exception:  # fechar é o que importa; menu que não fechou some com a janela
                pass
            hwnd = self.hwnd
            if hwnd:
                PostMessageW(hwnd, WM_CLOSE, 0, 0)
            limite = time.monotonic() + (timeout if timeout is not None else self.timeout_fechar)
            apertados: dict[int, float] = {}
            while time.monotonic() < limite and self._proc.poll() is None:
                for h, texto, botoes in self._alertas_detalhados():
                    if texto not in info["alertas"]:
                        info["alertas"].append(texto)
                    if time.monotonic() - apertados.get(h, -99.0) < 2.0:
                        continue  # já respondido; a caixa está fechando
                    alvo = None
                    for resposta in RESPOSTAS_FECHAR:
                        alvo = next((b for b in botoes if _casa(b[0], resposta, prefixo=True)), None)
                        if alvo is not None:
                            break
                    if alvo is not None:
                        self._apertar(h, alvo)
                        apertados[h] = time.monotonic()
                time.sleep(0.25)
            if self._proc.poll() is None:
                cap = self._capturar_erro("nao-fechou")
                info["captura" if isinstance(cap, Path) else "sem_captura"] = str(cap) if cap else None
                self._matar_arvore()
                info["modo"] = "nao_morreu" if self.viva() else "morta"
            else:
                info["modo"] = "graciosa"
            info["codigo"] = self._proc.returncode
            info["filhos_mortos"] = self._matar_sobras(filhos)
            self.fechamentos.append(info)
            return info

    def derrubar(self, motivo: str = "derrubada") -> dict:
        """Mata a árvore de processos agora (simula queda). O ambiente de teste continua."""
        with self._trava:
            info: dict = {"modo": "morta", "motivo": motivo, "alertas": [], "filhos_mortos": []}
            if self.viva():
                if motivo != "derrubada":
                    cap = self._capturar_erro("antes-de-matar")
                    info["captura" if isinstance(cap, Path) else "sem_captura"] = str(cap) if cap else None
                filhos = self._filhos()
                self._matar_arvore()
                info["filhos_mortos"] = self._matar_sobras(filhos)
                if self.viva():
                    info["modo"] = "nao_morreu"
            info["codigo"] = self._proc.returncode if self._proc else None
            self.fechamentos.append(info)
            return info

    def _filhos(self) -> list[psutil.Process]:
        try:
            return self._ps.children(recursive=True) if self._ps else []
        except psutil.Error:
            return []

    def _matar_arvore(self) -> None:
        alvos = [p for p in [self._ps, *self._filhos()] if p is not None]
        for p in alvos:
            try:
                p.kill()
            except psutil.Error:
                pass
        if self._ps is None and self._proc is not None and self._proc.poll() is None:
            try:  # sem o psutil do processo: mata pelo Popen
                self._proc.kill()
            except OSError:
                pass
        psutil.wait_procs(alvos, timeout=10)
        if self._proc is not None:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _matar_sobras(filhos: list[psutil.Process]) -> list[str]:
        mortos = []
        for p in filhos:
            try:
                if p.is_running():
                    nome = p.name()
                    p.kill()
                    mortos.append(nome)
            except psutil.Error:
                pass
        if mortos:
            psutil.wait_procs(filhos, timeout=5)
        return mortos

    # ---------- estado do aplicativo

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def hwnd(self) -> int:
        """Janela principal (achada de novo se a antiga sumiu, ex.: a JUCE recriou a janela)."""
        if self._hwnd and IsWindow(self._hwnd) and _pid_de(self._hwnd) == self.pid:
            return self._hwnd
        self._hwnd = self._achar_principal()
        return self._hwnd

    def janela(self) -> "JanelaSegura":
        """A janela principal como wrapper UIA do pywinauto, sem os métodos de entrada real (JanelaSegura)."""
        self._exigir_viva()
        return JanelaSegura(UIAWrapper(_elemento(self.hwnd)))

    def viva(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def respondendo(self) -> bool:
        return self.viva() and bool(self.hwnd) and responde(self.hwnd)

    def _exigir_sessao(self) -> None:
        if not self._aberta or self._encerrada:
            raise ErroApp("sessão não está aberta")

    def _exigir_viva(self) -> None:
        if self.maw_estrangeira:
            raise AppDoUsuarioAberto(f"o usuário abriu {self.exe.name} durante o teste (pid "
                                     f"{self.maw_estrangeira['pid']}): a sessão foi encerrada e o ambiente restaurado")
        self._exigir_sessao()
        if not self.viva():
            raise AppTravou(f"{self.exe.name} encerrou sozinho (código {self._proc.returncode if self._proc else '?'})",
                            motivo="encerrou")
        if not self.hwnd:
            raise AppTravou("a janela principal sumiu", self._capturar_erro("sem-janela"), motivo="encerrou")
        if not responde(self.hwnd):
            raise AppTravou(f"{self.exe.name} não responde", self._capturar_erro("nao-responde"))

    def esperar(self, condicao: Callable[[], Any], timeout: float, passo: float = 0.2):
        """Como `gui.esperar`, mas desiste com AppTravou se a janela ficar travada por LIMITE_TRAVADA s."""
        limite = time.monotonic() + timeout
        travada_desde = None
        while True:
            v = condicao()
            if v or time.monotonic() >= limite:
                return v
            h = self._hwnd
            if self.viva() and h and IsHungAppWindow(h):
                travada_desde = travada_desde or time.monotonic()
                if time.monotonic() - travada_desde >= LIMITE_TRAVADA:
                    raise AppTravou(f"{self.exe.name} parou de responder por {LIMITE_TRAVADA:g}s",
                                    self._capturar_erro("travada"))
            else:
                travada_desde = None
            time.sleep(passo)

    # ---------- janelas

    def _janelas_hwnd(self) -> list[int]:
        return janelas_do_processo(self.pid) if self.viva() else []

    def janelas(self) -> list[str]:
        """Títulos das janelas visíveis do processo (de cima para baixo)."""
        return [_texto(h) for h in self._janelas_hwnd()]

    def janela_por_titulo(self, padrao: str, timeout: float = 5.0) -> int:
        """hwnd da janela do processo cujo título casa com a regex (0 se não apareceu)."""
        rx = re.compile(padrao)
        return self.esperar(lambda: next((h for h in self._janelas_hwnd() if rx.search(_texto(h))), 0),
                            timeout) or 0

    def fechar_janela(self, padrao: str) -> bool:
        h = self.janela_por_titulo(padrao, 0)
        if h:
            PostMessageW(h, WM_CLOSE, 0, 0)
        return bool(h)

    def _hwnd_de(self, janela: str | int | None) -> int:
        if janela is None:
            return self.hwnd
        if isinstance(janela, int):
            if not IsWindow(janela) or _pid_de(janela) != self.pid:
                raise ControleNaoEncontrado(f"a janela {janela:#x} não é deste aplicativo (pid {self.pid})")
            return janela
        h = self.janela_por_titulo(janela, 0)
        if not h:
            raise ControleNaoEncontrado(f"janela {janela!r} não encontrada; abertas: {self.janelas()}")
        return h

    # ---------- captura

    def captura(self, nome: str, janela: str | int | None = None) -> Path:
        """PNG da janela (padrão: a principal) em `pasta_evidencias`, mesmo coberta por outras."""
        hwnd = self._hwnd_de(janela)
        if not hwnd:
            raise ErroApp("sem janela para capturar")
        terminou, img = _com_timeout(lambda: capturar_janela(hwnd), 8.0 if responde(hwnd, 500) else 3.0)
        if not terminou:  # janela travada não pinta para o PrintWindow
            if desktop_oculto():
                raise AppTravou("a janela não atende ao PrintWindow (travada) e está no desktop oculto, "
                                "coberta para a cópia da tela (que lá não existe)", motivo="nao_responde")
            if not _visivel_na_tela(hwnd):
                raise AppTravou("a janela não atende ao PrintWindow (travada) e está coberta por outras "
                                "(copiar a tela mostraria as outras janelas)", motivo="nao_responde")
            img = capturar_tela_da_janela(hwnd)
        for _ in range(2):  # a primeira pintura pode não ter chegado: tenta de novo antes de desistir
            if fracao_cor_dominante(img) < LIMITE_CAPTURA_VAZIA:
                break
            time.sleep(0.5)
            terminou, nova = _com_timeout(lambda: capturar_janela(hwnd), 8.0)
            if terminou and nova is not None:
                img = nova
        else:
            if fracao_cor_dominante(img) >= LIMITE_CAPTURA_VAZIA:
                raise CapturaVazia(f"a captura '{nome}' veio de uma cor só (≥{LIMITE_CAPTURA_VAZIA:.0%}): sessão "
                                   "bloqueada ou desktop sem pintura — a tela não pode ser julgada por ela")
        base = re.sub(r"[^\w.-]+", "_", nome).strip("_") or "captura"
        destino = self.pasta_evidencias / f"{base}.png"
        n = 2
        while destino.exists():
            destino = self.pasta_evidencias / f"{base}-{n}.png"
            n += 1
        return sandbox.escrever_bytes(destino, _png(img))

    def _capturar_erro(self, rotulo: str, hwnd: int | None = None) -> Path | str:
        """Captura para anexar a um erro; sem imagem, devolve o motivo (texto)."""
        try:
            h = hwnd or self._hwnd
            if not h or not IsWindow(h):
                return "a janela não existe mais"
            return self.captura(f"erro-{rotulo}-{time.strftime('%H%M%S')}", h)
        except Exception as ex:  # a captura é bônus: o erro original é o que importa
            return str(ex)

    # ---------- botões e menus

    def botoes(self, nome: str | re.Pattern | None = None, janela: str | int | None = None) -> list[Controle]:
        """Controles acionáveis (Button, CheckBox, MenuItem...) da janela; com `nome`, só os que casam."""
        self._exigir_viva()
        infos = _descendentes(self._hwnd_de(janela), TIPOS_BOTAO)
        return [Controle(i, self) for i in infos if nome is None or _casa(i.name, nome)]

    def botao(self, nome: str | re.Pattern, janela: str | int | None = None, indice: int = 0) -> Controle:
        achados = self.botoes(nome, janela)
        if len(achados) <= indice:
            nomes = sorted({c.nome for c in self.botoes(None, janela) if c.nome})
            raise ControleNaoEncontrado(f"botão {nome!r} não encontrado; há: {nomes}")
        return achados[indice]

    def controles(self, tipo: str | None = None, janela: str | int | None = None) -> list[Controle]:
        """Todos os elementos UIA da janela (ou só de um tipo: 'Slider', 'ComboBox', 'Edit'...)."""
        self._exigir_viva()
        infos = _descendentes(self._hwnd_de(janela), None if tipo is None else (tipo,))
        return [Controle(i, self) for i in infos]

    def _popups(self) -> list[int]:
        """Janelas de menu abertas (com itens MenuItem), de cima para baixo."""
        out = []
        principal = self.hwnd
        for h in self._janelas_hwnd():
            if h == principal or _classe(h) == "#32770":
                continue
            try:
                info = _elemento(h)
                if info.control_type == "Menu" or info.descendants(control_type="MenuItem"):
                    out.append(h)
            except Exception:  # janela sumindo no meio da consulta
                continue
        return out

    @staticmethod
    def _itens_diretos(popup: int) -> list[UIAElementInfo]:
        """Itens do próprio menu: os de submenus (que alguns toolkits expõem dentro do item pai
        mesmo fechados) ficam de fora."""
        raiz = _elemento(popup)
        out = []
        for item in raiz.descendants(control_type="MenuItem"):
            pai, aninhado = item.parent, False
            for _ in range(32):
                if pai is None or pai == raiz:
                    break
                if pai.control_type == "MenuItem":
                    aninhado = True
                    break
                pai = pai.parent
            if not aninhado:
                out.append(item)
        return out

    def _itens_abertos(self) -> list[UIAElementInfo]:
        """Itens do menu aberto mais recente. A JUCE põe os itens de cada (sub)menu na própria janela;
        o WinForms deixa a janela do submenu vazia e pendura os itens no item pai, expandido."""
        pops = self._popups()
        if not pops:
            return []
        itens = self._itens_diretos(pops[0])
        if itens:
            return itens
        mais_fundo: list[UIAElementInfo] = []
        for p in pops[1:]:
            for item in _elemento(p).descendants(control_type="MenuItem"):
                ec = _padrao(item, "ExpandCollapse")
                if ec is not None and ec.CurrentExpandCollapseState == 1:  # Expanded
                    filhos = item.children(control_type="MenuItem")
                    if filhos:
                        mais_fundo = filhos
        if not mais_fundo and self._expandido is not None:  # o proxy do MSAA não diz qual expandiu
            try:
                mais_fundo = self._expandido.children(control_type="MenuItem")
            except Exception:  # item já não existe
                mais_fundo = []
        return mais_fundo

    def itens_menu(self, popup: int | None = None) -> list[str]:
        """Nomes dos itens do menu aberto mais recente (ou do `popup` dado)."""
        itens = self._itens_diretos(popup) if popup else self._itens_abertos()
        return [i.name for i in itens]

    def _item(self, nome: str | re.Pattern) -> Controle:
        itens = self._itens_abertos()
        for prefixo in (False, True):
            for i in itens:
                if _casa(i.name, nome, prefixo=prefixo):
                    if not entrada_real() and item_proibido(i.name):
                        if prefixo:
                            continue  # o casamento por prefixo nunca cai num item proibido
                        _recusar_proibido(i.name)
                    return Controle(i, self)
        raise ControleNaoEncontrado(f"item {nome!r} não está no menu; há: {[i.name for i in itens]}")

    def _novo_popup(self, antes: set[int], timeout: float) -> int:
        return self.esperar(lambda: next((h for h in self._popups() if h not in antes), 0), timeout, 0.05) or 0

    def abrir_menu(self, *caminho: str | re.Pattern, timeout: float = 5.0,
                   primeiro_plano: bool | None = None) -> list[str]:
        """Clica no botão `caminho[0]`, abre os submenus do resto e devolve os itens do último menu
        (que fica aberto — feche com `fechar_menus`). Menus da JUCE se fecham sozinhos quando o
        aplicativo não está em primeiro plano. No desktop oculto ficam abertos sem trazer nada para a
        frente (lá não há primeiro plano); fora dele, sem entrada real, isso vira EntradaRealNecessaria."""
        if not caminho:
            raise ValueError("abrir_menu precisa de ao menos o botão")
        self._exigir_viva()
        if not desktop_oculto() and (entrada_real() if primeiro_plano is None else primeiro_plano):
            self.trazer_para_frente()
        self._expandido = None
        antes = set(self._popups())
        self.botao(caminho[0]).invocar()
        popup = self._novo_popup(antes, timeout)
        if not popup:
            raise self._menu_sumiu(caminho[0], timeout)
        for nome in caminho[1:]:
            item = self._item(nome)
            antes = set(self._popups())
            self._expandido = item.info
            item.expandir()
            popup = self._novo_popup(antes, timeout)
            if not popup:
                raise self._menu_sumiu(nome, timeout)
        itens = self.itens_menu()
        if not IsWindow(popup) or not IsWindowVisible(popup):
            raise self._menu_sumiu(caminho[-1], timeout)
        return itens

    def _menu_sumiu(self, nome, timeout: float) -> ErroApp:
        cap = self._capturar_erro("menu")
        frente = GetForegroundWindow()
        if frente and _pid_de(frente) != self.pid:
            return EntradaRealNecessaria(
                f"o menu de {nome!r} não ficou aberto: o aplicativo não está em primeiro plano (menus da JUCE se "
                f"fecham sozinhos assim); rode no desktop oculto (python -m maw_agent.desktop_oculto -- ...), "
                f"onde eles ficam abertos, ou com {VARIAVEL_ENTRADA_REAL}=1 à noite", cap)
        return ControleNaoEncontrado(f"nenhum menu abriu em {timeout:g}s depois de {nome!r}", cap)

    def menu(self, *caminho: str | re.Pattern, timeout: float = 5.0, primeiro_plano: bool | None = None) -> None:
        """Aciona o item final de `caminho` (ex.: menu("Arquivo", "Novo"))."""
        if len(caminho) < 2:
            raise ValueError("menu precisa do botão e de ao menos um item (use botao().invocar() para só clicar)")
        self.abrir_menu(*caminho[:-1], timeout=timeout, primeiro_plano=primeiro_plano)
        if not self._popups():
            raise self._menu_sumiu(caminho[-2], timeout)
        item = self._item(caminho[-1])
        if not item.habilitado:
            raise ControleNaoEncontrado(f"item {caminho[-1]!r} está desabilitado")
        item.invocar()

    def fechar_menus(self, tentativas: int = 5) -> bool:
        """Esc nos menus abertos até não sobrar nenhum. True se fecharam."""
        esc = interpretar_teclas("esc")
        self._expandido = None
        for _ in range(tentativas):
            pops = self._popups()
            if not pops:
                return True
            _postar_tecla(pops[0], esc)
            esperar(lambda: pops[0] not in self._popups(), 1.0, 0.05)
        if self._popups() and self.hwnd:
            _postar_tecla(self.hwnd, esc)
            esperar(lambda: not self._popups(), 1.0, 0.05)
        return not self._popups()

    # ---------- avisos (caixas de mensagem)

    def _modais(self) -> list[int]:
        """Avisos e diálogos abertos do processo (caixa Win32, TaskDialog, diálogo de arquivo, diálogo
        UIA como a AlertWindow da JUCE), fora a janela principal."""
        principal = self._hwnd
        out = []
        for h in self._janelas_hwnd():
            if h == principal:
                continue
            try:
                if _classe(h) == "#32770" or _eh_dialogo_uia(_elemento(h)):
                    out.append(h)
            except Exception:  # janela fechando
                continue
        return out

    def _recusar_se_modal(self, hwnd: int) -> None:
        """Com um aviso/diálogo aberto, entrada numa janela de trás faz o Windows (ou a JUCE, com
        MessageBeep) tocar o som de alerta: sem MAW_AGENTE_SOM=1, AlertaAberto."""
        if som_liberado():
            return
        modais = self._modais()
        if not modais:
            return
        raiz = int(GetAncestor(hwnd, GA_ROOT) or hwnd) if hwnd else 0
        if raiz in modais:
            return
        titulos = ", ".join(repr(_texto(h)) for h in modais)
        raise AlertaAberto(f"há aviso/diálogo aberto ({titulos}): mexer na janela de trás faz tocar o som de alerta; "
                           f"responda o aviso antes (responder_alerta/dialogo_arquivo) ou rode com {VARIAVEL_SOM}=1")

    def _eh_dialogo_arquivo(self, hwnd: int) -> bool:
        """Diálogo de abrir/salvar do Windows (a caixa de confirmação dele, TaskDialog, não conta)."""
        if _classe(hwnd) != "#32770":
            return False
        classes = {_classe(f) for f in _filhas(hwnd)}
        return bool(classes & {"DUIViewWndClassName", "SHELLDLL_DefView", "ComboBoxEx32"})

    def _alertas_detalhados(self) -> list[tuple[int, str, list[tuple[str, Any]]]]:
        """[(hwnd, texto, [(nome do botão, alvo)])] das caixas de aviso abertas (menos diálogos de arquivo)."""
        out = []
        principal = self._hwnd
        for h in self._janelas_hwnd():
            if h == principal:
                continue
            try:
                botoes = []
                if _classe(h) == "#32770":
                    if self._eh_dialogo_arquivo(h):
                        continue
                    textos = [_texto(h)]
                    for f in _filhas(h):
                        c = _classe(f)
                        if c == "Static" and IsWindowVisible(f):
                            textos.append(_texto(f))
                        elif c == "Button" and IsWindowVisible(f):
                            botoes.append((_texto(f).replace("&", ""), ("win32", f)))
                if _classe(h) == "#32770" and (len(textos) == 1 or not botoes):
                    # TaskDialog: a mensagem (e às vezes os botões) só aparecem pelo UIA
                    info = _elemento(h)
                    textos += [d.name for d in info.descendants(control_type="Text")]
                    if not botoes:
                        botoes = [(b.name, ("uia", b)) for b in info.descendants(control_type="Button")]
                elif _classe(h) != "#32770":
                    info = _elemento(h)
                    if not _eh_dialogo_uia(info):
                        continue
                    textos = [_texto(h)] + [d.name for d in info.descendants(control_type="Text")]
                    botoes = [(b.name, ("uia", b)) for b in info.descendants(control_type="Button")]
            except Exception:  # janela fechando no meio da leitura
                continue
            vistos: list[str] = []
            for t in textos:
                t = (t or "").strip()
                if t and t not in vistos:
                    vistos.append(t)
            out.append((h, "\n".join(vistos), botoes))
        return out

    def alertas(self) -> list[str]:
        """Textos das caixas de aviso abertas (título e mensagem)."""
        if not self.viva():
            return []
        return [t for _, t, _ in self._alertas_detalhados()]

    def _apertar(self, dialogo: int, botao: tuple[str, Any]) -> None:
        tipo, alvo = botao[1]
        if tipo == "win32":
            ident = GetDlgCtrlID(alvo)
            if ident:  # caixa de mensagem: WM_COMMAND funciona mesmo com o diálogo inativo
                PostMessageW(dialogo, WM_COMMAND, ident & 0xFFFF, alvo)  # BN_CLICKED = 0 na palavra alta
            else:  # TaskDialog: botões sem identificador
                PostMessageW(alvo, BM_CLICK, 0, 0)
        else:
            Controle(alvo).invocar()

    def responder_alerta(self, botao: str | re.Pattern, timeout: float = 5.0) -> str:
        """Aperta o botão (nome igual, ou começando com `botao`) da caixa de aviso aberta; devolve o
        texto dela."""
        self._exigir_viva()

        def achar():
            for prefixo in (False, True):
                for h, texto, botoes in self._alertas_detalhados():
                    for b in botoes:
                        if _casa(b[0], botao, prefixo=prefixo):
                            return h, texto, b
            return None

        achado = self.esperar(achar, timeout, 0.1)
        if not achado:
            vistos = [(t, [b[0] for b in bs]) for _, t, bs in self._alertas_detalhados()]
            raise ControleNaoEncontrado(f"nenhum aviso com o botão {botao!r}; avisos abertos: {vistos}",
                                        self._capturar_erro("alerta"))
        h, texto, b = achado
        self._apertar(h, b)
        return texto

    # ---------- diálogo de arquivo (nativo do Windows)

    def dialogo_arquivo(self, caminho: Path, salvar: bool, timeout: float = 15.0) -> None:
        """Preenche o nome no diálogo de abrir/salvar aberto pelo aplicativo e confirma (Sim na
        pergunta de substituir, ao salvar). A pergunta de substituir toca som: sem MAW_AGENTE_SOM=1, um
        destino que já existe é apagado antes (se estiver em work/) ou recusado (SomNecessario)."""
        self._exigir_viva()
        destino = Path(caminho)
        if salvar and destino.exists() and not som_liberado():
            if _dentro(destino, Path(config.WORK)):
                sandbox.remover(destino)
            else:
                raise SomNecessario(f"salvar por cima de {destino} abre a pergunta 'substituir?', que toca som: "
                                    f"use um destino novo ou rode com {VARIAVEL_SOM}=1")
        dlg = self.esperar(lambda: next((h for h in self._janelas_hwnd() if self._eh_dialogo_arquivo(h)), 0), timeout)
        if not dlg:
            raise ControleNaoEncontrado(f"nenhum diálogo de arquivo abriu em {timeout:g}s; janelas: {self.janelas()}",
                                        self._capturar_erro("sem-dialogo"))
        campo = self._campo_nome_arquivo(dlg)
        if campo is None:
            raise ControleNaoEncontrado("campo do nome do arquivo não encontrado no diálogo",
                                        self._capturar_erro("dialogo"))
        campo.definir(str(Path(caminho)))
        ok = next((f for f in _filhas(dlg) if GetDlgCtrlID(f) == 1 and _classe(f) == "Button"), 0)
        PostMessageW(dlg, WM_COMMAND, 1, ok)  # IDOK, BN_CLICKED
        fim = time.monotonic() + timeout
        while time.monotonic() < fim:
            if not IsWindow(dlg) or not IsWindowVisible(dlg):
                return
            for h, _texto_alerta, botoes in self._alertas_detalhados():
                dono = GetWindow(h, GW_OWNER)
                if dono == dlg:
                    alvo = None
                    if salvar:
                        alvo = next((b for b in botoes if _casa(b[0], "Sim") or _casa(b[0], "Yes")), None)
                    if alvo is None:
                        raise ControleNaoEncontrado(f"o diálogo de arquivo respondeu com um aviso: {_texto_alerta!r}",
                                                    self._capturar_erro("dialogo-aviso"))
                    self._apertar(h, alvo)
            time.sleep(0.2)
        raise ControleNaoEncontrado("o diálogo de arquivo não fechou depois de confirmar",
                                    self._capturar_erro("dialogo-aberto"))

    def _campo_nome_arquivo(self, dlg: int) -> Controle | None:
        edits = _elemento(dlg).descendants(control_type="Edit")
        for ident in ("1001", "1148"):
            for e in edits:
                if e.automation_id == ident:
                    return Controle(e, self)
        return Controle(edits[0], self) if edits else None

    # ---------- teclado e mouse

    def _alvo_teclado(self, hwnd: int) -> int:
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if GetGUIThreadInfo(_thread_de(hwnd), ctypes.byref(info)) and info.hwndFocus:
            f = int(info.hwndFocus)
            if f == hwnd or IsChild(hwnd, f):
                return f
        return hwnd

    def teclas(self, combinacao: str, janela: str | int | None = None) -> None:
        """Uma combinação ("ctrl+z", "k", "space", "f5"). Com MAW_AGENTE_ENTRADA_REAL=1: SendInput com a
        janela à frente. Sem: PostMessage; os modificadores vão só no estado de teclado da thread da
        janela (AttachThreadInput + SetKeyboardState), o que serve a quem os lê por GetKeyState. A JUCE
        lê do teclado físico: numa janela dela, tecla com modificador levanta EntradaRealNecessaria."""
        t = interpretar_teclas(combinacao)
        self._exigir_viva()
        hwnd = self._hwnd_de(janela)
        self._recusar_se_modal(hwnd)
        if entrada_real():
            self._frente_ou_erro(hwnd)
            _enviar_tecla_real(t)
            return
        alvo = self._alvo_teclado(hwnd)
        if t.modificadores:
            _recusar_modificador_juce(alvo, repr(combinacao))
            base = Tecla((), t.vk, t.caractere, t.precisa_shift)
            _com_modificadores(alvo, t.modificadores, lambda: _postar_tecla(alvo, base), 2, self.pid)
            return
        _postar_tecla(alvo, t)

    def digitar(self, texto: str, janela: str | int | None = None) -> None:
        """Texto como caracteres (WM_CHAR; SendInput Unicode com entrada real)."""
        self._exigir_viva()
        hwnd = self._hwnd_de(janela)
        self._recusar_se_modal(hwnd)
        if entrada_real():
            self._frente_ou_erro(hwnd)
            for ch in texto:
                _enviar([_tecla_input(0, False, ord(ch)), _tecla_input(0, True, ord(ch))])
            return
        alvo = self._alvo_teclado(hwnd)
        for ch in texto:
            PostMessageW(alvo, WM_CHAR, ord(ch), 1)

    def _frente_ou_erro(self, hwnd: int) -> None:
        if not _trazer_para_frente(hwnd, com_alt=True):
            raise ErroApp("não consegui trazer a janela para a frente para a entrada real",
                          self._capturar_erro("sem-frente"))

    def trazer_para_frente(self) -> bool:
        """Põe a janela principal em primeiro plano (lembra a janela que estava na frente, devolvida no
        fim da sessão ou por `devolver_primeiro_plano`). Só com entrada real (à noite). No desktop
        oculto não há primeiro plano (GetForegroundWindow é NULL e a JUCE já se considera à frente):
        não faz nada e devolve True. Fora dos dois, mudaria o foco do usuário: EntradaRealNecessaria."""
        self._exigir_viva()
        if desktop_oculto():
            return True
        if not entrada_real():
            raise EntradaRealNecessaria(
                "trazer o aplicativo para a frente muda o foco do usuário: rode no desktop oculto "
                f"(python -m maw_agent.desktop_oculto -- ...) ou com {VARIAVEL_ENTRADA_REAL}=1 à noite")
        frente = GetForegroundWindow()
        if frente and _pid_de(frente) != self.pid and not self._frente_anterior:
            self._frente_anterior = int(frente)
        return _trazer_para_frente(self.hwnd, com_alt=entrada_real())

    def devolver_primeiro_plano(self) -> None:
        self._devolver_frente()

    def _devolver_frente(self) -> None:
        """Devolve o primeiro plano à janela que estava na frente, só se o aplicativo ainda está na
        frente (se o foco já foi para outra janela, fica onde está)."""
        h, self._frente_anterior = self._frente_anterior, 0
        if not h or not IsWindow(h) or desktop_oculto():
            return
        frente = GetForegroundWindow()
        if frente and self.pid and _pid_de(frente) == self.pid:
            _trazer_para_frente(h)

    @staticmethod
    def _alvo_mouse(hwnd: int, x: int, y: int) -> tuple[int, int, int]:
        """A filha mais funda sob o ponto de cliente (x, y), com o ponto nas coordenadas dela."""
        alvo, px, py = hwnd, x, y
        for _ in range(16):
            filha = ChildWindowFromPointEx(alvo, wt.POINT(px, py), CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT)
            if not filha or int(filha) == alvo:
                break
            pt = wt.POINT(px, py)
            MapWindowPoints(alvo, filha, ctypes.byref(pt), 1)
            alvo, px, py = int(filha), pt.x, pt.y
        return alvo, px, py

    @staticmethod
    def _lp(x: int, y: int) -> int:
        return ((y & 0xFFFF) << 16) | (x & 0xFFFF)

    def clicar(self, x: int, y: int, botao: str = "esquerdo", duplo: bool = False,
               janela: str | int | None = None) -> None:
        """Clique em coordenadas de cliente físicas da janela (padrão: a principal)."""
        if botao not in _BOTOES:
            raise ValueError(f"botão do mouse desconhecido: {botao!r} (use {', '.join(_BOTOES)})")
        self._exigir_viva()
        hwnd = self._hwnd_de(janela)
        self._recusar_se_modal(hwnd)
        if entrada_real():
            pt = wt.POINT(x, y)
            ClientToScreen(hwnd, ctypes.byref(pt))
            self._frente_ou_erro(hwnd)
            self._clique_real((pt.x, pt.y), botao, duplo)
            return
        desce, sobe, dbl, mk, _, _ = _BOTOES[botao]
        alvo, cx, cy = self._alvo_mouse(hwnd, x, y)
        lp = self._lp(cx, cy)
        duplo_nativo = bool(GetClassLongPtrW(alvo, GCL_STYLE) & CS_DBLCLKS)
        PostMessageW(alvo, WM_MOUSEMOVE, 0, lp)
        PostMessageW(alvo, desce, mk, lp)
        PostMessageW(alvo, sobe, 0, lp)
        if duplo:
            time.sleep(0.03)
            PostMessageW(alvo, dbl if duplo_nativo else desce, mk, lp)
            PostMessageW(alvo, sobe, 0, lp)

    def _clicar_tela(self, ponto: tuple[int, int], botao: str, duplo: bool) -> None:
        """Clique num ponto de tela: vira coordenada de cliente da janela de topo do processo que o contém."""
        self._exigir_viva()
        for h in self._janelas_hwnd():
            l, t, r, b = _retangulo(h)
            if l <= ponto[0] < r and t <= ponto[1] < b:
                pt = wt.POINT(*ponto)
                ScreenToClient(h, ctypes.byref(pt))
                self.clicar(pt.x, pt.y, botao, duplo, janela=h)
                return
        raise ControleNaoEncontrado(f"nenhuma janela do processo contém o ponto {ponto}")

    def _clique_real(self, ponto: tuple[int, int], botao: str, duplo: bool) -> None:
        _, _, _, _, desce, sobe = _BOTOES[botao]
        SetCursorPos(*ponto)
        time.sleep(0.02)
        for _ in range(2 if duplo else 1):
            _enviar([_mouse_input(desce), _mouse_input(sobe)])
            time.sleep(0.05)

    def arrastar(self, de: tuple[int, int], para: tuple[int, int], modificadores: Sequence[str] = (),
                 botao: str = "esquerdo", passos: int = 12, janela: str | int | None = None) -> None:
        """Arrasto em coordenadas de cliente físicas. Modificadores sem entrada real: só no estado de
        teclado da thread da janela (como em `teclas`; numa janela da JUCE, EntradaRealNecessaria)."""
        if botao not in _BOTOES:
            raise ValueError(f"botão do mouse desconhecido: {botao!r}")
        mods = [MODIFICADORES.get(m.lower()) for m in modificadores]
        if None in mods:
            raise ValueError(f"modificador desconhecido em {modificadores!r}")
        self._exigir_viva()
        hwnd = self._hwnd_de(janela)
        self._recusar_se_modal(hwnd)
        desce, sobe, _, mk, in_desce, in_sobe = _BOTOES[botao]
        pontos = [(round(de[0] + (para[0] - de[0]) * i / passos), round(de[1] + (para[1] - de[1]) * i / passos))
                  for i in range(1, passos + 1)]
        if entrada_real():
            self._frente_ou_erro(hwnd)

            def tela(p):
                pt = wt.POINT(*p)
                ClientToScreen(hwnd, ctypes.byref(pt))
                return pt.x, pt.y

            _enviar([_tecla_input(m, False) for m in mods]) if mods else None
            try:
                SetCursorPos(*tela(de))
                time.sleep(0.03)
                _enviar([_mouse_input(in_desce)])
                for p in pontos:
                    time.sleep(0.015)
                    SetCursorPos(*tela(p))
                time.sleep(0.03)
                _enviar([_mouse_input(in_sobe)])
            finally:
                if mods:
                    _enviar([_tecla_input(m, True) for m in reversed(mods)])
            return
        alvo, cx, cy = self._alvo_mouse(hwnd, *de)
        dx, dy = cx - de[0], cy - de[1]  # mesmo deslocamento para os pontos seguintes
        mk_mods = sum(_MK_MODIFICADOR.get(m, 0) for m in mods)

        def postar() -> None:
            PostMessageW(alvo, WM_MOUSEMOVE, mk_mods, self._lp(cx, cy))
            PostMessageW(alvo, desce, mk | mk_mods, self._lp(cx, cy))
            for p in pontos:
                time.sleep(0.015)
                PostMessageW(alvo, WM_MOUSEMOVE, mk | mk_mods, self._lp(p[0] + dx, p[1] + dy))
            PostMessageW(alvo, sobe, mk_mods, self._lp(para[0] + dx, para[1] + dy))

        if mods:
            _recusar_modificador_juce(alvo, f"arrastar com {list(modificadores)}")
            _com_modificadores(alvo, mods, postar, len(pontos) + 3, self.pid)
        else:
            postar()
