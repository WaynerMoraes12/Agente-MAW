import json
from datetime import datetime

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


BLOCOS = [{"nome": "Bloco A", "sub": "x", "ok": 3, "falhas": 0},
          {"nome": "Bloco A", "sub": "y", "ok": 2, "falhas": 0},
          {"nome": "Bloco B", "sub": "z", "ok": 1, "falhas": 1},
          {"nome": "Bloco A em lote", "sub": "w", "ok": 1, "falhas": 1}]


def _item(verificacao, cenarios, marco="M2"):
    return {"id": "area/x", "verificacao": verificacao, "cenarios": cenarios, "marco": marco}


def test_suite_casa_bloco_por_nome_exato():
    # "suite:Bloco A" não pode casar "Bloco A em lote" (que falhou)
    r = fases.resultados_suite_por_item([_item(["suite"], ["suite:Bloco A"])], BLOCOS)
    assert r["area/x"] == ("passou", None)


def test_suite_bloco_referenciado_falhou_e_falhou():
    r = fases.resultados_suite_por_item([_item(["suite", "e2e"], ["suite:Bloco A", "suite:Bloco B"])], BLOCOS)
    assert r["area/x"] == ("falhou", None)


def test_suite_so_passa_quando_toda_verificacao_foi_coberta():
    r = fases.resultados_suite_por_item([_item(["suite"], ["suite:Bloco A"]),
                                         {**_item(["suite", "benchmark"], ["suite:Bloco A"]), "id": "area/y"}],
                                        BLOCOS, cobertas=("suite", "benchmark"))
    assert r["area/x"] == ("passou", None) and r["area/y"] == ("passou", None)


def test_suite_passou_mas_falta_verificacao_vira_parcial():
    r = fases.resultados_suite_por_item([_item(["e2e", "suite", "revisao"], ["suite:Bloco A"], "M3")], BLOCOS)
    assert r["area/x"] == ("nao_testavel",
                           "parcial: blocos da suíte passaram (Bloco A); e2e, revisao previstas para o marco M3")
    # benchmark só conta quando rodou nesta sprint
    r = fases.resultados_suite_por_item([_item(["suite", "benchmark"], ["suite:Bloco A"], "M1")], BLOCOS)
    assert r["area/x"][0] == "nao_testavel" and "benchmark previstas para o marco M1" in r["area/x"][1]


def test_suite_classe_ausente_e_nao_testavel_mesmo_com_outra_passando():
    r = fases.resultados_suite_por_item([_item(["suite"], ["suite:Bloco A", "suite:Sumiu", "suite:Tambem"])], BLOCOS)
    assert r["area/x"] == ("nao_testavel", "bloco da suíte não encontrado: Sumiu, Tambem")


# ---------- C2: só o binário desta sprint ----------
import os
import time as _time


def test_binario_valido(tmp_path):
    exe = tmp_path / "App.exe"
    inicio = "2026-09-28T10:00:00"
    t_inicio = _time.mktime(_time.strptime(inicio, "%Y-%m-%dT%H:%M:%S"))
    assert fases.binario_valido(None, exe, inicio)[0] is False
    assert fases.binario_valido({"ok": False}, exe, inicio) == (False, "build falhou nesta sprint")
    assert fases.binario_valido({"ok": True}, exe, inicio)[0] is False  # exe ausente
    exe.write_bytes(b"MZ")
    os.utime(exe, (t_inicio - 3600, t_inicio - 3600))  # binário de uma sprint anterior
    ok, motivo = fases.binario_valido({"ok": True}, exe, inicio)
    assert not ok and "anterior" in motivo
    assert fases.binario_valido({"ok": True}, exe, None)[0] is False
    os.utime(exe, (t_inicio, t_inicio))  # mesmo segundo do início: vale
    assert fases.binario_valido({"ok": True}, exe, inicio) == (True, "")


# ---------- menor: assinatura do erro de build ----------

def test_assinatura_de_build_sem_worktree_e_sem_linha_coluna():
    a = fases.achado_de_build({"nome": "main", "commit": "a" * 40},
                              {"config": "Release", "log": "l.log",
                               "erros": [r"C:\w\alvos\main\Source\c.cpp(7,2): error C2065: 'y': não declarado"]})
    b = fases.achado_de_build({"nome": "feature-x", "commit": "b" * 40},
                              {"config": "Release", "log": "l.log",
                               "erros": [r"D:\outro dir\alvos\feature-x\Source\c.cpp(9,14): error C2065: 'y': não declarado"]})
    assert a["assinatura"] == b["assinatura"] == "build:Release:C2065:c.cpp:'y': não declarado"
    assert achados.impressao_digital(a) == achados.impressao_digital(b)


def test_assinatura_de_erro_de_link_tira_o_caminho_da_mensagem():
    assert fases.assinatura_erro_build(
        r"LINK : fatal error LNK1104: não é possível abrir o arquivo 'C:\Agente X\work\alvos\main\x64\App.exe'") \
        == "LNK1104:LINK:não é possível abrir o arquivo 'App.exe'"


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

