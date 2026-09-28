"""Tests para o modulo de redacao de segredos."""
from maw_agent import redacao

CHAVE = "AIza" + "B" * 35


def test_redige_chave_google():
    assert redacao.redigir(f"key={CHAVE} fim") == "key=[REDACTED] fim"


def test_redige_tokens_github():
    t = "gho_" + "a" * 36
    assert "[REDACTED]" in redacao.redigir(f"Token: {t}")


def test_nao_mexe_em_texto_comum():
    assert redacao.redigir("RESULTADO: TUDO PASSOU.") == "RESULTADO: TUDO PASSOU."


def test_segredos_em_lista_os_tipos():
    assert redacao.segredos_em(f"x {CHAVE}") == ["chave-google"]
