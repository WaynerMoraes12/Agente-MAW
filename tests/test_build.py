import sys
import time

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

def test_extrai_diagnosticos_sem_prefixo_de_no_e_sem_repetir_o_resumo():
    log = ("     1>c.cpp(7,2): error C2065: 'y': identificador não declarado [C:\\p\\App.vcxproj]\n"
           "Build FAILED.\n"
           "         c.cpp(7,2): error C2065: 'y': identificador não declarado [C:\\p\\App.vcxproj]\n")
    _, erros = build.extrair_diagnosticos(log)
    assert erros == ["c.cpp(7,2): error C2065: 'y': identificador não declarado"]

def test_caminho_do_exe(tmp_path):
    assert build.caminho_exe(tmp_path, "Release") == \
        tmp_path / "Builds" / "VisualStudio2022" / "x64" / "Release" / "App" / "MAW_APP.exe"

def test_localiza_msbuild():
    p = build.localizar_msbuild()
    assert p.name.lower() == "msbuild.exe" and p.exists()

def test_projeto_inexistente_nao_levanta(tmp_path):
    r = build.compilar("x", tmp_path, "Release", tmp_path / "logs")
    assert r.ok is False and r.erros and "não existe" in r.erros[0]


def _projeto(tmp_path):
    wt = tmp_path / "wt"
    proj_dir = wt / "Builds" / "VisualStudio2022"
    proj_dir.mkdir(parents=True)
    (proj_dir / "MAW_APP_App.vcxproj").write_text("<Project></Project>")
    return wt


def _msbuild_falso(tmp_path, log: str, console: str = "", codigo: int = 1, criar_exe: Path | None = None):
    """MSBuild de mentira: escreve `log` no arquivo pedido em /flp:logfile=..., imprime `console`
    e sai com `codigo`. Um .cmd repassa a linha de comando crua (%*) para um script Python."""
    script = tmp_path / "falso_msbuild.py"
    script.write_text(
        "import os, sys\n"
        f"LOG = {log!r}\nCONSOLE = {console!r}\nEXE = {str(criar_exe) if criar_exe else None!r}\n"
        "flp = next(a for a in sys.argv[1:] if a.lower().startswith('/flp:'))\n"
        "caminho = flp[len('/flp:logfile='):].split(';')[0]\n"
        "open(caminho, 'w', encoding='utf-8').write(LOG)\n"
        "print(CONSOLE)\n"
        "if EXE:\n"
        "    os.makedirs(os.path.dirname(EXE), exist_ok=True)\n"
        "    open(EXE, 'wb').write(b'MZ')\n"
        # exe 'antigo' (2000): como numa compilação incremental que não religa
        "    os.utime(EXE, (946684800, 946684800))\n"
        f"sys.exit({codigo})\n", encoding="utf-8")
    fake = tmp_path / "falso.cmd"
    fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n@exit /b %errorlevel%\r\n')
    return fake


def test_msbuild_nonzero_sem_erro_reconhecivel(tmp_path):
    """Fallback: MSBuild sai nonzero mas sem diagnostic reconhecível."""
    wt = _projeto(tmp_path)
    fake_key = "AIza" + "Z" * 35
    fake = _msbuild_falso(tmp_path, f"algo deu errado\n{fake_key}\n")
    r = build.compilar("x", wt, "Release", tmp_path / "logs", msbuild=fake)
    assert r.ok is False
    assert r.erros and any("código 1" in e for e in r.erros)
    log_content = (tmp_path / "logs" / "build-x-Release.log").read_text(encoding="utf-8")
    assert "[REDACTED]" in log_content and fake_key not in log_content


def test_msbuild_sem_reuso_de_no_e_log_em_arquivo(tmp_path, monkeypatch):
    wt = _projeto(tmp_path)
    chamadas = []

    def run_falso(args, **kw):
        chamadas.append((args, kw))
        return build.subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(build.subprocess, "run", run_falso)
    build.compilar("x", wt, "Release", tmp_path / "logs", msbuild=tmp_path / "msbuild.exe")
    [(args, kw)] = chamadas
    assert "/nodeReuse:false" in args
    log = tmp_path / "logs" / "build-x-Release.log"
    assert f"/flp:logfile={log};encoding=UTF-8;verbosity=normal" in args
    # nada de pipe: nós do MSBuild herdariam a ponta e o processo nunca "terminaria"
    assert "capture_output" not in kw
    assert kw.get("stdout") not in (None, build.subprocess.PIPE)


def test_diagnosticos_vem_do_arquivo_de_log_redigidos(tmp_path):
    wt = _projeto(tmp_path)
    chave = "AIza" + "Q" * 35
    fake = _msbuild_falso(
        tmp_path,
        f"  1>c.cpp(7,2): error C2065: 'y' {chave}: identificador não declarado [C:\\p\\App.vcxproj]\n",
        console="d.cpp(1,1): error C9999: so no console")
    r = build.compilar("x", wt, "Release", tmp_path / "logs", msbuild=fake)
    assert r.erros == ["c.cpp(7,2): error C2065: 'y' [REDACTED]: identificador não declarado"]


def test_build_ok_marca_o_exe_como_desta_compilacao(tmp_path):
    wt = _projeto(tmp_path)
    exe = build.caminho_exe(wt, "Release")
    fake = _msbuild_falso(tmp_path, "Build succeeded.\n", codigo=0, criar_exe=exe)
    antes = time.time() - 1
    r = build.compilar("x", wt, "Release", tmp_path / "logs", msbuild=fake)
    assert r.ok and exe.stat().st_mtime >= antes


@pytest.mark.lento
def test_compila_main_de_verdade():
    from maw_agent import config
    wt = config.ALVOS_DIR / "main"
    r = build.compilar("main", wt, "Release", config.WORK / "logs-teste")
    assert r.ok and Path(r.exe).exists()
