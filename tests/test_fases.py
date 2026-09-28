from maw_agent import achados, fases

ALVO = {"nome": "main", "commit": "a" * 40}

def test_achado_de_build_e_valido_e_critico():
    a = fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"], "log": "l.log"})
    assert achados.validar(a) == [] and a["severidade"] == "critica" and a["item_catalogo"] == "saude/build-release"

def test_achados_da_suite_por_bloco_incoerencia_e_jassert():
    r = {"blocos": [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0},
                    {"nome": "Bloco B", "sub": "y", "ok": 1, "falhas": 2}],
         "detalhes": ["- Bloco B / y", "!!! Test 1 failed: z"], "incoerencias": ["relatório diz TUDO PASSOU mas o código de saída foi 1"],
         "assercoes": ["JUCE Assertion failure in a.cpp:10"], "exit_code": 1, "declarado": "PASSOU"}
    lista = fases.achados_da_suite(ALVO, r)
    assert all(achados.validar(a) == [] for a in lista)
    tipos = sorted(a["tipo"] for a in lista)
    assert tipos == ["erro", "erro", "violacao"]

def test_resultados_suite_por_item():
    itens = [{"id": "g/grade", "cenarios": ["suite:Bloco A"]}, {"id": "g/b", "cenarios": ["suite:Bloco B"]},
             {"id": "g/c", "cenarios": ["suite:Inexistente"]}, {"id": "g/d", "cenarios": []}]
    blocos = [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0},
              {"nome": "Bloco B", "sub": "y", "ok": 1, "falhas": 1}]
    r = fases.resultados_suite_por_item(itens, blocos)
    assert r["g/grade"] == ("passou", None)
    assert r["g/b"][0] == "falhou"
    assert r["g/c"][0] == "nao_testavel" and "não encontrado" in r["g/c"][1]
    assert "g/d" not in r

def test_achado_de_build_sem_erros_reconheciveis_ainda_e_valido():
    a = fases.achado_de_build(ALVO, {"config": "Release", "erros": [], "log": "l.log"})
    assert achados.validar(a) == []
    assert "l.log" in a["obtido"]

def test_achados_da_suite_isola_detalhe_por_bloco_e_anexa_evidencia():
    r = {"blocos": [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0},
                    {"nome": "Bloco B", "sub": "y", "ok": 1, "falhas": 1},
                    {"nome": "Bloco C", "sub": "z", "ok": 1, "falhas": 1}],
         "detalhes": ["- Bloco B / y", "!!! Test 1 failed: mensagem exclusiva de B",
                      "- Bloco C / z", "!!! Test 1 failed: mensagem exclusiva de C"],
         "incoerencias": [], "assercoes": [], "exit_code": 1, "declarado": "FALHOU"}
    lista = fases.achados_da_suite(ALVO, r)
    assert all(achados.validar(a) == [] for a in lista)
    por_assinatura = {a["assinatura"]: a for a in lista}
    achado_b = por_assinatura["suite:Bloco B|y"]
    achado_c = por_assinatura["suite:Bloco C|z"]
    assert "mensagem exclusiva de B" in achado_b["obtido"] and "mensagem exclusiva de C" not in achado_b["obtido"]
    assert "mensagem exclusiva de C" in achado_c["obtido"] and "mensagem exclusiva de B" not in achado_c["obtido"]
    assert all(ev["arquivo"] == f"suites/{ALVO['nome']}.json" for a in lista for ev in a["evidencias"])

def test_separar_validos_rejeita_invalidos_e_relata_origem():
    valido = fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"], "log": "l.log"})
    invalido = {"tipo": "erro"}
    validos, mensagens = fases.separar_validos([("build-a.json[0]", valido), ("build-b.json[0]", invalido)])
    assert validos == [valido]
    assert len(mensagens) == 1
    assert mensagens[0].startswith("achado inválido em build-b.json[0]:")
