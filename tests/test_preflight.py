"""Tests para pre-voo e retrato do ambiente."""
import ctypes
import re
import subprocess
from pathlib import Path
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
    match = re.search(r"(\d+)x(\d+) a (\d+) dpi \((\d+)%\)", v.detalhe)
    assert match, f"Tela detail format invalido: {v.detalhe}"
    width, height, dpi, escala = map(int, match.groups())
    assert escala == dpi * 100 // 96, f"Escala {escala} != {dpi}*100//96"
    u = ctypes.windll.user32
    assert u.GetDpiForSystem() == dpi, "DPI nao eh fisico (processo nao DPI aware)"

def test_msbuild_timeout_nao_quebra(monkeypatch):
    """verificar_tudo() nao levanta se MSBuild timeout."""
    from maw_agent import build
    monkeypatch.setattr(build, "localizar_msbuild",
                       lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired("vswhere", 60)))
    vs = preflight.verificar_tudo()
    v = next(v for v in vs if v.nome == "msbuild")
    assert not v.ok
    assert isinstance(v.detalhe, str) and v.detalhe

def test_python310_detalhe_distinto(monkeypatch):
    """python310 detail distingue 'uv não encontrado' de 'Python 3.10 ausente'."""
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    py_path, py_detail = preflight._python310()
    assert py_path is None
    assert "uv não encontrado" in py_detail

def test_ambiente_tem_escala():
    """ambiente() inclui escala alem de dpi e resolucao."""
    amb = preflight.ambiente()
    assert "escala" in amb
    assert isinstance(amb["escala"], int)

def test_msbuild_chamado_uma_vez(monkeypatch):
    """_msbuild() chama localizar_msbuild() exatamente uma vez, nao duas."""
    from maw_agent import build
    
    call_count = [0]
    fake_path = Path(r"C:ake\MSBuild.exe")
    
    def mock_localizar():
        call_count[0] += 1
        return fake_path
    
    monkeypatch.setattr(build, "localizar_msbuild", mock_localizar)
    
    caminho, detalhe = preflight._msbuild()
    
    assert call_count[0] == 1, f"localizar_msbuild chamado {call_count[0]} vezes, esperado 1"
    assert caminho == str(fake_path)
    assert detalhe == str(fake_path)

def test_python310_ausente_com_uv_disponivel(monkeypatch):
    """_python310() retorna detalhe adequado quando uv existe mas nao encontra Python 3.10."""
    fake_uv = Path.home() / ".local" / "bin" / "uv.exe"
    
    monkeypatch.setattr("shutil.which", lambda x: None)
    
    original_exists = Path.exists
    def mock_exists(self):
        if str(self) == str(fake_uv):
            return True
        return original_exists(self)
    monkeypatch.setattr(Path, "exists", mock_exists)
    
    def mock_run(*args, **kwargs):
        class MockResult:
            returncode = 1
            stdout = ""
        return MockResult()
    
    monkeypatch.setattr("subprocess.run", mock_run)
    
    py_path, py_detail = preflight._python310()
    
    assert py_path is None
    assert "Python 3.10 ausente" in py_detail
    assert "uv python install 3.10" in py_detail
