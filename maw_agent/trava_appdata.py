"""Trava única da máquina para toda mudança no %APPDATA%\\MAW: sessão de GUI (a sessão inteira),
suíte (backup → roda → restaura), calibração, restauração de pendências e a restauração do
encerramento da sprint. O aplicativo é de instância única e todos usam a mesma pasta de
configuração, então só um dono por vez.

A trava é o primeiro byte de `%LOCALAPPDATA%\\AgenteMAW\\sessao-maw.lock`, travado com
`msvcrt.locking` (o Windows solta sozinho se o processo morrer). Dentro de um processo ela é
reentrante por thread: a mesma thread que já tem a trava entra de novo na hora (ex.: a sessão de GUI
restaurando pendências antes de montar o ambiente); outra thread do mesmo processo espera como se
fosse outro processo. Quem pega recebe uma `Posse`, que pode ser solta de qualquer thread (o
encerramento forçado de uma sessão vem da thread que controla o tempo do cenário)."""
from __future__ import annotations

import msvcrt
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import config, sandbox

ARQUIVO = "sessao-maw.lock"
PASSO = 2.0  # segundos entre as tentativas
AVISO = "esperando a MAW ficar livre (outro cenário em curso)"


class TravaOcupada(RuntimeError):
    """A trava do %APPDATA%\\MAW não ficou livre no prazo: nada foi tocado."""


def _caminho_real() -> Path:
    return Path(config.BACKUPS).parent / ARQUIVO


def caminho_padrao() -> Path:
    """Onde fica o arquivo da trava (ao lado da pasta dos backups). Os testes trocam isto."""
    return _caminho_real()


class Posse:
    """A trava na mão. `liberar()` (de qualquer thread) é idempotente; também serve de `with`."""

    def __init__(self, trava: "_Trava"):
        self._trava = trava
        self.solta = False

    def liberar(self) -> None:
        self._trava._liberar(self)

    def __enter__(self) -> "Posse":
        return self

    def __exit__(self, *_exc) -> None:
        self.liberar()


class _Trava:
    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self._mutex = threading.Lock()
        self._fd: int | None = None
        self._dono: int | None = None  # thread que pegou a trava no arquivo
        self._posses = 0

    def _tentar(self) -> int | None:
        sandbox.criar_pasta(self.caminho.parent)
        sandbox.garantir_escrita(self.caminho)
        fd = os.open(self.caminho, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return fd
        except OSError:
            os.close(fd)
            return None

    def adquirir(self, espera: float, cancelar: Callable[[], bool] = lambda: False,
                 ao_esperar: Callable[[], None] | None = None) -> Posse | None:
        """Posse da trava, esperando até `espera` s (tentativa a cada PASSO s; avisa uma vez no stderr e
        chama `ao_esperar`). None se o tempo acabou ou se `cancelar()` ficou verdadeiro."""
        eu = threading.get_ident()
        limite = time.monotonic() + espera
        avisou = False
        while True:
            with self._mutex:
                if self._fd is not None and self._dono == eu:
                    self._posses += 1
                    return Posse(self)
                if self._fd is None:
                    fd = self._tentar()
                    if fd is not None:
                        self._fd, self._dono, self._posses = fd, eu, 1
                        return Posse(self)
            falta = limite - time.monotonic()
            if falta <= 0 or cancelar():
                return None
            if not avisou:
                avisou = True
                try:
                    print(AVISO, file=sys.stderr, flush=True)
                except (OSError, ValueError):
                    pass
                if ao_esperar is not None:
                    ao_esperar()
            time.sleep(min(PASSO, falta))

    def _liberar(self, posse: Posse) -> None:
        with self._mutex:
            if posse.solta:
                return
            posse.solta = True
            self._posses -= 1
            if self._posses > 0 or self._fd is None:
                return
            fd, self._fd, self._dono = self._fd, None, None
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            finally:
                os.close(fd)

    def presa_por_mim(self) -> bool:
        """Esta thread já tem a trava?"""
        with self._mutex:
            return self._fd is not None and self._dono == threading.get_ident()


_travas: dict[str, _Trava] = {}
_travas_mutex = threading.Lock()


def trava(caminho: Path | None = None) -> _Trava:
    """A trava (uma por arquivo, no processo inteiro)."""
    p = Path(caminho) if caminho is not None else caminho_padrao()
    chave = os.path.normcase(os.path.abspath(p))
    with _travas_mutex:
        if chave not in _travas:
            _travas[chave] = _Trava(p)
        return _travas[chave]


def adquirir(espera: float, cancelar: Callable[[], bool] = lambda: False,
             ao_esperar: Callable[[], None] | None = None, caminho: Path | None = None) -> Posse | None:
    return trava(caminho).adquirir(espera, cancelar, ao_esperar)


def segurar(espera: float, oque: str = "mexer no %APPDATA%\\MAW") -> Posse:
    """Posse da trava ou TravaOcupada (para `with trava_appdata.segurar(...):`)."""
    posse = adquirir(espera)
    if posse is None:
        raise TravaOcupada(f"não deu para {oque}: outra sessão do agente está usando a MAW há mais de "
                           f"{espera:g} s")
    return posse
