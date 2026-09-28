"""Trava do %APPDATA% da MAW: exclusiva entre processos e entre threads, reentrante na mesma thread,
solta de qualquer thread."""
import subprocess
import sys
import threading
import time

import pytest

from maw_agent import trava_appdata as ta


@pytest.fixture(autouse=True)
def _passo_curto(monkeypatch):
    monkeypatch.setattr(ta, "PASSO", 0.05)


def _livre() -> bool:
    caixa = {}
    t = threading.Thread(target=lambda: caixa.update(p=ta.adquirir(0)))
    t.start()
    t.join(5)
    if caixa["p"] is not None:
        caixa["p"].liberar()
        return True
    return False


def test_reentrante_na_mesma_thread_e_solta_so_na_ultima_posse():
    a = ta.adquirir(0)
    b = ta.adquirir(0)  # a mesma thread entra de novo na hora
    assert a is not None and b is not None and ta.trava().presa_por_mim()
    a.liberar()
    a.liberar()  # idempotente
    assert not _livre()
    b.liberar()
    assert _livre() and not ta.trava().presa_por_mim()


def test_outra_thread_espera_e_a_posse_solta_de_qualquer_thread():
    a = ta.adquirir(0)
    marcas = {}

    def outra():
        marcas["pediu"] = time.monotonic()
        p = ta.adquirir(10, ao_esperar=lambda: marcas.setdefault("esperou", True))
        marcas["pegou"] = time.monotonic()
        p.liberar()

    t = threading.Thread(target=outra)
    t.start()
    time.sleep(0.5)
    solta_em = time.monotonic()
    threading.Thread(target=a.liberar).start()  # solta por uma terceira thread
    t.join(10)
    assert marcas.get("esperou") and marcas["pegou"] >= solta_em
    assert _livre()


def test_prazo_cancelamento_e_segurar(capsys):
    a = ta.adquirir(0)
    try:
        caixa = {}
        t = threading.Thread(target=lambda: caixa.update(p=ta.adquirir(0.3)))
        t.start()
        t.join(5)
        assert caixa["p"] is None
        assert ta.AVISO in capsys.readouterr().err
        t = threading.Thread(target=lambda: caixa.update(c=ta.adquirir(30, cancelar=lambda: True)))
        t0 = time.monotonic()
        t.start()
        t.join(5)
        assert caixa["c"] is None and time.monotonic() - t0 < 2
        erro = {}

        def segura():
            try:
                ta.segurar(0.2, "restaurar")
            except ta.TravaOcupada as ex:
                erro["e"] = str(ex)

        t = threading.Thread(target=segura)
        t.start()
        t.join(5)
        assert "não deu para restaurar" in erro["e"]
    finally:
        a.liberar()


def test_vale_entre_processos():
    codigo = ("import msvcrt, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
              "msvcrt.locking(fd, msvcrt.LK_NBLCK, 1); print('pegou')")
    caminho = ta.caminho_padrao()
    with ta.segurar(0):
        r = subprocess.run([sys.executable, "-c", codigo, str(caminho)], capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        assert r.returncode != 0 and "pegou" not in r.stdout
    r = subprocess.run([sys.executable, "-c", codigo, str(caminho)], capture_output=True, text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert r.returncode == 0 and "pegou" in r.stdout