def test_catalogo_publicar_sem_rascunho_recusa(tmp_path, monkeypatch, capsys):
    from maw_agent import config
    monkeypatch.setattr(config, "CATALOGO", tmp_path / "f.yaml")
    from maw_agent.cli import main
    assert main(["catalogo", "publicar"]) == 1
    assert "rascunho" in capsys.readouterr().out

def test_catalogo_publicar_recusa_yaml_com_erro_de_sintaxe_e_preserva_atual(tmp_path, monkeypatch, capsys):
    from maw_agent import config
    cat = tmp_path / "f.yaml"
    original = "conteudo: preservado\n"
    cat.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    monkeypatch.setattr(config, "PRINCIPIOS", tmp_path / "nao.yaml")
    monkeypatch.setattr(fases.catalogo.time, "sleep", lambda s: None)  # não esperar de verdade no teste
    rascunho = tmp_path / "rascunho.yaml"
    rascunho.write_text(":\n  - quebrado [", encoding="utf-8")
    from maw_agent.cli import main
    assert main(["catalogo", "publicar", str(rascunho)]) == 1
    assert cat.read_text(encoding="utf-8") == original

def test_catalogo_publicar_recusa_catalogo_estruturalmente_invalido_e_preserva_atual(tmp_path, monkeypatch, capsys):
    import yaml
    from maw_agent import config
    cat = tmp_path / "f.yaml"
    original = yaml.safe_dump([{"id": "a/b", "area": "a", "titulo": "t", "descricao": "d", "origem": ["x"],
                                "verificacao": ["e2e"], "cenarios": [], "requisitos": [], "marco": "M2"}])
    cat.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    monkeypatch.setattr(config, "PRINCIPIOS", tmp_path / "nao.yaml")
    rascunho = tmp_path / "rascunho.yaml"
    rascunho.write_text(yaml.safe_dump([{"id": "a/dup"}, {"id": "a/dup"}]), encoding="utf-8")
    from maw_agent.cli import main
    assert main(["catalogo", "publicar", str(rascunho)]) == 1
    saida = capsys.readouterr().out
    assert "duplicado" in saida
    assert cat.read_text(encoding="utf-8") == original

def test_catalogo_publicar_troca_o_catalogo_atomicamente_quando_valido(tmp_path, monkeypatch, capsys):
    import yaml
    from maw_agent import config
    cat = tmp_path / "f.yaml"
    cat.write_text("conteudo: antigo\n", encoding="utf-8")
    princ = tmp_path / "p.yaml"
    princ.write_text(yaml.safe_dump([{"id": "P1", "titulo": "t"}]), encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    monkeypatch.setattr(config, "PRINCIPIOS", princ)
    obrigatorios = ["saude/build-release", "saude/build-debug", "saude/suite-existente", "saude/benchmark",
                    "principio/P1"]
    itens = [{"id": i, "area": "a", "titulo": "t", "descricao": "d", "origem": ["x"], "verificacao": ["e2e"],
             "cenarios": [], "requisitos": [], "marco": "M2"} for i in obrigatorios]
    rascunho = tmp_path / "rascunho.yaml"
    rascunho.write_text(yaml.safe_dump(itens, allow_unicode=True), encoding="utf-8")
    from maw_agent.cli import main
    assert main(["catalogo", "publicar", str(rascunho)]) == 0
    saida = json.loads(capsys.readouterr().out)
    assert saida["ok"] is True and saida["itens"] == 5
    assert yaml.safe_load(cat.read_text(encoding="utf-8")) == itens

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



def test_juntar_reverificacoes_com_extras_e_sem_base(tmp_path):
    (tmp_path / "reverificacoes.json").write_text(json.dumps({"MAW-0001": "persiste"}), encoding="utf-8")
    (tmp_path / "reverificacoes-a.json").write_text(json.dumps({"MAW-0002": "corrigido"}), encoding="utf-8")
    rever, conflitos = fases.juntar_reverificacoes(tmp_path, incluir_base=False,
                                                   extras=[("automatico", {"MAW-0002": "nao_verificavel"})])
    assert rever == {"MAW-0002": "nao_verificavel"}
    assert conflitos == ["MAW-0002: automatico diz nao_verificavel, a diz corrigido — ficou nao_verificavel"]


def test_juntar_reverificacoes_arquivo_ilegivel_nao_quebra(tmp_path):
    (tmp_path / "reverificacoes-a.json").write_text("{quebrado", encoding="utf-8")
    (tmp_path / "reverificacoes-b.json").write_text(json.dumps(["lista"]), encoding="utf-8")
    rever, msgs = fases.juntar_reverificacoes(tmp_path)
    assert rever == {} and len(msgs) == 2 and all("ilegíveis" in m for m in msgs)


# ---------- I5: reverificação dos achados automáticos ----------

def _hist(*regs):
    return {"proximo": 10, "itens": {f"imp{i}": r for i, r in enumerate(regs)}}


def _reg(id_, fonte, assinatura, alvos_=("main",), estado_="aberto", item="saude/suite-existente"):
    return {"id": id_, "estado": estado_, "historico": ["01"],
            "ultimo": {"fonte": fonte, "assinatura": assinatura, "item_catalogo": item,
                       "alvos": [{"alvo": a, "commit": "c"} for a in alvos_]}}


ALVOS_SPRINT = [{"nome": "main", "commit": "a" * 40, "compartilha_com": None},
                {"nome": "docs-z", "commit": "d" * 40, "compartilha_com": "main"}]


def _gravar(pasta, rel, obj):
    (pasta / rel).parent.mkdir(parents=True, exist_ok=True)
    (pasta / rel).write_text(json.dumps(obj), encoding="utf-8")


def test_reverificacao_automatica_de_build(tmp_path):
    h = _hist(_reg("MAW-0001", "build", "build:Release:C2065:c.cpp:x", item="saude/build-release"),
              _reg("MAW-0002", "build", "build:Debug:C2065:c.cpp:x", item="saude/build-debug"),
              _reg("MAW-0003", "build", "build:Release:C1:x.cpp:y", alvos_=("sumiu",), item="saude/build-release"))
    _gravar(tmp_path, "builds/main-Release.json", {"ok": True})
    _gravar(tmp_path, "builds/main-Debug.json", {"ok": False, "erros": ["outro erro"]})
    r = fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path)
    # Release compilou: o critério de aceite passa. Debug falhou por outro motivo: não dá para dizer.
    assert r == {"MAW-0001": "corrigido", "MAW-0002": "nao_verificavel"}  # MAW-0003: alvo não existe nesta sprint


