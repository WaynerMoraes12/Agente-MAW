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

def test_msbuild_nonzero_sem_erro_reconhecivel(tmp_path):
    """Fallback: MSBuild sai nonzero mas sem diagnostic reconhecível."""
    wt = tmp_path / "wt"
    proj_dir = wt / "Builds" / "VisualStudio2022"
    proj_dir.mkdir(parents=True)
    (proj_dir / "MAW_APP_App.vcxproj").write_text("<Project></Project>")

    fake_key = "AIza" + "Z" * 35
    fake_msbuild = tmp_path / "falso.cmd"
    fake_msbuild.write_text(f"@echo algo deu errado\r\n@echo {fake_key}\r\n@exit /b 1")

    r = build.compilar("x", wt, "Release", tmp_path / "logs", msbuild=fake_msbuild)
    assert r.ok is False
    assert r.erros and any("código 1" in e for e in r.erros)
    assert (tmp_path / "logs" / "build-x-Release.log").exists()
    log_content = (tmp_path / "logs" / "build-x-Release.log").read_text()
    assert "[REDACTED]" in log_content and fake_key not in log_content

@pytest.mark.lento
def test_compila_main_de_verdade():
    from maw_agent import config
    wt = config.ALVOS_DIR / "main"
    r = build.compilar("main", wt, "Release", config.WORK / "logs-teste")
    assert r.ok and Path(r.exe).exists()
