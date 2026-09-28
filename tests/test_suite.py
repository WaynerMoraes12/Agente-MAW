from pathlib import Path
from maw_agent import suite

OK = ("  (aviso qualquer)\r\n\r\nNOME - suite de testes automatizados (juce::UnitTest)\r\n\r\n"
      "=====\r\n\r\n"
      "[ok]     Bloco A  ->  faz x   (5 ok, 0 falha(s))\r\n\r\n"
      "[ok]     Bloco B  ->  faz y   (3 ok, 0 falha(s))\r\n\r\n"
      "Blocos de teste ........ 2\r\n\r\nVerificacoes que deram ok 8\r\n\r\n"
      "Verificacoes que falharam 0\r\n\r\nTempo total ............ 120 ms\r\n\r\n"
      "RESULTADO: TUDO PASSOU.\r\n")

FALHA = ("NOME - suite de testes automatizados (juce::UnitTest)\n"
         "[ok]     Bloco A  ->  faz x   (5 ok, 0 falha(s))\n"
         "[FALHOU] Bloco B  ->  faz y   (2 ok, 1 falha(s))\n"
         "Blocos de teste ........ 2\nVerificacoes que deram ok 7\nVerificacoes que falharam 1\n"
         "Tempo total ............ 99 ms\n"
         "RESULTADO: FALHOU - 1 verificacao(oes) em 1 bloco(s).\n\nDetalhe das falhas:\n"
         "  - Bloco B / faz y\n      !!! Test 1 failed: esperado 1, obtido 2\n")

def test_interpreta_sucesso_com_crlf_e_preambulo():
    r = suite.interpretar_suite(OK, 0, 1.0)
    assert [b.nome for b in r.blocos] == ["Bloco A", "Bloco B"]
    assert r.total_ok == 8 and r.total_falhas == 0 and r.blocos_declarados == 2
    assert r.preambulo == ["(aviso qualquer)"]
    assert r.passou and r.incoerencias == []

def test_interpreta_falha_com_detalhe():
    r = suite.interpretar_suite(FALHA, 1, 1.0)
    assert not r.passou and r.total_falhas == 1
    assert r.blocos[1].falhas == 1 and r.blocos[1].sub == "faz y"
    assert any("esperado 1, obtido 2" in d for d in r.detalhes)

def test_passou_com_exit_1_e_incoerente():
    r = suite.interpretar_suite(OK, 1, 1.0)
    assert not r.passou and any("código de saída" in i for i in r.incoerencias)

def test_saida_truncada_nao_e_sucesso():
    r = suite.interpretar_suite("NOME - suite de testes automatizados\n[ok]     Bloco A  ->  x   (1 ok, 0 falha(s))\n", 0, 1.0)
    assert not r.passou and r.declarado == "AUSENTE"
    assert any("totais" in i for i in r.incoerencias)

def test_soma_dos_blocos_confere_com_totais():
    texto = OK.replace("Verificacoes que deram ok 8", "Verificacoes que deram ok 9")
    r = suite.interpretar_suite(texto, 0, 1.0)
    assert not r.passou and any("soma" in i for i in r.incoerencias)

BENCH = """ trilhas | tempo medio (ms) | tempo maximo (ms) | carga media (%) | carga maxima (%)
---------+------------------+-------------------+-----------------+------------------
       1 |            0.004 |             0.032 |            0.04 |             0.30
      32 |            0.031 |             0.080 |            0.29 |             0.75
"""

def test_interpreta_benchmark():
    linhas, cab = suite.interpretar_benchmark("Plugin do master descarregado\n" + BENCH)
    assert [l.trilhas for l in linhas] == [1, 32]
    assert linhas[1].carga_max == 0.75
    assert "Plugin do master descarregado" in cab[0]

def test_timeout_sem_exit_code():
    """Processo morto pelo timeout: exit_code é None."""
    r = suite.interpretar_suite(OK, None, 1.0)
    assert not r.passou
    assert r.exit_code is None


# ---------- executar: ambiente, diálogos do Windows e MAW aberta ----------
import ctypes
import os
import sys


def test_executar_passa_o_ambiente(tmp_path):
    env = {**os.environ, "VAR_DO_TESTE": "valor-do-teste"}
    codigo, saida, _ = suite.executar(Path(sys.executable),
                                      "-cimport os; print(os.environ.get('VAR_DO_TESTE'))", tmp_path, 60, env=env)
    assert codigo == 0 and "valor-do-teste" in saida


def test_executar_desliga_dialogos_de_erro_no_filho_e_restaura(tmp_path, monkeypatch):
    k32 = ctypes.windll.kernel32
    original = k32.GetErrorMode()
    visto = {}

    def run_falso(args, **kw):
        visto["modo"] = k32.GetErrorMode()
        return suite.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(suite.subprocess, "run", run_falso)
    k32.SetErrorMode(0)
    try:
        suite.executar(Path("x.exe"), "--run-tests", tmp_path, 10)
        assert visto["modo"] & 0x3 == 0x3  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
        assert k32.GetErrorMode() == 0
    finally:
        k32.SetErrorMode(original)


def test_maw_aberta(monkeypatch):
    class P:
        def __init__(self, nome): self.info = {"name": nome}
    monkeypatch.setattr(suite.psutil, "process_iter", lambda attrs: [P("explorer.exe"), P(None)])
    assert suite.maw_aberta() is False
    monkeypatch.setattr(suite.psutil, "process_iter", lambda attrs: [P("MAW_APP.exe")])
    assert suite.maw_aberta() is True


def test_ambiente_com_ffmpeg_no_path(tmp_path):
    base = {"PATH": r"C:\Windows", "OUTRA": "1"}
    assert suite.ambiente_com_ffmpeg(base, tmp_path / "nao-existe") == base
    bin_ = tmp_path / "ffmpeg" / "bin"; bin_.mkdir(parents=True)
    env = suite.ambiente_com_ffmpeg(base, bin_)
    assert env["PATH"].split(os.pathsep)[0] == str(bin_) and env["OUTRA"] == "1"
    assert base["PATH"] == r"C:\Windows"  # não altera o dicionário recebido
