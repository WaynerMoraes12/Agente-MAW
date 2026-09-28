"""Tests para a captura de OutputDebugString (protocolo DBWIN) usada para pegar jassert."""
import ctypes, os, time
from maw_agent import saude


def test_captura_outputdebugstring_do_proprio_processo():
    with saude.CapturaDepuracao() as cap:
        time.sleep(0.2)
        ctypes.windll.kernel32.OutputDebugStringW("JUCE Assertion failure in x.cpp:10")
        time.sleep(0.5)
    assert cap.erro is None
    textos = saude.filtrar(cap.mensagens, os.getpid())
    assert saude.assercoes(textos) == ["JUCE Assertion failure in x.cpp:10"]


def test_assercoes_ignora_resto():
    assert saude.assercoes(["oi", "JUCE Assertion failure in a.h:1"]) == ["JUCE Assertion failure in a.h:1"]
