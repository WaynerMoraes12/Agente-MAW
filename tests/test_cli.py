import subprocess, sys
from pathlib import Path

import pytest

import maw_agent
from maw_agent import config
from maw_agent.cli import main, _carregar_modulos, _SUBCOMANDOS


def _escrever_fase_temporaria(nome: str, conteudo: str) -> Path:
    caminho = Path(maw_agent.__file__).parent / f"{nome}.py"
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


def _limpar_fase_temporaria(nome: str, caminho: Path, subcomando: str | None = None) -> None:
    caminho.unlink(missing_ok=True)
    cache = caminho.parent / "__pycache__"
    if cache.exists():
        for f in cache.glob(f"{nome}.*"):
            f.unlink(missing_ok=True)
    sys.modules.pop(f"maw_agent.{nome}", None)
    if subcomando is not None:
        _SUBCOMANDOS.pop(subcomando, None)


def test_carregar_modulos_descobre_fase_dinamica():
    nome = "fase_teste_tmp"
    caminho = _escrever_fase_temporaria(
        nome,
        "from maw_agent.cli import registrar\n"
        "\n"
        "@registrar('teste-tmp', 'ajuda de teste', lambda p: None)\n"
        "def _executar(args):\n"
        "    return 0\n",
    )
    try:
        _carregar_modulos()
        assert f"maw_agent.{nome}" in sys.modules
        assert "teste-tmp" in _SUBCOMANDOS
    finally:
        _limpar_fase_temporaria(nome, caminho, "teste-tmp")


def test_carregar_modulos_propaga_erro_de_fase_com_import_quebrado():
    nome = "fase_quebrada_tmp"
    caminho = _escrever_fase_temporaria(nome, "import modulo_que_nao_existe_xyz_123\n")
    try:
        with pytest.raises(ModuleNotFoundError):
            _carregar_modulos()
    finally:
        _limpar_fase_temporaria(nome, caminho)


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
