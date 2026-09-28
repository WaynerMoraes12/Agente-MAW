import json
from pathlib import Path
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