def test_reverificacao_automatica_de_build_que_nao_rodou(tmp_path):
    h = _hist(_reg("MAW-0001", "build", "build:Release:C2065:c.cpp:x", item="saude/build-release"))
    assert fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path) == {"MAW-0001": "nao_verificavel"}


def test_reverificacao_automatica_da_suite(tmp_path):
    h = _hist(_reg("MAW-0001", "suite", "suite:Bloco A|x"),
              _reg("MAW-0002", "suite", "suite:Bloco B|y"),
              _reg("MAW-0003", "suite", "suite:Bloco Sumido|z"),
              _reg("MAW-0004", "suite", "jassert:JUCE Assertion failure in a.cpp:10"),
              _reg("MAW-0005", "suite", "suite-incoerente:totais ausentes"),
              _reg("MAW-0006", "suite", "suite:Bloco A|x", estado_="corrigido"))
    _gravar(tmp_path, "suites/main.json", {"blocos": [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0},
                                                      {"nome": "Bloco B", "sub": "y", "ok": 0, "falhas": 1}],
                                           "incoerencias": [], "assercoes": [], "captura_erro": None})
    r = fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path)
    assert r == {"MAW-0001": "corrigido", "MAW-0002": "persiste", "MAW-0003": "nao_verificavel",
                 "MAW-0004": "corrigido", "MAW-0005": "corrigido"}


def test_reverificacao_automatica_jassert_sem_captura_nao_e_corrigido(tmp_path):
    h = _hist(_reg("MAW-0004", "suite", "jassert:JUCE Assertion failure in a.cpp:10"))
    _gravar(tmp_path, "suites/main.json", {"blocos": [], "incoerencias": [], "assercoes": [],
                                           "captura_erro": "outro ouvinte ativo"})
    assert fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path) == {"MAW-0004": "nao_verificavel"}


def test_reverificacao_automatica_jassert_com_outra_assercao_nao_e_corrigido(tmp_path):
    h = _hist(_reg("MAW-0004", "suite", "jassert:JUCE Assertion failure in a.cpp:10"),
              _reg("MAW-0005", "suite", "jassert:JUCE Assertion failure in b.cpp:3"))
    _gravar(tmp_path, "suites/main.json", {"blocos": [], "incoerencias": [], "captura_erro": None,
                                           "assercoes": ["JUCE Assertion failure in b.cpp:4"]})
    r = fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path)
    assert r == {"MAW-0004": "nao_verificavel", "MAW-0005": "persiste"}


def test_reverificacao_automatica_alvo_que_compartilha_usa_a_origem(tmp_path):
    h = _hist(_reg("MAW-0001", "suite", "suite:Bloco A|x", alvos_=("docs-z",)),
              _reg("MAW-0002", "suite", "suite:Bloco A|x", alvos_=("main", "docs-z", "sumiu")))
    _gravar(tmp_path, "suites/main.json", {"blocos": [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0}],
                                           "incoerencias": [], "assercoes": [], "captura_erro": None})
    r = fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path)
    assert r == {"MAW-0001": "corrigido", "MAW-0002": "corrigido"}


