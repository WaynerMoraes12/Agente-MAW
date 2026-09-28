"""Tests para pré-voo e retrato do ambiente."""
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
