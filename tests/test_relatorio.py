import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from maw_agent.relatorio import contexto, pdf

ACHADO = {"id": "MAW-0001", "estado": "novo", "titulo": "Suíte diz passou mas sai com 1", "tipo": "violacao",
          "severidade": "alta", "prioridade": None, "alvos": [{"alvo": "feature-x", "commit": "abc"}],
          "item_catalogo": "saude/suite-existente", "principio": "P4", "passos": ["rodar"],
          "esperado": "coerente", "obtido": "incoerente", "evidencias": [], "causa_provavel": None,
          "sugestao": "corrigir", "criterio_aceite": "suite coerente", "confianca": "confirmado",
          "assinatura": "x", "introduzido_por": "feature-x", "historico": ["01"]}

def _sprint(tmp_path: Path) -> Path:
    s = tmp_path / "sprint-01"; s.mkdir()
    (s / "estado.json").write_text(json.dumps({"numero": 1, "criado": "2026-09-28T01:00:00", "passos": {}}))
    (s / "alvos.json").write_text(json.dumps({"alvos": [
        {"nome": "main", "branch": "main", "commit": "a" * 40, "origem": "github", "compartilha_com": None},
        {"nome": "feature-x", "branch": "feature/x", "commit": "b" * 40, "origem": "github", "compartilha_com": None}],
        "avisos": ["MAW_a1: há alterações não commitadas"]}))
    (s / "achados.json").write_text(json.dumps([ACHADO]))
    (s / "intocada.json").write_text(json.dumps({"verificado": True, "diferencas": [], "ambiente_restaurado": True}))
    (s / "resultados.jsonl").write_text(json.dumps({"item": "saude/suite-existente", "alvo": "main",
                                                    "resultado": "passou", "motivo": None, "achados": [], "fonte": "suite"}) + "\n")
    return s

ITENS = [{"id": "saude/suite-existente", "area": "saude", "titulo": "Suíte existente passa", "descricao": "d",
          "origem": ["x"], "verificacao": ["suite"], "cenarios": [], "requisitos": [], "marco": "M1"},
         {"id": "edicao/split", "area": "edicao", "titulo": "Split", "descricao": "d",
          "origem": ["x"], "verificacao": ["e2e"], "cenarios": [], "requisitos": ["gui"], "marco": "M2"}]

def test_semaforo():
    assert contexto.semaforo([], True) == "pronto"
    assert contexto.semaforo([], False) == "bloqueado"
    assert contexto.semaforo([ACHADO], True) == "bloqueado"
    assert contexto.semaforo([dict(ACHADO, severidade="baixa")], True) == "com_ressalvas"
    assert contexto.semaforo([dict(ACHADO, confianca="provavel")], True) == "com_ressalvas"

def test_contexto_completo_e_matriz_sem_buraco(tmp_path):
    ctx = contexto.montar(_sprint(tmp_path), ITENS, [], None, None)
    assert ctx["sprint"] == "sprint-01"
    assert [a["nome"] for a in ctx["alvos"]] == ["main", "feature-x"]
    assert {a["nome"]: a["semaforo"] for a in ctx["alvos"]}["feature-x"] == "bloqueado"
    assert all(len(l["celulas"]) == 2 for l in ctx["matriz"]["linhas"])
    assert ctx["cobertura"]["passou"] == 1 and ctx["cobertura"]["nao_testavel"] == 3
    assert any("não commitadas" in l for l in ctx["limitacoes"])
    assert ctx["intocada"]["verificado"] is True

def test_pdf_real_com_anexo(tmp_path):
    s = _sprint(tmp_path)
    ctx = contexto.montar(s, ITENS, [], None, None)
    destino = pdf.renderizar(ctx, s / "MAW-Sprint-01.pdf")
    texto = "\n".join(p.extract_text() for p in PdfReader(destino).pages)
    assert "MAW intocada" in texto and "MAW-0001" in texto and "Matriz de cobertura" in texto
    pdf.anexar(destino, s / "achados.json", "achados.json")
    assert json.loads(pdf.ler_anexo(destino, "achados.json"))[0]["id"] == "MAW-0001"

def test_anexar_e_atomico_preserva_original_em_falha(tmp_path, monkeypatch):
    s = _sprint(tmp_path)
    ctx = contexto.montar(s, ITENS, [], None, None)
    destino = pdf.renderizar(ctx, s / "MAW-Sprint-01.pdf")
    original = destino.read_bytes()

    def _falha(*args, **kwargs):
        raise OSError("disco cheio (simulado)")

    monkeypatch.setattr(pdf.sandbox, "escrever_bytes", _falha)
    with pytest.raises(OSError):
        pdf.anexar(destino, s / "achados.json", "achados.json")
    assert destino.exists()
    assert destino.read_bytes() == original

def test_ambiente_restaurado_aparece_na_capa(tmp_path):
    s = _sprint(tmp_path)
    (s / "intocada.json").write_text(json.dumps({"verificado": True, "diferencas": [],
        "ambiente_restaurado": False, "erro_restauracao": "falha ao restaurar arquivo de projeto"}))
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert ctx["intocada"]["ambiente_restaurado"] is False
    destino = pdf.renderizar(ctx, s / "amb-restaurado.pdf")
    texto = "\n".join(p.extract_text() for p in PdfReader(destino).pages)
    assert "Ambiente restaurado: NÃO" in texto
    assert "MAW intocada: verificado" in texto  # continua condicionado só a 'verificado'

def test_ausencia_de_resumo_e_texto_do_alvo_vira_limitacao(tmp_path):
    ctx = contexto.montar(_sprint(tmp_path), ITENS, [], None, None)
    assert "resumo executivo não redigido nesta execução" in ctx["limitacoes"]
    assert "texto do alvo main não redigido nesta execução" in ctx["limitacoes"]
    assert "texto do alvo feature-x não redigido nesta execução" in ctx["limitacoes"]
    assert ctx["resumo"] == ""
    assert all(a["texto"] == "" for a in ctx["alvos"])

def test_ausencias_nunca_aparecem_como_none(tmp_path):
    s = tmp_path / "sprint-02"; s.mkdir()
    (s / "estado.json").write_text(json.dumps({"numero": 2, "criado": "2026-09-28T02:00:00",
        "passos": {"saude": {"status": "falhou"}}}))
    (s / "alvos.json").write_text(json.dumps({"alvos": [
        {"nome": "main", "branch": "main", "commit": "a" * 40, "origem": "github", "compartilha_com": None}],
        "avisos": []}))
    (s / "achados.json").write_text(json.dumps([]))
    (s / "intocada.json").write_text(json.dumps({"verificado": True, "diferencas": [], "ambiente_restaurado": True}))
    (s / "resultados.jsonl").write_text(json.dumps({"item": "saude/suite-existente", "alvo": "main",
        "resultado": "nao_testavel", "motivo": None, "achados": [], "fonte": "suite"}) + "\n")
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert any("motivo não informado" in l for l in ctx["limitacoes"])
    assert not any("None" in l for l in ctx["limitacoes"])
    assert any("sem mensagem de erro" in e for e in ctx["erros_agente"])
    assert not any("None" in e for e in ctx["erros_agente"])
