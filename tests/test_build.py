import pytest
from pathlib import Path
from maw_agent import build

LOG = """
a.cpp(10,5): warning C4100: 'x': parâmetro formal não referenciado [C:\\p\\App.vcxproj]
a.cpp(10,5): warning C4100: 'x': parâmetro formal não referenciado [C:\\p\\App.vcxproj]
b.cpp(3,1): aviso C4244: conversão de 'double' para 'float' [C:\\p\\App.vcxproj]
c.cpp(7,2): error C2065: 'y': identificador não declarado [C:\\p\\App.vcxproj]
LINK : fatal error LNK1104: não é possível abrir o arquivo 'App.exe'
"""

def test_extrai_avisos_e_erros_sem_repetir():
    avisos, erros = build.extrair_diagnosticos(LOG)
    assert len(avisos) == 2 and "C4100" in avisos[0] and "C4244" in avisos[1]
    assert len(erros) == 2 and "C2065" in erros[0] and "LNK1104" in erros[1]

def test_caminho_do_exe(tmp_path):
    assert build.caminho_exe(tmp_path, "Release") == \
        tmp_path / "Builds" / "VisualStudio2022" / "x64" / "Release" / "App" / "MAW_APP.exe"

def test_localiza_msbuild():
    p = build.localizar_msbuild()
    assert p.name.lower() == "msbuild.exe" and p.exists()

def test_projeto_inexistente_nao_levanta(tmp_path):
    r = build.compilar("x", tmp_path, "Release", tmp_path / "logs")
    assert r.ok is False and r.erros and "não existe" in r.erros[0]

@pytest.mark.lento
def test_compila_main_de_verdade():
    from maw_agent import config
    wt = config.ALVOS_DIR / "main"
    r = build.compilar("main", wt, "Release", config.WORK / "logs-teste")
    assert r.ok and Path(r.exe).exists()
