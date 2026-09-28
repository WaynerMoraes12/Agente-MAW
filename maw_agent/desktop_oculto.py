"""Desktop oculto: roda processos num desktop extra da estação de janelas (`CreateDesktopW`) que nunca
aparece na tela. Todas as janelas deles (e dos filhos, que herdam o desktop) nascem lá: nada aparece
no desktop do usuário, o foco dele não muda e a entrada real (teclado e mouse) não chega lá.

Medido na janela de teste (tests/app_teste_gui.ps1), a partir de uma thread no desktop oculto:
- `GetForegroundWindow()` devolve NULL e `SetForegroundWindow` falha (o primeiro plano do usuário
  não muda); `GetCursorPos` falha (acesso negado) e `GetAsyncKeyState` devolve 0 (o teclado e o
  mouse reais do usuário não chegam lá);
- `PrintWindow` (com e sem `PW_RENDERFULLCONTENT`) devolve a janela desenhada; copiar a tela
  (`GetDC(NULL)` + `BitBlt`) falha;
- UI Automation e PostMessage funcionam; menus ficam abertos.

Cada `executar` cria o processo com `lpDesktop` apontando para o desktop oculto (conferido antes),
dentro de um job object (a árvore inteira morre junto: no fim, no tempo esgotado ou se o agente
cair), com `CREATE_NO_WINDOW` (sem console visível) e com a saída (stdout+stderr) redigida linha a
linha no log. Um vigia confere o desktop do usuário ("Default") a cada 100 ms: qualquer janela de um
processo do job lá mata o job na hora e vira `JanelaNoDesktopDoUsuario`.

Uso: `python -m maw_agent.desktop_oculto [--nome N] [--timeout S] [--log ARQ] -- <argv...>`.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path
from typing import Callable, Sequence

from . import config, redacao, sandbox

VARIAVEL = "MAW_AGENTE_DESKTOP_OCULTO"
NOME_PADRAO = "AgenteMAW"
DESKTOP_DO_USUARIO = "Default"
CODIGO_ESTOUROU = 124  # código de saída dado à árvore morta por tempo esgotado
CODIGO_VIOLACAO = 125  # código dado à árvore morta porque abriu janela no desktop do usuário
PASSO_VIGIA = 0.1

# Restrições de interface do job: os processos não criam nem trocam de desktop, não desligam o
# Windows, não mudam parâmetros do sistema nem a tela, e não leem nem escrevem a área de
# transferência do usuário.
RESTRICOES_UI = {"readclipboard": 0x2, "writeclipboard": 0x4, "systemparameters": 0x8,
                 "displaysettings": 0x10, "desktop": 0x40, "exitwindows": 0x80}
# Janelas de programas que um processo do desktop oculto pode acordar fora dele (Explorer, navegador,
# app da Store): uma nova no desktop do usuário durante a execução vira "possível fuga" (nunca é fechada:
# pode ser do próprio usuário).
CLASSES_FUGA = frozenset({"CabinetWClass", "ExploreWClass", "Chrome_WidgetWin_1", "ApplicationFrameWindow",
                          "MozillaWindowClass", "IEFrame", "Notepad"})


class DesktopOcultoIndisponivel(RuntimeError):
    """O desktop oculto não existe (ou é o desktop do usuário): nenhum processo foi criado."""


class JanelaNoDesktopDoUsuario(RuntimeError):
    """Um processo do job abriu janela no desktop do usuário: a árvore foi morta na hora."""


# ---------------------------------------------------------------- Win32 (ctypes)

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _decl(dll, nome: str, res, *args):
    f = getattr(dll, nome)
    f.restype = res
    f.argtypes = list(args)
    return f


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
DESKENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.LPWSTR, wt.LPARAM)


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wt.BOOL)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                  "OtherOperationCount", "ReadTransferCount",
                                                  "WriteTransferCount", "OtherTransferCount")]


class _LIMITES_BASICOS(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class _LIMITES_ESTENDIDOS(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _LIMITES_BASICOS), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _LISTA_PIDS(ctypes.Structure):
    _fields_ = [("NumberOfAssignedProcesses", wt.DWORD), ("NumberOfProcessIdsInList", wt.DWORD),
                ("ProcessIdList", ctypes.c_size_t * 1024)]


CreateDesktopW = _decl(_u32, "CreateDesktopW", wt.HANDLE, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD,
                       wt.DWORD, ctypes.c_void_p)
OpenDesktopW = _decl(_u32, "OpenDesktopW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD)
OpenInputDesktop = _decl(_u32, "OpenInputDesktop", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
CloseDesktop = _decl(_u32, "CloseDesktop", wt.BOOL, wt.HANDLE)
EnumDesktopsW = _decl(_u32, "EnumDesktopsW", wt.BOOL, wt.HANDLE, DESKENUMPROC, wt.LPARAM)
EnumDesktopWindows = _decl(_u32, "EnumDesktopWindows", wt.BOOL, wt.HANDLE, WNDENUMPROC, wt.LPARAM)
GetProcessWindowStation = _decl(_u32, "GetProcessWindowStation", wt.HANDLE)
GetThreadDesktop = _decl(_u32, "GetThreadDesktop", wt.HANDLE, wt.DWORD)
GetUserObjectInformationW = _decl(_u32, "GetUserObjectInformationW", wt.BOOL, wt.HANDLE, ctypes.c_int,
                                  ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
GetWindowThreadProcessId = _decl(_u32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
IsWindowVisible = _decl(_u32, "IsWindowVisible", wt.BOOL, wt.HWND)
GetWindowTextW = _decl(_u32, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
GetClassNameW = _decl(_u32, "GetClassNameW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)

GetCurrentThreadId = _decl(_k32, "GetCurrentThreadId", wt.DWORD)
CreateProcessW = _decl(_k32, "CreateProcessW", wt.BOOL, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p,
                       ctypes.POINTER(PROCESS_INFORMATION))
ResumeThread = _decl(_k32, "ResumeThread", wt.DWORD, wt.HANDLE)
TerminateProcess = _decl(_k32, "TerminateProcess", wt.BOOL, wt.HANDLE, wt.UINT)
CloseHandle = _decl(_k32, "CloseHandle", wt.BOOL, wt.HANDLE)
WaitForSingleObject = _decl(_k32, "WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
GetExitCodeProcess = _decl(_k32, "GetExitCodeProcess", wt.BOOL, wt.HANDLE, ctypes.POINTER(wt.DWORD))
CreateJobObjectW = _decl(_k32, "CreateJobObjectW", wt.HANDLE, ctypes.c_void_p, wt.LPCWSTR)
SetInformationJobObject = _decl(_k32, "SetInformationJobObject", wt.BOOL, wt.HANDLE, ctypes.c_int,
                                ctypes.c_void_p, wt.DWORD)
QueryInformationJobObject = _decl(_k32, "QueryInformationJobObject", wt.BOOL, wt.HANDLE, ctypes.c_int,
                                  ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
AssignProcessToJobObject = _decl(_k32, "AssignProcessToJobObject", wt.BOOL, wt.HANDLE, wt.HANDLE)
TerminateJobObject = _decl(_k32, "TerminateJobObject", wt.BOOL, wt.HANDLE, wt.UINT)
CreatePipe = _decl(_k32, "CreatePipe", wt.BOOL, ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE),
                   ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD)
SetHandleInformation = _decl(_k32, "SetHandleInformation", wt.BOOL, wt.HANDLE, wt.DWORD, wt.DWORD)
CreateFileW = _decl(_k32, "CreateFileW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                    ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD, wt.DWORD, wt.HANDLE)
InitializeProcThreadAttributeList = _decl(_k32, "InitializeProcThreadAttributeList", wt.BOOL, ctypes.c_void_p,
                                          wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t))
UpdateProcThreadAttribute = _decl(_k32, "UpdateProcThreadAttribute", wt.BOOL, ctypes.c_void_p, wt.DWORD,
                                  ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                  ctypes.c_void_p)
DeleteProcThreadAttributeList = _decl(_k32, "DeleteProcThreadAttributeList", None, ctypes.c_void_p)

# todos os direitos de desktop menos DESKTOP_SWITCHDESKTOP (o agente nunca troca o desktop da tela)
ACESSO_DESKTOP = 0x00FF | 0x00020000  # | READ_CONTROL
LER_DESKTOP = 0x0001 | 0x0040  # DESKTOP_READOBJECTS | DESKTOP_ENUMERATE
UOI_NAME = 2
CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT = 0x00000004, 0x00000400
EXTENDED_STARTUPINFO_PRESENT, CREATE_NO_WINDOW = 0x00080000, 0x08000000
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
HANDLE_FLAG_INHERIT = 0x1
GENERIC_READ, FILE_SHARE_RW, OPEN_EXISTING = 0x80000000, 0x3, 3
JOB_KILL_ON_JOB_CLOSE, JOB_DIE_ON_UNHANDLED_EXCEPTION = 0x2000, 0x0400
JOB_INFO_BASIC_PID_LIST, JOB_INFO_BASIC_UI, JOB_INFO_EXTENDED = 3, 4, 9
STILL_ACTIVE, WAIT_OBJECT_0 = 259, 0
_INVALIDO = wt.HANDLE(-1).value


def _erro(texto: str) -> OSError:
    codigo = ctypes.get_last_error()
    return OSError(codigo, f"{texto}: {ctypes.FormatError(codigo).strip()}")


# ---------------------------------------------------------------- consultas

def _nome_objeto(h) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = wt.DWORD()
    if h and GetUserObjectInformationW(h, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(n)):
        return buf.value
    return ""


def estacao() -> str:
    """Nome da estação de janelas deste processo (normalmente WinSta0)."""
    return _nome_objeto(GetProcessWindowStation()) or "WinSta0"


def desktop_da_thread() -> str:
    """Nome do desktop da thread que chama (o de um processo no desktop oculto é o nome dele)."""
    return _nome_objeto(GetThreadDesktop(GetCurrentThreadId()))


def desktop_de_entrada() -> str | None:
    """Nome do desktop que está na tela agora (None se não der para abrir, ex.: sessão bloqueada)."""
    h = OpenInputDesktop(0, False, 0x0001)
    if not h:
        return None
    try:
        return _nome_objeto(h)
    finally:
        CloseDesktop(h)


def desktops() -> list[str]:
    """Desktops da estação de janelas deste processo."""
    out: list[str] = []
    EnumDesktopsW(GetProcessWindowStation(), DESKENUMPROC(lambda n, _: out.append(n) or True), 0)
    return out


def dentro_do_oculto() -> bool:
    """Este processo foi lançado no desktop oculto pelo agente (variável ligada por `executar`)?"""
    return os.environ.get(VARIAVEL) == "1"


def no_desktop_oculto() -> bool:
    """Garantia para abrir janela: lançado pelo agente no desktop oculto E a thread está mesmo num
    desktop que não é o do usuário nem o que está na tela."""
    atual = desktop_da_thread()
    return (dentro_do_oculto() and bool(atual) and atual.casefold() != DESKTOP_DO_USUARIO.casefold()
            and atual != desktop_de_entrada())


def _texto(h) -> str:
    b = ctypes.create_unicode_buffer(512)
    GetWindowTextW(h, b, 512)
    return b.value


def _classe(h) -> str:
    b = ctypes.create_unicode_buffer(256)
    GetClassNameW(h, b, 256)
    return b.value


def _pid(h) -> int:
    p = wt.DWORD()
    GetWindowThreadProcessId(h, ctypes.byref(p))
    return p.value


def janelas_do_desktop(nome: str) -> list[dict]:
    """Janelas de topo do desktop `nome` (pid, título, classe, visível). Vazio se não abrir."""
    h = OpenDesktopW(nome, 0, False, LER_DESKTOP)
    if not h:
        return []
    out: list[dict] = []

    def cb(hwnd, _):
        out.append({"hwnd": int(hwnd), "pid": _pid(hwnd), "titulo": _texto(hwnd), "classe": _classe(hwnd),
                    "visivel": bool(IsWindowVisible(hwnd))})
        return True

    try:
        EnumDesktopWindows(h, WNDENUMPROC(cb), 0)
    finally:
        CloseDesktop(h)
    return out


def _decodificar(dados: bytes) -> str:
    try:
        return dados.decode("utf-8")
    except UnicodeDecodeError:
        try:
            cp = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except (AttributeError, OSError):
            cp = "cp850"
        return dados.decode(cp, errors="replace")


def _resolver_exe(argv0: str, cwd: Path) -> str:
    p = Path(argv0)
    if not p.is_absolute() and (("/" in argv0) or ("\\" in argv0)):
        p = Path(cwd) / p
    if p.is_absolute():
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"executável não encontrado: {p}")
    achado = shutil.which(argv0)
    if not achado:
        raise FileNotFoundError(f"executável não encontrado no PATH: {argv0}")
    return achado


def _bloco_ambiente(env: dict[str, str]) -> ctypes.Array:
    itens = sorted(env.items(), key=lambda kv: kv[0].upper())
    s = "".join(f"{k}={v}\0" for k, v in itens) + "\0"
    return (ctypes.c_wchar * len(s))(*s)


# ---------------------------------------------------------------- job object

class _Job:
    """Job object com a árvore de uma execução: morre inteiro ao terminar ou ao fechar o handle."""

    def __init__(self, restricoes: int):
        self.h = CreateJobObjectW(None, None)
        if not self.h:
            raise _erro("CreateJobObjectW falhou")
        info = _LIMITES_ESTENDIDOS()
        info.BasicLimitInformation.LimitFlags = JOB_KILL_ON_JOB_CLOSE | JOB_DIE_ON_UNHANDLED_EXCEPTION
        if not SetInformationJobObject(self.h, JOB_INFO_EXTENDED, ctypes.byref(info), ctypes.sizeof(info)):
            e = _erro("SetInformationJobObject (limites) falhou")
            CloseHandle(self.h)
            raise e
        if restricoes:
            v = wt.DWORD(restricoes)
            if not SetInformationJobObject(self.h, JOB_INFO_BASIC_UI, ctypes.byref(v), ctypes.sizeof(v)):
                e = _erro("SetInformationJobObject (restrições de interface) falhou")
                CloseHandle(self.h)
                raise e
        self.terminado = False
        self.violacoes: list[dict] = []
        self._mutex = threading.Lock()  # o vigia e a thread que executa usam o mesmo handle

    def pids(self) -> list[int]:
        with self._mutex:
            if not self.h:
                return []
            lista = _LISTA_PIDS()
            if not QueryInformationJobObject(self.h, JOB_INFO_BASIC_PID_LIST, ctypes.byref(lista),
                                             ctypes.sizeof(lista), None):
                return []
            return [int(lista.ProcessIdList[i]) for i in range(lista.NumberOfProcessIdsInList)]

    def terminar(self, codigo: int) -> None:
        with self._mutex:
            if self.h and not self.terminado:
                TerminateJobObject(self.h, codigo)
                self.terminado = True

    def fechar(self) -> None:
        self.terminar(1)
        with self._mutex:
            if self.h:
                CloseHandle(self.h)
                self.h = None


# ---------------------------------------------------------------- vigia do desktop do usuário

class _Vigia(threading.Thread):
    """Confere o desktop do usuário a cada PASSO_VIGIA s: janela de processo de um job ativo lá mata
    o job na hora (a janela some antes de o usuário perceber) e fica registrada."""

    def __init__(self, dono: "DesktopOculto"):
        super().__init__(daemon=True, name="vigia-desktop-oculto")
        self.dono = dono
        self.parar = threading.Event()

    def run(self) -> None:
        while not self.parar.wait(PASSO_VIGIA):
            try:
                self.dono._vigiar_uma_vez()
            except Exception:  # o vigia não pode morrer por uma janela sumindo no meio da consulta
                continue


# ---------------------------------------------------------------- desktop oculto

class DesktopOculto:
    """Desktop extra (nunca mostrado) para rodar processos de GUI sem tocar na tela do usuário.

    `with DesktopOculto() as d: d.executar([...], cwd=..., log=..., timeout=...)`. Ao sair, mata
    todas as árvores que ele criou e fecha o handle (o Windows apaga o desktop quando o último
    processo dele termina)."""

    def __init__(self, nome: str = NOME_PADRAO, *, restricoes: int | None = None, vigiar: bool = True):
        if not nome or any(c in nome for c in "\\/") or nome.casefold() == DESKTOP_DO_USUARIO.casefold():
            raise ValueError(f"nome de desktop oculto inválido: {nome!r}")
        self.nome = nome
        self.restricoes = sum(RESTRICOES_UI.values()) if restricoes is None else restricoes
        self.vigiar = vigiar
        self._h = None
        self._criado = False
        self._jobs: list[_Job] = []
        self._trava = threading.Lock()
        self._vigia: _Vigia | None = None
        self.violacoes: list[dict] = []
        self.execucoes: list[dict] = []
        self.fugas: list[dict] = []  # {classe, processo, quando}: nunca o título da janela
        self._fugas_vistas: set[int] = set()
        self._iniciais: set[int] | None = None
        self._inicio_vigia = 0.0

    @property
    def caminho(self) -> str:
        """Valor de `STARTUPINFO.lpDesktop`: "WinSta0\\<nome>"."""
        return f"{estacao()}\\{self.nome}"

    # ---------- ciclo de vida

    def __enter__(self) -> "DesktopOculto":
        if self._h:
            return self
        h = OpenDesktopW(self.nome, 0, False, ACESSO_DESKTOP)
        if not h:
            h = CreateDesktopW(self.nome, None, None, 0, ACESSO_DESKTOP, None)
            self._criado = bool(h)
        if not h:
            raise DesktopOcultoIndisponivel(f"não consegui criar o desktop oculto {self.nome!r}: "
                                            f"{ctypes.FormatError(ctypes.get_last_error()).strip()}")
        self._h = h
        try:
            self._conferir()
        except BaseException:
            self.fechar()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self.fechar()

    def fechar(self) -> None:
        """Mata todas as árvores criadas por este objeto, para o vigia e fecha o handle do desktop."""
        with self._trava:
            for job in self._jobs:
                job.fechar()
            self._jobs.clear()
        if self._vigia is not None:
            self._vigia.parar.set()
            self._vigia.join(2)
            self._vigia = None
        if self._h:
            CloseDesktop(self._h)
            self._h = None

    def existe(self) -> bool:
        """O desktop existe na estação e abre pelo nome?"""
        if self.nome not in desktops():
            return False
        h = OpenDesktopW(self.nome, 0, False, 0x0001)
        if not h:
            return False
        CloseDesktop(h)
        return True

    def _conferir(self) -> None:
        """Antes de criar qualquer processo: o desktop oculto existe e não é o que está na tela."""
        if not self._h:
            raise DesktopOcultoIndisponivel("o desktop oculto não está aberto (use `with DesktopOculto()`)")
        if not self.existe():
            raise DesktopOcultoIndisponivel(f"o desktop oculto {self.nome!r} não existe: nenhum processo criado")
        if desktop_de_entrada() == self.nome:
            raise DesktopOcultoIndisponivel(f"o desktop {self.nome!r} está na tela: nenhum processo criado")

    # ---------- vigia

    def _janelas_do_usuario(self) -> list[dict]:
        return janelas_do_desktop(DESKTOP_DO_USUARIO)

    def _vigiar_uma_vez(self) -> None:
        with self._trava:
            ativos = [(j, set(j.pids())) for j in self._jobs if not j.terminado]
        if not any(p for _, p in ativos):
            return
        janelas = self._janelas_do_usuario()
        self._olhar_fugas(janelas, set().union(*(p for _, p in ativos)))
        for jan in janelas:
            for job, pids in ativos:
                if jan["pid"] in pids:
                    job.terminar(CODIGO_VIOLACAO)
                    v = {**jan, "quando": time.strftime("%Y-%m-%dT%H:%M:%S")}
                    job.violacoes.append(v)
                    self.violacoes.append(v)

    def _olhar_fugas(self, janelas: list[dict], pids_do_job: set[int]) -> None:
        """Janela nova de Explorer/navegador (CLASSES_FUGA) no desktop do usuário, de fora do job e de um
        processo que nasceu depois do começo da execução: registrada como possível fuga (nada é fechado).
        Um aplicativo que o usuário já tinha aberto (o navegador abrindo uma janela nova) não conta. Só o
        nome do processo e a classe da janela são guardados: o título nunca (é do usuário)."""
        if self._iniciais is None:
            return
        import psutil

        for jan in janelas:
            h = jan["hwnd"]
            if (not jan["visivel"] or h in self._iniciais or h in self._fugas_vistas or jan["pid"] in pids_do_job
                    or jan["classe"] not in CLASSES_FUGA):
                continue
            self._fugas_vistas.add(h)
            try:
                proc = psutil.Process(jan["pid"])
                if proc.create_time() < self._inicio_vigia:
                    continue  # processo do usuário que já existia antes
                nome = proc.name()
            except psutil.Error:
                continue
            self.fugas.append({"classe": jan["classe"], "processo": nome,
                               "quando": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def _garantir_vigia(self) -> None:
        if self._iniciais is None:
            self._inicio_vigia = time.time()
            try:  # só leitura: o que já estava aberto no desktop do usuário antes de começar
                self._iniciais = {j["hwnd"] for j in self._janelas_do_usuario() if j["visivel"]}
            except Exception:
                self._iniciais = set()
        if self.vigiar and self._vigia is None:
            self._vigia = _Vigia(self)
            self._vigia.start()

    # ---------- processos

    def executar(self, argv: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None, log: Path,
                 timeout: float, eco: Callable[[str], None] | None = None) -> int:
        """Roda `argv` no desktop oculto e devolve o código de saída. stdout+stderr vão, redigidos
        linha a linha, para `log` (e para `eco`, se dado). No tempo esgotado a árvore inteira morre
        (código CODIGO_ESTOUROU); no fim, o que sobrou da árvore também morre. `env` (padrão: o
        ambiente atual) ganha MAW_AGENTE_DESKTOP_OCULTO=1."""
        if not argv:
            raise ValueError("argv vazio")
        cwd = Path(cwd)
        log = sandbox.garantir_escrita(Path(log))
        sandbox.criar_pasta(log.parent)
        exe = _resolver_exe(str(argv[0]), cwd)
        amb = dict(os.environ if env is None else env)
        amb[VARIAVEL] = "1"
        amb.setdefault("PYTHONIOENCODING", "utf-8")
        amb.setdefault("PYTHONUNBUFFERED", "1")
        self._conferir()
        arq = open(log, "ab")  # antes de o processo existir: um log que não abre não deixa processo solto
        job = _Job(self.restricoes)
        with self._trava:
            self._jobs.append(job)
        self._garantir_vigia()
        inicio = time.monotonic()
        registro: dict = {"argv": [exe, *map(str, argv[1:])], "log": str(log), "estourou": False,
                          "violacao": False, "sobras": []}
        try:
            leitor_fd, hproc, pid = self._criar(exe, [str(a) for a in argv[1:]], cwd, amb, job)
        except BaseException:
            arq.close()
            with self._trava:
                if job in self._jobs:
                    self._jobs.remove(job)
            job.fechar()
            raise
        registro["pid"] = pid
        with arq:
            leitor = threading.Thread(target=self._ler, args=(leitor_fd, arq, eco), daemon=True,
                                      name=f"leitor-oculto-{pid}")
            leitor.start()
            try:
                limite = inicio + timeout
                while True:
                    falta = limite - time.monotonic()
                    if falta <= 0:
                        registro["estourou"] = True
                        job.terminar(CODIGO_ESTOUROU)
                        WaitForSingleObject(hproc, 10_000)
                        break
                    if WaitForSingleObject(hproc, int(min(falta, 0.5) * 1000)) == WAIT_OBJECT_0:
                        break
                codigo = wt.DWORD()
                GetExitCodeProcess(hproc, ctypes.byref(codigo))
                registro["codigo"] = int(codigo.value)
                registro["sobras"] = [n for n in self._nomes(job.pids())
                                      if not n.casefold().startswith("conhost.exe")]  # o console sem janela
            finally:
                job.terminar(CODIGO_ESTOUROU if registro["estourou"] else 1)  # o que sobrou da árvore
                CloseHandle(hproc)
                leitor.join(15)
                registro["violacao"] = bool(job.violacoes)
                registro["segundos"] = round(time.monotonic() - inicio, 1)
                if registro["estourou"]:
                    self._linha(arq, eco, f"[desktop oculto] tempo esgotado ({timeout:g} s): árvore de processos "
                                          "encerrada")
                if registro["sobras"]:
                    self._linha(arq, eco, "[desktop oculto] processos que sobraram e foram encerrados: "
                                          + ", ".join(registro["sobras"]))
                with self._trava:
                    if job in self._jobs:
                        self._jobs.remove(job)
                job.fechar()
                self.execucoes.append(registro)
        if job.violacoes:
            raise JanelaNoDesktopDoUsuario(
                "processo do desktop oculto abriu janela no desktop do usuário; a árvore foi encerrada: "
                + "; ".join(f"pid {v['pid']} {v['classe']!r} {v['titulo']!r}" for v in job.violacoes))
        return CODIGO_ESTOUROU if registro["estourou"] else registro["codigo"]

    @staticmethod
    def _nomes(pids: list[int]) -> list[str]:
        import psutil

        out = []
        for p in pids:
            try:
                out.append(f"{psutil.Process(p).name()} ({p})")
            except psutil.Error:
                continue
        return out

    @staticmethod
    def _linha(arq, eco, texto: str) -> None:
        limpo = redacao.redigir(texto)
        arq.write((limpo + "\n").encode("utf-8"))
        arq.flush()
        if eco is not None:
            eco(limpo)

    def _ler(self, fd: int, arq, eco) -> None:
        with os.fdopen(fd, "rb", buffering=0) as f:
            pendente = b""
            while True:
                try:
                    bloco = f.read(65536)
                except OSError:
                    bloco = b""
                if not bloco:
                    break
                pendente += bloco
                *linhas, pendente = pendente.split(b"\n")
                for bruta in linhas:
                    self._linha(arq, eco, _decodificar(bruta).rstrip("\r"))
            if pendente:
                self._linha(arq, eco, _decodificar(pendente).rstrip("\r"))

    def _criar(self, exe: str, args: list[str], cwd: Path, amb: dict[str, str], job: _Job) -> tuple[int, int, int]:
        """CreateProcessW suspenso, no desktop oculto, sem console visível, no job; stdout+stderr num
        pipe (só as duas pontas certas são herdadas). Devolve (fd de leitura, hProcess, pid)."""
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        leitura, escrita = wt.HANDLE(), wt.HANDLE()
        if not CreatePipe(ctypes.byref(leitura), ctypes.byref(escrita), ctypes.byref(sa), 0):
            raise _erro("CreatePipe falhou")
        SetHandleInformation(leitura, HANDLE_FLAG_INHERIT, 0)
        nul = CreateFileW("NUL", GENERIC_READ, FILE_SHARE_RW, ctypes.byref(sa), OPEN_EXISTING, 0, None)
        if nul == _INVALIDO:
            e = _erro("abrir NUL falhou")
            CloseHandle(leitura)
            CloseHandle(escrita)
            raise e
        tam = ctypes.c_size_t()
        InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(tam))
        atributos = ctypes.create_string_buffer(tam.value)
        herdados = (wt.HANDLE * 2)(escrita, nul)
        pi = PROCESS_INFORMATION()
        try:
            if not InitializeProcThreadAttributeList(atributos, 1, 0, ctypes.byref(tam)):
                raise _erro("InitializeProcThreadAttributeList falhou")
            try:
                if not UpdateProcThreadAttribute(atributos, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, herdados,
                                                 ctypes.sizeof(herdados), None, None):
                    raise _erro("UpdateProcThreadAttribute falhou")
                si = STARTUPINFOEXW()
                si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
                desktop = ctypes.create_unicode_buffer(self.caminho)
                si.StartupInfo.lpDesktop = ctypes.cast(desktop, wt.LPWSTR)
                si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
                si.StartupInfo.hStdInput = nul
                si.StartupInfo.hStdOutput = escrita
                si.StartupInfo.hStdError = escrita
                si.lpAttributeList = ctypes.cast(atributos, ctypes.c_void_p)
                linha = ctypes.create_unicode_buffer(subprocess.list2cmdline([exe, *args]))
                bloco = _bloco_ambiente(amb)
                flags = (CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
                         | EXTENDED_STARTUPINFO_PRESENT)
                if not CreateProcessW(None, linha, None, None, True, flags, bloco, str(cwd), ctypes.byref(si),
                                      ctypes.byref(pi)):
                    raise _erro(f"CreateProcessW falhou para {exe}")
            finally:
                DeleteProcThreadAttributeList(atributos)
        except BaseException:
            CloseHandle(leitura)
            raise
        finally:
            CloseHandle(escrita)
            CloseHandle(nul)
        if not AssignProcessToJobObject(job.h, pi.hProcess):
            e = _erro("AssignProcessToJobObject falhou")
            TerminateProcess(pi.hProcess, 1)
            CloseHandle(pi.hThread)
            CloseHandle(pi.hProcess)
            CloseHandle(leitura)
            raise e
        ResumeThread(pi.hThread)
        CloseHandle(pi.hThread)
        import msvcrt

        fd = msvcrt.open_osfhandle(leitura.value, os.O_RDONLY)
        return fd, pi.hProcess, int(pi.dwProcessId)


def rodar_oculto(argv: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None, log: Path,
                 timeout: float, nome: str = NOME_PADRAO, eco: Callable[[str], None] | None = None,
                 relatorio: dict | None = None) -> int:
    """Atalho: abre o desktop oculto, executa `argv` lá e fecha (mata o que sobrou). `relatorio`, se
    dado, recebe "fugas", "violacoes" e "execucoes" (também quando a execução levanta)."""
    with DesktopOculto(nome) as d:
        try:
            return d.executar(argv, cwd=cwd, env=env, log=log, timeout=timeout, eco=eco)
        finally:
            if relatorio is not None:
                relatorio.update(fugas=list(d.fugas), violacoes=list(d.violacoes), execucoes=list(d.execucoes))


def descrever_fuga(f: dict) -> str:
    """Só a classe da janela e o nome do processo (o título é do usuário e nunca é gravado)."""
    return f"possível janela fora do desktop oculto: {f.get('classe')} ({f.get('processo')})"


def log_padrao(rotulo: str = "execucao") -> Path:
    """Log fora do repositório público: work/desktop-oculto/<data-hora>-<rótulo>-<pid>.log."""
    return config.WORK / "desktop-oculto" / f"{time.strftime('%Y%m%d-%H%M%S')}-{rotulo}-{os.getpid()}.log"


def _imprimir(texto: str) -> None:
    try:
        print(texto, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(texto.encode(enc, errors="replace").decode(enc), flush=True)
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("uso: python -m maw_agent.desktop_oculto [--nome N] [--timeout S] [--log ARQ] -- <comando...>",
              file=sys.stderr)
        return 2
    i = argv.index("--")
    p = argparse.ArgumentParser(prog="python -m maw_agent.desktop_oculto",
                                description="roda um comando no desktop oculto e repassa o log")
    p.add_argument("--nome", default=NOME_PADRAO)
    p.add_argument("--timeout", type=float, default=4 * 3600.0)
    p.add_argument("--log", type=Path, default=None)
    opcoes = p.parse_args(argv[:i])
    comando = argv[i + 1:]
    if not comando:
        print("faltou o comando depois de --", file=sys.stderr)
        return 2
    log = opcoes.log or log_padrao(Path(comando[0]).stem)
    relatorio: dict = {}
    try:
        codigo = rodar_oculto(comando, cwd=Path.cwd(), log=log, timeout=opcoes.timeout, nome=opcoes.nome,
                              eco=_imprimir, relatorio=relatorio)
    except (DesktopOcultoIndisponivel, JanelaNoDesktopDoUsuario, FileNotFoundError) as ex:
        print(f"[desktop oculto] {ex}", file=sys.stderr)
        return CODIGO_VIOLACAO if isinstance(ex, JanelaNoDesktopDoUsuario) else 2
    finally:
        for f in relatorio.get("fugas", []):
            print(f"[desktop oculto] {descrever_fuga(f)}", file=sys.stderr)
    print(f"[desktop oculto] código {codigo}; log: {log}", file=sys.stderr)
    return codigo


if __name__ == "__main__":
    sys.exit(main())
