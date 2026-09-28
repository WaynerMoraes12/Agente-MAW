from maw_agent import achados

def base(**over):
    a = {"titulo": "Exportar sem ffmpeg trava", "tipo": "bug", "severidade": "alta", "prioridade": None,
         "alvos": [{"alvo": "main", "commit": "aaa"}], "item_catalogo": "exportacao/mp3",
         "principio": "P4", "passos": ["abrir", "exportar"], "esperado": "mensagem clara",
         "obtido": "travou sem aviso", "evidencias": [{"arquivo": "e/1.png", "legenda": "tela", "embutir": True}],
         "causa_provavel": {"arquivo_linha": "x.cpp:10", "texto": "hipótese"}, "sugestao": "avisar",
         "criterio_aceite": "cenário exportacao/mp3 passa", "confianca": "confirmado",
         "assinatura": "Export.cpp:123 encode()"}
    a.update(over)
    return a

def test_valido_e_invalido():
    assert achados.validar(base()) == []
    assert len(achados.validar(base(tipo="palpite", passos=[]))) >= 2

def test_bug_exige_severidade_melhoria_exige_prioridade():
    assert achados.validar(base(severidade=None))
    assert achados.validar(base(tipo="melhoria", severidade=None, prioridade=None))
    assert achados.validar(base(tipo="melhoria", severidade=None, prioridade="media")) == []

def test_impressao_ignora_numero_de_linha_e_espacos():
    a = achados.impressao_digital(base(assinatura="Export.cpp:123   encode()"))
    b = achados.impressao_digital(base(assinatura="export.cpp:130 encode()"))
    assert a == b and len(a) == 16

def test_deduplica_entre_alvos():
    x = base(); y = base(alvos=[{"alvo": "feature-x", "commit": "bbb"}], severidade="critica")
    [u] = achados.deduplicar([x, y])
    assert {a["alvo"] for a in u["alvos"]} == {"main", "feature-x"}
    assert u["severidade"] == "critica"

def test_introduzido_por_uma_branch():
    l = [base(alvos=[{"alvo": "feature-x", "commit": "b"}]), base(assinatura="outra")]
    achados.marcar_introducao(l)
    assert l[0]["introduzido_por"] == "feature-x" and l[1]["introduzido_por"] is None

def test_ciclo_de_estados_entre_sprints():
    h = {"proximo": 1, "itens": {}}
    [a1], h = achados.consolidar([base()], h, "01", {})
    assert a1["id"] == "MAW-0001" and a1["estado"] == "novo"
    [a2], h = achados.consolidar([base()], h, "02", {})
    assert a2["id"] == "MAW-0001" and a2["estado"] == "aberto"
    [a3], h = achados.consolidar([], h, "03", {"MAW-0001": "corrigido"})
    assert a3["estado"] == "corrigido"
    [a4], h = achados.consolidar([base()], h, "04", {})
    assert a4["estado"] == "regressao" and a4["id"] == "MAW-0001"
    assert a4["historico"] == ["01", "02", "03", "04"]

def test_corrigido_antigo_nao_reaparece_nas_sprints_seguintes():
    h = {"proximo": 1, "itens": {}}
    _, h = achados.consolidar([base()], h, "01", {})
    _, h = achados.consolidar([], h, "02", {"MAW-0001": "corrigido"})
    l, h = achados.consolidar([], h, "03", {})
    assert l == []

def test_sem_reverificacao_vira_nao_verificavel_mantendo_anterior():
    h = {"proximo": 1, "itens": {}}
    _, h = achados.consolidar([base()], h, "01", {})
    [x], h = achados.consolidar([], h, "02", {})
    assert x["estado"] == "nao_verificavel" and x["estado_anterior"] == "novo"

def test_ids_nunca_reaproveitados():
    h = {"proximo": 1, "itens": {}}
    l, h = achados.consolidar([base(), base(assinatura="b2")], h, "01", {})
    assert sorted(a["id"] for a in l) == ["MAW-0001", "MAW-0002"]
    l, h = achados.consolidar([base(assinatura="b3")], h, "02", {"MAW-0001": "corrigido", "MAW-0002": "corrigido"})
    assert [a["id"] for a in l if a["estado"] == "novo"] == ["MAW-0003"]

def test_derrubado_nao_entra():
    l, _ = achados.consolidar([base(veredito={"resultado": "derrubado", "justificativa": "x"})],
                              {"proximo": 1, "itens": {}}, "01", {})
    assert l == []

def test_historico_ida_e_volta(tmp_path):
    h = {"proximo": 3, "itens": {"abc": {"id": "MAW-0002"}}}
    achados.salvar_historico(tmp_path / "h.json", h)
    assert achados.carregar_historico(tmp_path / "h.json") == h
    assert achados.carregar_historico(tmp_path / "nao.json") == {"proximo": 1, "itens": {}}