def test_reverificacao_automatica_combina_alvos_pelo_mais_conservador(tmp_path):
    alvos_ = [{"nome": "main", "commit": "a", "compartilha_com": None},
              {"nome": "feature-x", "commit": "b", "compartilha_com": None}]
    h = _hist(_reg("MAW-0001", "suite", "suite:Bloco A|x", alvos_=("main", "feature-x")))
    _gravar(tmp_path, "suites/main.json", {"blocos": [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0}],
                                           "incoerencias": [], "assercoes": [], "captura_erro": None})
    assert fases.reverificacoes_automaticas(h, alvos_, tmp_path) == {"MAW-0001": "nao_verificavel"}


# ---------- I6: veredito ausente ou ilegível ----------

def test_ler_brutos_vereditos(tmp_path):
    # achado de julgamento (de subagente): sem veredito legível cai para provável
    bruto = dict(fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"],
                                              "log": "l.log"}), fonte="testador-motor")
    outro = dict(bruto, assinatura="outra")
    _gravar(tmp_path, "achados-brutos/a.json", bruto)              # sem veredito
    _gravar(tmp_path, "achados-brutos/b.json", [bruto, outro])     # lista de vereditos curta
    _gravar(tmp_path, "vereditos/b.json", [{"resultado": "confirmado", "justificativa": "ok"}])
    _gravar(tmp_path, "achados-brutos/c.json", bruto)              # veredito sem 'resultado'
    _gravar(tmp_path, "vereditos/c.json", {"justificativa": "esqueci"})
    _gravar(tmp_path, "achados-brutos/d.json", bruto)              # veredito com JSON inválido
    (tmp_path / "vereditos" / "d.json").write_text("{nao e json", encoding="utf-8")
    _gravar(tmp_path, "achados-brutos/e.json", bruto)              # veredito legível
    _gravar(tmp_path, "vereditos/e.json", {"resultado": "provavel", "justificativa": "x"})
    (tmp_path / "achados-brutos" / "f.json").write_text("[quebrado", encoding="utf-8")  # bruto ilegível
    entradas, sem_verificacao, msgs = fases.ler_brutos(tmp_path)
    por_origem = {o: a for o, a in entradas}
    assert set(por_origem) == {"a.json[0]", "b.json[0]", "b.json[1]", "c.json[0]", "d.json[0]", "e.json[0]"}
    sem = {o for o, a in entradas if id(a) in sem_verificacao}
    assert sem == {"a.json[0]", "b.json[1]", "c.json[0]", "d.json[0]"}
    assert all(por_origem[o]["confianca"] == "provavel" and por_origem[o]["veredito"] is None for o in sem)
    assert por_origem["b.json[0]"]["veredito"]["resultado"] == "confirmado"
    assert por_origem["e.json[0]"]["veredito"]["resultado"] == "provavel"
    assert any("b.json[1]" in m for m in msgs) and any("c.json[0]" in m for m in msgs)
    assert any("d.json" in m for m in msgs) and any("f.json" in m for m in msgs)
    assert not any("a.json" in m for m in msgs)  # só ausente: vai para o contador, não para os erros


# ---------- I9: referências do achado ----------

def test_conferir_referencias():
    a = dict(fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"],
                                          "log": "l.log"}), fonte="testador-motor")
    ids = {"saude/build-release"}
    alvos_ = {"main": "a" * 40}
    assert fases.conferir_referencias([a], ids, alvos_) == []
    assert fases.conferir_referencias([a], None, alvos_) == []  # sem catálogo, não confere o item
    erros = fases.conferir_referencias([dict(a, item_catalogo="nao/existe")], ids, alvos_)
    assert len(erros) == 1 and "nao/existe" in erros[0] and "catálogo" in erros[0]
    erros = fases.conferir_referencias([dict(a, alvos=[{"alvo": "feature/x", "commit": "a" * 40}])], ids, alvos_)
    assert len(erros) == 1 and "feature/x" in erros[0] and "alvos.json" in erros[0]
    erros = fases.conferir_referencias([dict(a, alvos=[{"alvo": "main", "commit": "b" * 40}])], ids, alvos_)
    assert len(erros) == 1 and "commit" in erros[0]
    assert "alvos.json" in fases.conferir_referencias([a], ids, None)[0]


def test_achado_mecanico_de_build_ou_suite_nao_depende_de_veredito(tmp_path):
    build_ = fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"], "log": "l.log"})
    suite_ = fases.achados_da_suite(ALVO, {"blocos": [{"nome": "Bloco B", "sub": "y", "ok": 0, "falhas": 1}],
                                           "detalhes": [], "incoerencias": [], "assercoes": []})[0]
    julgamento = dict(suite_, fonte="testador-motor", assinatura="Source/a.cpp::f::condicao")
    _gravar(tmp_path, "achados-brutos/suite-main-000.json", suite_)          # sem veredito
    _gravar(tmp_path, "achados-brutos/build-main-Release.json", build_)      # veredito ilegível
    (tmp_path / "vereditos").mkdir(parents=True, exist_ok=True)
    (tmp_path / "vereditos" / "build-main-Release.json").write_text("{quebrado", encoding="utf-8")
    _gravar(tmp_path, "achados-brutos/suite-main-001.json", dict(suite_, assinatura="suite:Bloco C|z"))
    _gravar(tmp_path, "vereditos/suite-main-001.json", {"resultado": "derrubado", "justificativa": "x"})
    _gravar(tmp_path, "achados-brutos/testador-motor-main-001.json", julgamento)  # sem veredito
    entradas, sem_verificacao, msgs = fases.ler_brutos(tmp_path)
    por_origem = {o: a for o, a in entradas}
    assert por_origem["suite-main-000.json[0]"]["confianca"] == "confirmado"
    assert por_origem["build-main-Release.json[0]"]["confianca"] == "confirmado"
    assert por_origem["suite-main-001.json[0]"]["veredito"]["resultado"] == "derrubado"  # veredito vale igual
    assert por_origem["testador-motor-main-001.json[0]"]["confianca"] == "provavel"
    sem = {o for o, a in entradas if id(a) in sem_verificacao}
    assert sem == {"testador-motor-main-001.json[0]"}
    assert any("build-main-Release.json" in m for m in msgs)  # o veredito ilegível continua declarado


