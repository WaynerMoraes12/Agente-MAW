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


# ---------- I7: ausências declaradas ----------

FASES_OK = {f: {"status": "concluido", "inicio": "2026-09-28T01:00:00", "fim": "2026-09-28T01:10:00"}
            for f in ("preflight", "preparar", "compilar", "catalogar", "executar", "verificar", "consolidar",
                      "encerrar")}


def _estado(s: Path, passos: dict, **extra):
    (s / "estado.json").write_text(json.dumps({"numero": 1, "criado": "2026-09-28T01:00:00", "passos": passos,
                                               **extra}))


def test_fases_nao_concluidas_e_passos_interrompidos_viram_limitacao(tmp_path):
    s = _sprint(tmp_path)
    passos = {k: v for k, v in FASES_OK.items() if k not in ("verificar", "consolidar")}
    passos["suite:main"] = {"status": "em_andamento", "inicio": "2026-09-28T01:05:00"}
    passos["relatorio"] = {"status": "em_andamento", "inicio": "2026-09-28T01:20:00"}
    _estado(s, passos)
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert "fase verificar não concluída" in ctx["limitacoes"]
    assert "fase consolidar não concluída" in ctx["limitacoes"]
    assert "passo suite:main não concluído (interrompido)" in ctx["limitacoes"]
    assert not any("relatorio" in l for l in ctx["limitacoes"])  # é o passo que está gerando o PDF
    assert "achados não consolidados nesta execução" in ctx["limitacoes"]
    assert ctx["achados_anexados"] is False


def test_com_consolidacao_os_achados_sao_anexados(tmp_path):
    s = _sprint(tmp_path)
    _estado(s, dict(FASES_OK, consolidar={"status": "concluido", "detalhe": {"sem_verificacao_adversarial": 2}}))
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert ctx["achados_anexados"] is True
    assert not any("não concluída" in l for l in ctx["limitacoes"])
    assert any(l.startswith("2 achado(s) sem verificação adversarial") for l in ctx["limitacoes"])


def test_instrucoes_nao_prometem_anexo_sem_consolidacao(tmp_path):
    s = _sprint(tmp_path)
    ctx = contexto.montar(s, ITENS, [], None, None)
    html = pdf._html(ctx)
    assert "anexado a este PDF" not in html and "não foram consolidados" in html
    _estado(s, FASES_OK)
    assert "anexado a este PDF" in pdf._html(contexto.montar(s, ITENS, [], None, None))


def test_limitacoes_dos_agentes_captura_e_preambulo_da_suite(tmp_path):
    s = _sprint(tmp_path)
    (s / "limitacoes-testador-motor.json").write_text(json.dumps(["não consegui abrir o arquivo X"]))
    (s / "limitacoes-guardiao-da-ideia-main.json").write_text(json.dumps(["README Y sem versão"]))
    (s / "limitacoes-quebrado.json").write_text("{nao e lista")
    (s / "suites").mkdir()
    (s / "suites" / "main.json").write_text(json.dumps({
        "blocos": [], "total_ok": 0, "total_falhas": 0, "assercoes": [],
        "captura_erro": "outro ouvinte de OutputDebugString já está ativo; jassert não capturado",
        "preambulo": ["Plugin do master descarregado"]}))
    ctx = contexto.montar(s, ITENS, [], None, None)
    lim = ctx["limitacoes"]
    assert "testador-motor: não consegui abrir o arquivo X" in lim
    assert "guardiao-da-ideia-main: README Y sem versão" in lim
    assert any(l.startswith("jassert não capturado no alvo main: outro ouvinte") for l in lim)
    assert "main: aviso antes da suíte: Plugin do master descarregado" in lim
    assert any("limitacoes-quebrado.json" in e for e in ctx["erros_agente"])


# ---------- I8: alvo com a mesma árvore herda os achados da origem ----------

