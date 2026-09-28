import subprocess, sys
from pathlib import Path
from maw_agent import config
from maw_agent.cli import main

def test_ajuda_lista_subcomandos(capsys):
    assert main(["--help-json"]) == 0
    saida = capsys.readouterr().out
    assert '"subcomandos"' in saida

def test_modulo_executavel():
    p = subprocess.run([sys.executable, "-m", "maw_agent", "--help-json"],
                       capture_output=True, text=True)
    assert p.returncode == 0

def test_caminhos_relativos_a_raiz():
    assert config.WORK == config.RAIZ / "work"
    assert config.PRIVADO == config.RAIZ / "privado"
    assert config.RELATORIOS == config.PRIVADO / "relatorios"

def test_pastas_protegidas_aceita_override(monkeypatch, tmp_path):
    a, b = tmp_path / "MAW", tmp_path / "MAW_x"
    a.mkdir(); b.mkdir()
    monkeypatch.setenv("MAW_AGENTE_PROTEGIDAS", f"{a};{b}")
    assert config.pastas_protegidas() == [a.resolve(), b.resolve()]