def test_ler_brutos_veredito_confirmado_promove_confianca(tmp_path):
    bruto = dict(fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"],
                                              "log": "l.log"}), fonte="testador-motor", confianca="provavel")
    _gravar(tmp_path, "achados-brutos/promove.json", bruto)
    _gravar(tmp_path, "vereditos/promove.json", {"resultado": "confirmado", "justificativa": "reproduzi"})
    entradas, _, _ = fases.ler_brutos(tmp_path)
    por_origem = {o: a for o, a in entradas}
    assert por_origem["promove.json[0]"]["confianca"] == "confirmado"


def test_ler_brutos_veredito_provavel_rebaixa_confianca(tmp_path):
    bruto = dict(fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"],
                                              "log": "l.log"}), fonte="testador-motor", confianca="confirmado")
    _gravar(tmp_path, "achados-brutos/rebaixa.json", bruto)
    _gravar(tmp_path, "vereditos/rebaixa.json", {"resultado": "provavel", "justificativa": "não reproduzi bem"})
    entradas, _, _ = fases.ler_brutos(tmp_path)
    por_origem = {o: a for o, a in entradas}
    assert por_origem["rebaixa.json[0]"]["confianca"] == "provavel"


def test_conferir_referencias_recusa_fonte_mecanica():
    a = fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"], "log": "l.log"})
    for fonte in ("build", "suite"):
        erros = fases.conferir_referencias([dict(a, fonte=fonte)], {"saude/build-release"}, {"main": "a" * 40})
        assert len(erros) == 1 and f"'{fonte}'" in erros[0] and "reservada" in erros[0]
    assert fases.conferir_referencias([dict(a, fonte="suite")], None, None)[0].startswith("achado 0: fonte")


def test_saida_com_stdout_cp1252_redirecionado_nao_quebra(monkeypatch):
    import io
    import sys
    bruto = io.BytesIO()
    saida = io.TextIOWrapper(bruto, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", saida)
    obj = {"bloco": "SONDA Andamento → clipe dividido", "ok": "✓", "acento": "ação"}
    assert fases._saida(obj, False) == 1
    saida.flush()
    texto = bruto.getvalue().decode("cp1252")
    assert json.loads(texto) == obj  # o mesmo JSON, sem perder nada (escapes \uXXXX onde o cp1252 não tem)


# ---------- sondas do agente na suíte e no build ----------

MAPA_SONDAS = {"SONDA Andamento": ["ia/andamento"], "SONDA Audio": ["ia/audio", "principio/P4"],
               "SONDA Alocacao": ["principio/P2", "efeitos/sem-alocacao"]}


def test_itens_com_sondas_junta_o_mapa_sem_duplicar_nem_mexer_no_original():
    itens = [{"id": "ia/andamento", "cenarios": ["suite:Deteccao de andamento", "suite:SONDA Andamento"]},
             {"id": "ia/audio", "cenarios": []}, {"id": "outro/x", "cenarios": ["suite:Bloco A"]}]
    novos = fases.itens_com_sondas(itens, MAPA_SONDAS)
    por_id = {i["id"]: i for i in novos}
    assert por_id["ia/andamento"]["cenarios"] == ["suite:Deteccao de andamento", "suite:SONDA Andamento"]
    assert por_id["ia/audio"]["cenarios"] == ["suite:SONDA Audio"]
    assert por_id["outro/x"]["cenarios"] == ["suite:Bloco A"]
    assert itens[1]["cenarios"] == []  # o catálogo carregado não muda


def test_resultados_suite_por_item_sonda_citada_cobre_a_verificacao_sonda():
    itens = [{"id": "a/com-sonda", "verificacao": ["suite", "sonda"], "marco": "M5",
              "cenarios": ["suite:Bloco A", "suite:SONDA Andamento"]},
             {"id": "a/sem-sonda", "verificacao": ["suite", "sonda"], "marco": "M5", "cenarios": ["suite:Bloco A"]}]
    blocos = [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0},
              {"nome": "SONDA Andamento", "sub": "y", "ok": 2, "falhas": 0}]
    r = fases.resultados_suite_por_item(itens, blocos, ("suite",))
    assert r["a/com-sonda"] == ("passou", None)
    assert r["a/sem-sonda"][0] == "nao_testavel" and "sonda" in r["a/sem-sonda"][1]


