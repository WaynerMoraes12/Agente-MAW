"""Tests para pré-voo e retrato do ambiente."""
import ctypes
import re
import subprocess
from maw_agent import preflight

NOMES = {"maw_fechada", "msbuild", "edge", "disco", "vb_cable", "python310", "ffmpeg",
         "midi_loopback", "nao_perturbe", "tela"}

def test_todas_as_verificacoes_presentes():
    vs = preflight.verificar_tudo()
    assert {v.nome for v in vs} == NOMES
    assert all(isinstance(v.detalhe, str) and v.detalhe for v in vs)

def test_requisitos_ausentes_une_afeta():
    vs = [preflight.Verificacao("a", False, "x", False, ["vb-cable"]),
          preflight.Verificacao("b", True, "x", False, ["gui"]),
          preflight.Verificacao("c", False, "x", False, ["python310", "gui"])]
    assert preflight.requisitos_ausentes(vs) == {"vb-cable", "python310", "gui"}

def test_maw_aberta_bloqueia_gui(monkeypatch):
    monkeypatch.setattr(preflight, "_processos", lambda: ["explorer.exe", "MAW_APP.exe"])
    v = next(v for v in preflight.verificar_tudo() if v.nome == "maw_fechada")
    assert not v.ok and "gui" in v.afeta

def test_ambiente_tem_campos():
    amb = preflight.ambiente()
    for k in ("windows", "python", "cpu", "ram_gb", "dpi", "resolucao", "dispositivos_audio", "portas_midi"):
        assert k in amb

def test_dpi_escala_consistente():
    """Tela detail tem DPI real e escala consistente."""
    vs = preflight.verificar_tudo()
    v = next(v for v in vs if v.nome == "tela")
    # Detail tem formato: "WIDTHxHEIGHT a DPI dpi (SCALE%)"
    # Ex: "1920x1080 a 120 dpi (125%)"
    match = re.search(r"(\d+)x(\d+) a (\d+) dpi \((\d+)%\)", v.detalhe)
    assert match, f"Tela detail format inválido: {v.detalhe}"
    width, height, dpi, escala = map(int, match.groups())
    # Verificar que escala é consistente: dpi*100//96
    assert escala == dpi * 100 // 96, f"Escala {escala} != {dpi}*100//96"
    # Verificar que GetDpiForSystem() após awareness call retorna o DPI reportado
    u = ctypes.windll.user32
    assert u.GetDpiForSystem() == dpi, "DPI não é físico (processo não DPI aware)"

def test_msbuild_timeout_nao_quebra(monkeypatch):
    """verificar_tudo() não levanta se MSBuild timeout."""
    from maw_agent import build
    monkeypatch.setattr(build, "localizar_msbuild",
                       lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired("vswhere", 60)))
    # Não deve levantar
    vs = preflight.verificar_tudo()
    v = next(v for v in vs if v.nome == "msbuild")
    assert not v.ok
    assert isinstance(v.detalhe, str) and v.detalhe

def test_python310_detalhe_distinto(monkeypatch):
    """python310 detail distingue 'uv não encontrado' de 'Python 3.10 ausente'."""
    # Simular uv não encontrado
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    py_path, py_detail = preflight._python310()
    assert py_path is None
    assert "uv não encontrado" in py_detail

def test_ambiente_tem_escala():
    """ambiente() inclui escala além de dpi e resolucao."""
    amb = preflight.ambiente()
    assert "escala" in amb
    assert isinstance(amb["escala"], int)
