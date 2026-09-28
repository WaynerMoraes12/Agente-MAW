import json

from maw_agent import achados, estado, fases

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

def test_catalogo_validar_exige_itens_de_saude(tmp_path, monkeypatch, capsys):
    import yaml
    from maw_agent import config
    cat = tmp_path / "f.yaml"
    cat.write_text(yaml.safe_dump([{"id": "a/b", "area": "a", "titulo": "t", "descricao": "d", "origem": ["x"],
                                    "verificacao": ["e2e"], "cenarios": [], "requisitos": [], "marco": "M2"}]),
                   encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    monkeypatch.setattr(config, "PRINCIPIOS", tmp_path / "nao.yaml")
    from maw_agent.cli import main
    assert main(["catalogo", "validar"]) == 1
    assert "saude/suite-existente" in capsys.readouterr().out

def test_catalogo_validar_exige_item_para_cada_principio(tmp_path, monkeypatch, capsys):
    import yaml
    from maw_agent import config
    cat = tmp_path / "f.yaml"
    obrigatorios = ["saude/build-release", "saude/build-debug", "saude/suite-existente", "saude/benchmark"]
    itens = [{"id": i, "area": "a", "titulo": "t", "descricao": "d", "origem": ["x"], "verificacao": ["e2e"],
             "cenarios": [], "requisitos": [], "marco": "M2"} for i in obrigatorios]
    cat.write_text(yaml.safe_dump(itens), encoding="utf-8")
    princ = tmp_path / "p.yaml"
    princ.write_text(yaml.safe_dump([{"id": "P1", "titulo": "t"}]), encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    monkeypatch.setattr(config, "PRINCIPIOS", princ)
    from maw_agent.cli import main
    assert main(["catalogo", "validar"]) == 1
    saida = capsys.readouterr().out
    assert "principio/P1" in saida

def test_catalogo_validar_ok_quando_tudo_presente(tmp_path, monkeypatch, capsys):
    import yaml
    from maw_agent import config
    cat = tmp_path / "f.yaml"
    obrigatorios = ["saude/build-release", "saude/build-debug", "saude/suite-existente", "saude/benchmark",
                    "principio/P1"]
    itens = [{"id": i, "area": "a", "titulo": "t", "descricao": "d", "origem": ["x"], "verificacao": ["e2e"],
             "cenarios": [], "requisitos": [], "marco": "M2"} for i in obrigatorios]
    cat.write_text(yaml.safe_dump(itens), encoding="utf-8")
    princ = tmp_path / "p.yaml"
    princ.write_text(yaml.safe_dump([{"id": "P1", "titulo": "t"}]), encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    monkeypatch.setattr(config, "PRINCIPIOS", princ)
    from maw_agent.cli import main
    assert main(["catalogo", "validar"]) == 0
    saida = json.loads(capsys.readouterr().out)
    assert saida == {"itens": 5, "principios": 1, "erros": []}

def test_juntar_reverificacoes_sem_arquivos(tmp_path):
    assert fases.juntar_reverificacoes(tmp_path) == ({}, [])

def test_juntar_reverificacoes_junta_dois_arquivos(tmp_path):
    (tmp_path / "reverificacoes.json").write_text(json.dumps({"MAW-0001": "corrigido"}), encoding="utf-8")
    (tmp_path / "reverificacoes-testador-motor.json").write_text(json.dumps({"MAW-0002": "persiste"}),
                                                                 encoding="utf-8")
    rever, conflitos = fases.juntar_reverificacoes(tmp_path)
    assert rever == {"MAW-0001": "corrigido", "MAW-0002": "persiste"}
    assert conflitos == []

def test_juntar_reverificacoes_mesmo_valor_nao_e_conflito(tmp_path):
    (tmp_path / "reverificacoes-a.json").write_text(json.dumps({"MAW-0005": "corrigido"}), encoding="utf-8")
    (tmp_path / "reverificacoes-b.json").write_text(json.dumps({"MAW-0005": "corrigido"}), encoding="utf-8")
    rever, conflitos = fases.juntar_reverificacoes(tmp_path)
    assert rever == {"MAW-0005": "corrigido"} and conflitos == []

def test_juntar_reverificacoes_conflito_vence_mais_conservador(tmp_path):
    (tmp_path / "reverificacoes-testador-motor.json").write_text(json.dumps({"MAW-0003": "corrigido"}),
                                                                  encoding="utf-8")
    (tmp_path / "reverificacoes-guardiao-da-ideia.json").write_text(json.dumps({"MAW-0003": "persiste"}),
                                                                     encoding="utf-8")
    rever, conflitos = fases.juntar_reverificacoes(tmp_path)
    assert rever == {"MAW-0003": "persiste"}
    assert len(conflitos) == 1
    msg = conflitos[0]
    assert "MAW-0003" in msg and "testador-motor" in msg and "guardiao-da-ideia" in msg
    assert "corrigido" in msg and msg.strip().endswith("persiste")

def test_juntar_reverificacoes_ordem_completa_de_conservadorismo(tmp_path):
    (tmp_path / "reverificacoes-a.json").write_text(json.dumps({"MAW-0001": "corrigido"}), encoding="utf-8")
    (tmp_path / "reverificacoes-b.json").write_text(json.dumps({"MAW-0001": "nao_verificavel"}), encoding="utf-8")
    rever, _ = fases.juntar_reverificacoes(tmp_path)
    assert rever["MAW-0001"] == "nao_verificavel"

def test_juntar_reverificacoes_valor_invalido_ignorado_e_relatado(tmp_path):
    (tmp_path / "reverificacoes-a.json").write_text(json.dumps({"MAW-0004": "talvez"}), encoding="utf-8")
    rever, conflitos = fases.juntar_reverificacoes(tmp_path)
    assert rever == {}
    assert len(conflitos) == 1 and "MAW-0004" in conflitos[0] and "talvez" in conflitos[0]

def test_juntar_reverificacoes_atribuicao_correta_em_conflito_de_tres(tmp_path):
    # a: corrigido; b: persiste (vence e passa a ser a origem do valor atual);
    # c: corrigido de novo, não vence -- a mensagem deve continuar atribuindo 'persiste' a 'b', não a 'c'.
    (tmp_path / "reverificacoes-a.json").write_text(json.dumps({"MAW-0009": "corrigido"}), encoding="utf-8")
    (tmp_path / "reverificacoes-b.json").write_text(json.dumps({"MAW-0009": "persiste"}), encoding="utf-8")
    (tmp_path / "reverificacoes-c.json").write_text(json.dumps({"MAW-0009": "corrigido"}), encoding="utf-8")
    rever, conflitos = fases.juntar_reverificacoes(tmp_path)
    assert rever == {"MAW-0009": "persiste"}
    assert len(conflitos) == 2
    assert "a diz corrigido, b diz persiste" in conflitos[0]
    assert "b diz persiste, c diz corrigido — ficou persiste" in conflitos[1]

def test_consolidar_junta_reverificacoes_grava_e_relata_conflito(tmp_path, monkeypatch):
    from maw_agent import config
    monkeypatch.setattr(config, "HISTORICO", tmp_path / "historico.json")
    e1 = estado.Estado(tmp_path / "sprint-01", 1)
    (e1.pasta / "achados-brutos").mkdir(parents=True)
    achado = fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"], "log": "l.log"})
    (e1.pasta / "achados-brutos" / "a.json").write_text(json.dumps(achado), encoding="utf-8")
    monkeypatch.setattr(fases, "_sprint_atual", lambda: e1)
    fases._consolidar(None)

    e2 = estado.Estado(tmp_path / "sprint-02", 2)
    (e2.pasta / "achados-brutos").mkdir(parents=True)
    (e2.pasta / "reverificacoes-testador-motor.json").write_text(json.dumps({"MAW-0001": "corrigido"}),
                                                                  encoding="utf-8")
    (e2.pasta / "reverificacoes-guardiao-da-ideia.json").write_text(json.dumps({"MAW-0001": "persiste"}),
                                                                     encoding="utf-8")
    monkeypatch.setattr(fases, "_sprint_atual", lambda: e2)
    fases._consolidar(None)

    gravado = json.loads((e2.pasta / "reverificacoes.json").read_text(encoding="utf-8"))
    assert gravado == {"MAW-0001": "persiste"}
    erros = json.loads((e2.pasta / "erros_agente.json").read_text(encoding="utf-8"))
    assert any("MAW-0001" in m for m in erros)
    [a] = json.loads((e2.pasta / "achados.json").read_text(encoding="utf-8"))
    assert a["estado"] == "aberto"