def test_resultados_suite_por_item_diz_por_que_a_sonda_nao_rodou():
    itens = [{"id": "a/x", "verificacao": ["suite", "sonda"], "marco": "M5",
              "cenarios": ["suite:Bloco A", "suite:SONDA Andamento", "suite:Sumido"]}]
    blocos = [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0}]
    r = fases.resultados_suite_por_item(itens, blocos, ("suite",),
                                        {"SONDA Andamento": "sonda não compila em main: SondaA.cpp(3): error C2065: x"})
    res, motivo = r["a/x"]
    assert res == "nao_testavel"
    assert "SONDA Andamento: sonda não compila em main" in motivo and "bloco da suíte não encontrado: Sumido" in motivo


def test_achados_das_sondas_so_para_bloco_que_falha_com_o_detalhe_dele():
    blocos = [{"nome": "SONDA Andamento", "sub": "clipe dividido", "ok": 3, "falhas": 2},
              {"nome": "SONDA Andamento", "sub": "120 BPM", "ok": 4, "falhas": 0},
              {"nome": "SONDA Alocacao", "sub": "efeitos", "ok": 1, "falhas": 1},
              {"nome": "SONDA Nova", "sub": "z", "ok": 0, "falhas": 1}]
    detalhes = ["- Bloco A / x", "!!! falha da MAW", "- SONDA Andamento / clipe dividido",
                "!!! Test 1 failed: a metade da direita tem 140 BPM e T mediu 115.58 BPM",
                "- SONDA Alocacao / efeitos", "!!! Test 1 failed: 3 alocacoes"]
    lista = fases.achados_das_sondas(ALVO, blocos, detalhes, MAPA_SONDAS, {"SONDA Andamento": "SondaA.cpp"})
    assert all(achados.validar(a) == [] for a in lista)
    assert len(lista) == 3
    a, b, c = lista
    # a sonda é teste do próprio agente: não é fonte mecânica, passa pela verificação adversarial
    assert a["fonte"] == "sonda" and a["assinatura"] == "suite:SONDA Andamento|clipe dividido"
    assert "sonda" not in fases.FONTES_MECANICAS
    assert a["causa_provavel"]["arquivo_linha"] == "privado/sondas/SondaA.cpp"
    assert "teste do próprio agente" in a["causa_provavel"]["texto"]
    assert b["causa_provavel"]["arquivo_linha"].startswith("privado/sondas/")  # sem arquivo conhecido
    assert a["item_catalogo"] == "ia/andamento" and a["principio"] is None
    assert "115.58" in a["obtido"] and "falha da MAW" not in a["obtido"] and "3 alocacoes" not in a["obtido"]
    assert any("SondaA.cpp" in p for p in a["passos"]) and a["evidencias"][0]["arquivo"] == "suites/main.json"
    assert "sonda" in a["titulo"].lower()
    # item de funcionalidade primeiro; o princípio vai para `principio`
    assert b["item_catalogo"] == "efeitos/sem-alocacao" and b["principio"] == "P2" and b["severidade"] == "alta"
    assert a["severidade"] == "media"
    # sonda fora do mapa: item genérico de saúde, nunca a suíte existente da MAW
    assert c["item_catalogo"] == "saude/geral"


def test_sondas_nos_erros_do_msbuild():
    injetadas = ["SondaA.cpp", "SondaB.cpp"]
    wt = r"C:\x\work\alvos\main\Source"
    assert fases.sondas_nos_erros([wt + r"\Tests\Sondas\SondaA.cpp(12,5): error C2039: 'x': is not a member"],
                                  injetadas) == {"SondaA.cpp"}
    assert fases.sondas_nos_erros(["SondaB.obj : error LNK2019: unresolved external symbol f"], injetadas) \
        == {"SondaB.cpp"}
    assert fases.sondas_nos_erros([wt + r"\Audio\Motor.cpp(3): error C2065: 'y': undeclared identifier",
                                   "LINK : fatal error LNK1120: 1 unresolved externals"], injetadas) == set()
    # na pasta das sondas, mas sem nome reconhecível: todas são suspeitas
    assert fases.sondas_nos_erros([wt + r"/Tests/Sondas/Outra.cpp(1): error C1083: cannot open"], injetadas) \
        == {"SondaA.cpp", "SondaB.cpp"}
    assert fases.sondas_nos_erros(["qualquer coisa"], []) == set()