def test_alvo_que_compartilha_herda_achados_da_origem(tmp_path):
    s = _sprint(tmp_path)
    alvos_json = json.loads((s / "alvos.json").read_text())
    alvos_json["alvos"].append({"nome": "docs-z", "branch": "docs/z", "commit": "d" * 40, "origem": "github",
                                "compartilha_com": "main"})
    (s / "alvos.json").write_text(json.dumps(alvos_json))
    grave = dict(ACHADO, id="MAW-0002", alvos=[{"alvo": "main", "commit": "a" * 40}], introduzido_por=None)
    doc = dict(ACHADO, id="MAW-0003", item_catalogo="documentacao/readme-raiz", tipo="afirmacao_falsa",
               alvos=[{"alvo": "main", "commit": "a" * 40}], introduzido_por=None)
    (s / "achados.json").write_text(json.dumps([ACHADO, grave, doc]))
    (s / "builds").mkdir()
    (s / "builds" / "main-Release.json").write_text(json.dumps({"ok": True, "segundos": 1, "avisos": []}))
    ctx = contexto.montar(s, ITENS, [], None, None)
    docs = next(a for a in ctx["alvos"] if a["nome"] == "docs-z")
    assert docs["herdados"] == ["MAW-0002"]           # documentação é revisada por alvo: não herda
    assert "MAW-0002" in docs["achados_ids"]
    assert docs["semaforo"] == "bloqueado"           # o achado grave herdado conta


# ---------- menores do relatório ----------

def test_corrigidos_nao_aparecem_nas_fichas(tmp_path):
    s = _sprint(tmp_path)
    corrigido = dict(ACHADO, id="MAW-0009", estado="corrigido", titulo="Achado já corrigido")
    melhoria_corrigida = dict(ACHADO, id="MAW-0010", estado="corrigido", tipo="melhoria", prioridade="baixa")
    (s / "achados.json").write_text(json.dumps([ACHADO, corrigido, melhoria_corrigida]))
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert [a["id"] for a in ctx["achados"]] == ["MAW-0001"] and ctx["melhorias"] == []
    assert {a["id"] for a in ctx["corrigidos"]} == {"MAW-0009", "MAW-0010"}


def test_prova_de_intocada_em_tabela_e_metodo_do_backup(tmp_path):
    s = _sprint(tmp_path)
    (s / "prova-antes.json").write_text(json.dumps({
        "MAW": {"tipo": "git", "head": "1" * 40, "branch": "main", "hash_status": "s", "hash_diff": "d"},
        "MAW_sem_git": {"tipo": "arquivos", "arquivos": [["a.txt", 1, 5]]}}))
    (s / "prova-depois.json").write_text(json.dumps({
        "MAW": {"tipo": "git", "head": "1" * 40, "branch": "main", "hash_status": "s", "hash_diff": "d"},
        "MAW_sem_git": {"tipo": "arquivos", "arquivos": [["a.txt", 1, 5], ["b.txt", 2, 6]]}}))
    _estado(s, FASES_OK, restauracoes=[{"quem": "suite", "backup": "C:/x/backups/1", "verificado": True,
                                        "backup_apagado": True, "quando": "2026-09-28T01:06:00"}])
    ctx = contexto.montar(s, ITENS, [], None, None)
    por_pasta = {l["pasta"]: l for l in ctx["prova"]}
    assert por_pasta["MAW"]["antes"] == "1" * 10 and por_pasta["MAW"]["igual"] is True
    assert por_pasta["MAW_sem_git"]["antes"] == "sem git: 1 arquivo(s)" and por_pasta["MAW_sem_git"]["igual"] is False
    assert any("backup temporário" in m and "apagado" in m for m in ctx["metodo"])
    html = pdf._html(ctx)
    assert "Prova de MAW intocada" in html and "MAW_sem_git" in html and "backup temporário" in html


def test_metodo_declara_backup_que_nao_foi_apagado(tmp_path):
    s = _sprint(tmp_path)
    _estado(s, FASES_OK, restauracoes=[{"quem": "suite", "backup": "C:/x/backups/1", "verificado": False,
                                        "erro": "em uso", "backup_apagado": False}])
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert any("mantido" in m and "C:/x/backups/1" in m for m in ctx["metodo"])


def test_metodo_usa_o_desfecho_final_de_cada_backup(tmp_path):
    s = _sprint(tmp_path)
    _estado(s, FASES_OK, restauracoes=[
        {"quem": "suite", "backup": "B1", "verificado": False, "erro": "em uso", "backup_apagado": False},
        {"quem": "retomada", "backup": "B1", "verificado": True, "backup_apagado": True}])
    [m] = contexto.montar(s, ITENS, [], None, None)["metodo"]
    assert "foi apagado" in m and "1 tentativa(s)" in m and "mantido" not in m