def test_reverificacao_automatica_de_achado_de_sonda(tmp_path):
    ultimo = fases.achados_das_sondas(ALVO, [{"nome": "SONDA Andamento", "sub": "s", "ok": 0, "falhas": 1}], [],
                                      MAPA_SONDAS)[0]
    hist = {"itens": {"k": {"id": "MAW-0009", "estado": "aberto", "ultimo": ultimo}}}
    alvos_sprint = [{"nome": "main", "commit": "a" * 40, "compartilha_com": None}]
    suites = tmp_path / "suites"
    suites.mkdir()
    base = {"blocos": [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0}], "incoerencias": [], "assercoes": []}
    (suites / "main.json").write_text(json.dumps({**base, "sondas": {"blocos": [
        {"nome": "SONDA Andamento", "sub": "s", "ok": 1, "falhas": 0}]}}), encoding="utf-8")
    assert fases.reverificacoes_automaticas(hist, alvos_sprint, tmp_path) == {"MAW-0009": "corrigido"}
    (suites / "main.json").write_text(json.dumps({**base, "sondas": {"blocos": [
        {"nome": "SONDA Andamento", "sub": "s", "ok": 0, "falhas": 1}]}}), encoding="utf-8")
    assert fases.reverificacoes_automaticas(hist, alvos_sprint, tmp_path) == {"MAW-0009": "persiste"}
    (suites / "main.json").write_text(json.dumps(base), encoding="utf-8")  # a sonda não rodou
    assert fases.reverificacoes_automaticas(hist, alvos_sprint, tmp_path) == {"MAW-0009": "nao_verificavel"}


def test_achado_de_sonda_sem_veredito_vira_provavel_e_com_veredito_confirmado(tmp_path):
    [a] = fases.achados_das_sondas(ALVO, [{"nome": "SONDA Andamento", "sub": "s", "ok": 0, "falhas": 1}], [],
                                   MAPA_SONDAS, {"SONDA Andamento": "SondaA.cpp"})
    brutos = tmp_path / "achados-brutos"
    brutos.mkdir()
    (brutos / "sonda-main-000.json").write_text(json.dumps(a), encoding="utf-8")
    (brutos / "sonda-main-001.json").write_text(json.dumps(dict(a, assinatura="suite:SONDA X|y")), encoding="utf-8")
    (tmp_path / "vereditos").mkdir()
    (tmp_path / "vereditos" / "sonda-main-001.json").write_text(json.dumps({"resultado": "confirmado"}),
                                                                encoding="utf-8")
    entradas, sem_verificacao, _ = fases.ler_brutos(tmp_path)
    por_nome = {o.split("[")[0]: x for o, x in entradas}
    assert por_nome["sonda-main-000.json"]["confianca"] == "provavel"
    assert id(por_nome["sonda-main-000.json"]) in sem_verificacao
    assert por_nome["sonda-main-001.json"]["confianca"] == "confirmado"


def test_fonte_sonda_e_reservada_a_cli():
    a = dict(fases.achados_das_sondas(ALVO, [{"nome": "SONDA Andamento", "sub": "s", "ok": 0, "falhas": 1}], [],
                                      MAPA_SONDAS)[0])
    erros = fases.conferir_referencias([a], None, {"main": "a" * 40})
    assert any("sonda" in e and "reservada" in e for e in erros)


def test_achado_da_suite_que_morreu_no_meio_diz_o_bloco_que_rodava():
    """A MAW escreve "rodando: <área> / <bloco>" no stderr: o achado do processo que caiu diz onde."""
    inc = "totais ausentes: a execução pode ter morrido antes do fim"
    r = {"blocos": [], "detalhes": [], "incoerencias": [inc], "assercoes": [], "exit_code": -1073741571,
         "declarado": "AUSENTE", "ultimo_bloco": "Area X / bloco y"}
    [a] = fases.achados_da_suite(ALVO, r)
    assert achados.validar(a) == []
    assert "Area X / bloco y" in a["obtido"] and "Area X / bloco y" in a["titulo"]
    assert "0xC00000FD" in a["obtido"]  # o código de saída do Windows em hexadecimal
    assert a["assinatura"] == f"suite-incoerente:{inc}"  # a impressão digital não depende do bloco
    # sem o progresso (MAW antiga), o achado continua como era
    [b] = fases.achados_da_suite(ALVO, {**r, "ultimo_bloco": None})
    assert b["obtido"] == inc


# ---------- reverificação dos achados da E2E e do serviço pelo cenário/checagem desta sprint ----------

def test_reverificacao_automatica_da_e2e_pelo_cenario_desta_sprint(tmp_path):
    """O critério de aceite de um achado da E2E é o próprio cenário passar no alvo."""
    h = _hist(_reg("MAW-0001", "e2e", "e2e::servidor::localhost-ipv6-prazo-2s", item="ia/servidor"),
              _reg("MAW-0002", "e2e", "e2e::mesa", item="mixer/geral"),
              _reg("MAW-0003", "e2e", "e2e::gemini", item="ia/conselheiro"),
              _reg("MAW-0004", "e2e", "e2e::sumiu", item="ia/conselheiro"),
              _reg("MAW-0005", "e2e", "e2e::placa", alvos_=("docs-z",), item="interface/placa"))
    _gravar(tmp_path, "estado.json", {"passos": {
        "e2e:main:servidor": {"status": "concluido", "detalhe": {"estado": "passou"}},
        "e2e:main:mesa": {"status": "concluido", "detalhe": {"estado": "problema"}},
        "e2e:main:gemini": {"status": "concluido", "detalhe": {"estado": "pulei"}},
        "e2e:main:placa": {"status": "concluido", "detalhe": {"estado": "passou"}}}})
    r = fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path)
    assert r == {"MAW-0001": "corrigido", "MAW-0002": "persiste", "MAW-0003": "nao_verificavel",
                 "MAW-0004": "nao_verificavel", "MAW-0005": "corrigido"}  # docs-z usa a execução da origem


def test_reverificacao_automatica_do_servico_pela_checagem_desta_sprint(tmp_path):
    h = _hist(_reg("MAW-0001", "servico", "servico:separate-corpo-nao-json", item="servico/x"),
              _reg("MAW-0002", "servico", "servico:separate-2stems", item="servico/x"),
              _reg("MAW-0003", "servico", "servico:nao-rodou", item="servico/x"))
    _gravar(tmp_path, "estado.json", {"passos": {"servico:main": {"status": "concluido", "detalhe": {
        "checagens": [{"id": "separate-corpo-nao-json", "ok": True, "problemas": []},
                      {"id": "separate-2stems", "ok": False, "problemas": ["x"]}]}}}})
    r = fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path)
    assert r == {"MAW-0001": "corrigido", "MAW-0002": "persiste", "MAW-0003": "nao_verificavel"}


def test_reverificacao_automatica_da_e2e_sem_estado_nao_verifica(tmp_path):
    h = _hist(_reg("MAW-0001", "e2e", "e2e::mesa", item="mixer/geral"),
              _reg("MAW-0002", "servico", "servico:health", item="servico/x"))
    assert fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path) == {
        "MAW-0001": "nao_verificavel", "MAW-0002": "nao_verificavel"}


def test_reverificacao_automatica_do_servico_checagem_sem_resultado_nao_verifica(tmp_path):
    """`ok: None` (checagem pulada ou `na` neste alvo) não é nem corrigido nem persiste."""
    h = _hist(_reg("MAW-0001", "servico", "servico:sem-resultado", item="servico/x"))
    _gravar(tmp_path, "estado.json", {"passos": {"servico:main": {"status": "concluido", "detalhe": {
        "checagens": [{"id": "sem-resultado", "ok": None, "problemas": []}]}}}})
    assert fases.reverificacoes_automaticas(h, ALVOS_SPRINT, tmp_path) == {"MAW-0001": "nao_verificavel"}


# ---------- modo da revisão de código: completa (semanal) ou pelo diff desde a sprint anterior ----------

def _sprint_falsa(raiz, n, passos, main=None):
    p = raiz / f"sprint-{n:02d}"
    p.mkdir(parents=True)
    (p / "estado.json").write_text(json.dumps({"numero": n, "passos": passos}), encoding="utf-8")
    if main:
        (p / "alvos.json").write_text(json.dumps({"alvos": [{"nome": "main", "commit": main}]}), encoding="utf-8")
    return p


def test_revisao_completa_quando_nunca_houve_uma(tmp_path):
    _sprint_falsa(tmp_path, 1, {}, main="a" * 40)
    atual = _sprint_falsa(tmp_path, 2, {})
    r = fases.modo_da_revisao(estado.carregar(atual), agora=datetime(2026, 10, 1, 22, 0))
    assert r["modo"] == "completa" and r["ultima_completa"] is None and r["base_main"] == "a" * 40


def test_revisao_pelo_diff_quando_a_completa_e_recente(tmp_path):
    _sprint_falsa(tmp_path, 1, {"revisao_completa": {"status": "concluido", "fim": "2026-09-28T23:00:00"}},
                  main="a" * 40)
    _sprint_falsa(tmp_path, 2, {}, main="b" * 40)
    atual = _sprint_falsa(tmp_path, 3, {})
    r = fases.modo_da_revisao(estado.carregar(atual), agora=datetime(2026, 10, 1, 22, 0))
    assert r == {"modo": "diff", "ultima_completa": "2026-09-28T23:00:00", "base_main": "b" * 40}


def test_revisao_completa_depois_de_sete_dias(tmp_path):
    _sprint_falsa(tmp_path, 1, {"revisao_completa": {"status": "concluido", "fim": "2026-09-20T23:00:00"}})
    atual = _sprint_falsa(tmp_path, 2, {})
    assert fases.modo_da_revisao(estado.carregar(atual), agora=datetime(2026, 10, 1, 22, 0))["modo"] == "completa"


def test_revisao_completa_ja_feita_nesta_sprint_continua_completa(tmp_path):
    atual = _sprint_falsa(tmp_path, 1, {"revisao_completa": {"status": "concluido", "fim": "2026-10-01T22:30:00"}})
    assert fases.modo_da_revisao(estado.carregar(atual), agora=datetime(2026, 10, 1, 23, 0))["modo"] == "completa"
