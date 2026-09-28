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
    # melhoria (mesmo grave) e lacuna leve abertas não bloqueiam, mas também não são "pronto": há
    # ressalva a registrar. Uma lacuna grave e confirmada continua bloqueando (não é "não-melhoria"
    # que o achado deixa de ser só por ser lacuna).
    assert contexto.semaforo([dict(ACHADO, tipo="melhoria")], True) == "com_ressalvas"
    assert contexto.semaforo([dict(ACHADO, tipo="lacuna", severidade="baixa")], True) == "com_ressalvas"

def test_contexto_completo_e_matriz_sem_buraco(tmp_path):
    ctx = contexto.montar(_sprint(tmp_path), ITENS, [], None, None)
    assert ctx["sprint"] == "sprint-01"
    assert [a["nome"] for a in ctx["alvos"]] == ["main", "feature-x"]
    assert {a["nome"]: a["semaforo"] for a in ctx["alvos"]}["feature-x"] == "bloqueado"
    assert all(len(l["celulas"]) == 2 for l in ctx["matriz"]["linhas"])
    assert ctx["cobertura"]["passou"] == 1 and ctx["cobertura"]["nao_testavel"] == 3
    assert any("não commitadas" in l for l in ctx["limitacoes"])
    assert ctx["intocada"]["verificado"] is True

def test_nota_da_verificacao_vem_da_justificativa_do_veredito(tmp_path):
    s = _sprint(tmp_path)
    achado = dict(ACHADO, veredito={"resultado": "confirmado", "justificativa": "Reproduzi seguindo o passo 2."})
    (s / "achados.json").write_text(json.dumps([achado]))
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert ctx["achados"][0]["nota_verificacao"] == "Reproduzi seguindo o passo 2."

def test_nota_da_verificacao_trunca_em_700_caracteres(tmp_path):
    s = _sprint(tmp_path)
    achado = dict(ACHADO, confianca="provavel",
                  veredito={"resultado": "provavel", "justificativa": "x" * 800})
    (s / "achados.json").write_text(json.dumps([achado]))
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert len(ctx["achados"][0]["nota_verificacao"]) == 700

def test_nota_da_verificacao_ausente_sem_veredito(tmp_path):
    ctx = contexto.montar(_sprint(tmp_path), ITENS, [], None, None)  # ACHADO sem 'veredito'
    assert ctx["achados"][0]["nota_verificacao"] is None

def test_nota_da_verificacao_aparece_no_texto_do_pdf(tmp_path):
    s = _sprint(tmp_path)
    achado = dict(ACHADO, veredito={"resultado": "confirmado", "justificativa": "Reproduzi seguindo o passo 2."})
    (s / "achados.json").write_text(json.dumps([achado]))
    ctx = contexto.montar(s, ITENS, [], None, None)
    destino = pdf.renderizar(ctx, s / "MAW-Sprint-01.pdf")
    texto = "\n".join(p.extract_text() for p in PdfReader(destino).pages)
    # o h4 vai em caixa alta pelo CSS (como "SUGESTÃO", "CRITÉRIO DE ACEITE"); o texto em si, não
    assert "verifica" in texto.lower() and "Reproduzi seguindo o passo 2." in texto

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
    # MAW-0002 também afeta o main (está em seus alvos): não é algo que docs-z trouxe, então não
    # conta para a situação de docs-z, mesmo herdado — só o main vê todos os seus achados abertos.
    assert docs["semaforo"] == "pronto"


# ---------- semáforo da branch considera só o que ela traz além do main ----------

def test_achado_presente_tambem_no_main_nao_bloqueia_a_branch(tmp_path):
    s = _sprint(tmp_path)
    (s / "builds").mkdir()
    for nome in ("main", "feature-x"):
        (s / "builds" / f"{nome}-Release.json").write_text(json.dumps({"ok": True, "segundos": 1, "avisos": []}))
    # achado grave, mas presente nos dois alvos: não foi a branch que introduziu, já existe no main
    comum = dict(ACHADO, id="MAW-0005", introduzido_por=None,
                 alvos=[{"alvo": "feature-x", "commit": "b" * 40}, {"alvo": "main", "commit": "a" * 40}])
    (s / "achados.json").write_text(json.dumps([comum]))
    ctx = contexto.montar(s, ITENS, [], None, None)
    por_nome = {a["nome"]: a["semaforo"] for a in ctx["alvos"]}
    assert por_nome["main"] == "bloqueado"        # o main considera todos os seus achados abertos
    assert por_nome["feature-x"] == "pronto"      # a branch não trouxe nada além do que já existe no main


def test_branch_com_apenas_melhoria_aberta_fica_com_ressalvas(tmp_path):
    s = _sprint(tmp_path)
    (s / "builds").mkdir()
    (s / "builds" / "feature-x-Release.json").write_text(json.dumps({"ok": True, "segundos": 1, "avisos": []}))
    melhoria = dict(ACHADO, id="MAW-0006", tipo="melhoria", estado="novo")
    (s / "achados.json").write_text(json.dumps([melhoria]))
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert {a["nome"]: a["semaforo"] for a in ctx["alvos"]}["feature-x"] == "com_ressalvas"


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


def test_metodo_e_limitacao_de_restauracao_adiada_ou_descartada(tmp_path):
    s = _sprint(tmp_path)
    motivo = "restauração do %APPDATA%\\MAW adiada: MAW aberta — feche a MAW e rode `sprint iniciar` de novo"
    (s / "limitacoes-ambiente.json").write_text(json.dumps([motivo]), encoding="utf-8")
    _estado(s, FASES_OK, restauracoes=[
        {"quem": "retomada", "backup": "B1", "verificado": False, "adiada": True, "erro": motivo,
         "backup_apagado": False},
        {"quem": "retomada", "backup": "B2", "verificado": True, "backup_apagado": True,
         "descartado": "o backup mais antigo, de sprint-01, é o estado original e já foi restaurado"}])
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert motivo in ctx["limitacoes"]  # sem prefixo de agente
    m1, m2 = ctx["metodo"]
    assert "B1" in m1 and "adiada" in m1 and "MAW estava aberta" in m1
    assert "sem ser restaurado" in m2 and "sprint-01" in m2


# ---------- bancada do usuário e calibração ----------

def _bancada_falsa(raiz: Path) -> Path:
    """work/bancada falso: duas seções (na ordem do meta) e três testes."""
    b = raiz / "bancada"
    (b / "testes").mkdir(parents=True)
    (b / "meta").mkdir()
    (b / "meta" / "secoes.json").write_text(json.dumps({"lista": [
        {"id": "inicio", "titulo": "Abrir e configurar"}, {"id": "gravar", "titulo": "Gravar e tocar"}]}),
        encoding="utf-8")
    testes = {"abrir": ("Abrir a programa", "inicio", 1), "placa": ("Escolher a placa", "inicio", 2),
              "gravar-voz": ("Gravar a voz", "gravar", 1)}
    for codigo, (titulo, secao, ordem) in testes.items():
        (b / "testes" / f"{codigo}.json").write_text(json.dumps({"titulo": titulo, "secao": secao, "ordem": ordem}),
                                                    encoding="utf-8")
    return b


def test_bancada_agrupa_por_secao_com_estado_nota_e_contagens(tmp_path):
    s = _sprint(tmp_path)
    b = _bancada_falsa(tmp_path / "work")
    (s / "bancada.json").write_text(json.dumps({
        "placa": {"estado": "problema", "nota": "a lista de placas veio vazia <script>", "alvo": "main"},
        "gravar-voz": {"estado": "pulei", "nota": "sem entrada com sinal conhecido", "alvo": "main"},
        "fantasma": {"estado": "passou", "nota": "", "alvo": "main"}}), encoding="utf-8")
    ctx = contexto.montar(s, ITENS, [], None, None, pasta_bancada=b)
    ban = ctx["bancada"]
    assert [x["titulo"] for x in ban["secoes"]] == ["Abrir e configurar", "Gravar e tocar"]
    inicio = ban["secoes"][0]
    assert [(t["codigo"], t["estado"]) for t in inicio["testes"]] == [("abrir", "sem resultado"), ("placa", "problema")]
    assert inicio["contagens"] == {"passou": 0, "problema": 1, "pulei": 0, "sem resultado": 1}
    assert ban["contagens"] == {"passou": 0, "problema": 1, "pulei": 1, "sem resultado": 1} and ban["total"] == 3
    assert any("fantasma" in l for l in ban["linhas"])  # resultado fora da lista não some
    html = pdf._html(ctx)
    assert "Bancada do usuário" in html and "&lt;script&gt;" in html and "<script>" not in html
    destino = pdf.renderizar(ctx, s / "bancada.pdf")
    texto = "\n".join(p.extract_text() for p in PdfReader(destino).pages)
    assert "Bancada do usuário" in texto and "Escolher a placa" in texto and "a lista de placas veio vazia" in texto
    assert "sem resultado" in texto


def test_bancada_sem_dados_diz_que_nao_rodou(tmp_path):
    s = _sprint(tmp_path)
    ctx = contexto.montar(s, ITENS, [], None, None, pasta_bancada=tmp_path / "nao-existe")
    ban = ctx["bancada"]
    assert ban["secoes"] == [] and ban["linhas"]
    assert any("não rodou" in l for l in ban["linhas"])
    html = pdf._html(ctx)
    secao = html.split("Bancada do usuário", 1)[1].split("<section", 1)[0]
    assert "não rodou" in secao


def test_bancada_sem_bancada_json_lista_tudo_sem_resultado(tmp_path):
    s = _sprint(tmp_path)
    ctx = contexto.montar(s, ITENS, [], None, None, pasta_bancada=_bancada_falsa(tmp_path / "work"))
    ban = ctx["bancada"]
    assert ban["contagens"]["sem resultado"] == 3 and any("não rodou" in l for l in ban["linhas"])


def test_bancada_json_ilegivel_vira_erro_do_agente(tmp_path):
    s = _sprint(tmp_path)
    (s / "bancada.json").write_text("{quebrado", encoding="utf-8")
    ctx = contexto.montar(s, ITENS, [], None, None, pasta_bancada=_bancada_falsa(tmp_path / "work"))
    assert any("bancada.json" in e for e in ctx["erros_agente"])
    assert ctx["bancada"]["contagens"]["sem resultado"] == 3


CALIBRACAO = {
    "historico-eq": {"detectado": True, "blocos_que_falharam": ["Equalizador → coeficientes", "SONDA Alocacao → eq"],
                     "incoerencias": [], "como_esperado": True, "erro": None, "origem": "historico"},
    "plantado-marcador": {"detectado": False, "blocos_que_falharam": [], "incoerencias": [], "como_esperado": False,
                          "erro": None, "origem": "plantado"},
    "plantado-quebrado": {"detectado": False, "blocos_que_falharam": [], "incoerencias": [], "como_esperado": None,
                          "erro": "não compilou com o defeito: C2065", "origem": "plantado"}}


def test_calibracao_mostra_a_taxa_e_quem_pegou_cada_defeito(tmp_path):
    s = _sprint(tmp_path)
    (s / "calibracao.json").write_text(json.dumps(CALIBRACAO, ensure_ascii=False), encoding="utf-8")
    _estado(s, dict(FASES_OK, calibrar={"status": "concluido", "detalhe": {
        "controle": {"erro": None, "blocos_que_falharam": ["Opcoes de exportacao → mp3"]}}}))
    ctx = contexto.montar(s, ITENS, [], None, None)
    cal = ctx["calibracao"]
    assert cal["rodou"] is True and cal["taxa"]["detectados"] == 1 and cal["taxa"]["validos"] == 2
    assert "1 de 2" in cal["texto_taxa"] and "1 defeito(s) fora da taxa" in cal["texto_taxa"]
    por_id = {d["id"]: d for d in cal["defeitos"]}
    assert por_id["historico-eq"]["situacao"] == "detectado"
    assert por_id["historico-eq"]["blocos"] == ["Equalizador → coeficientes", "SONDA Alocacao → eq"]
    assert por_id["plantado-marcador"]["situacao"] == "não detectado"
    assert por_id["plantado-quebrado"]["situacao"] == "erro" and "C2065" in por_id["plantado-quebrado"]["erro"]
    assert any("Opcoes de exportacao" in l for l in cal["linhas"])
    destino = pdf.renderizar(ctx, s / "calibracao.pdf")
    texto = "\n".join(p.extract_text() for p in PdfReader(destino).pages)
    assert "Calibração" in texto and "1 de 2" in texto and "Equalizador" in texto


def test_calibracao_ausente_diz_que_nao_rodou(tmp_path):
    s = _sprint(tmp_path)
    _estado(s, dict(FASES_OK, calibrar={"status": "falhou", "erro": "MAW aberta"}))
    ctx = contexto.montar(s, ITENS, [], None, None)
    cal = ctx["calibracao"]
    assert cal["rodou"] is False and any("não rodou" in l and "MAW aberta" in l for l in cal["linhas"])
    html = pdf._html(ctx)
    secao = html.split("Calibração</h2>", 1)[1].split("<section", 1)[0]
    assert "não rodou" in secao


def test_calibracao_parcial_e_ilegivel(tmp_path):
    s = _sprint(tmp_path)
    (s / "calibracao.json").write_text(json.dumps({"historico-eq": CALIBRACAO["historico-eq"]}), encoding="utf-8")
    _estado(s, dict(FASES_OK, calibrar={"status": "em_andamento"}))
    cal = contexto.montar(s, ITENS, [], None, None)["calibracao"]
    assert any("parcial" in l for l in cal["linhas"])
    (s / "calibracao.json").write_text("[1, 2]", encoding="utf-8")
    ctx = contexto.montar(s, ITENS, [], None, None)
    assert ctx["calibracao"]["rodou"] is False and any("calibracao.json" in e for e in ctx["erros_agente"])


def test_saude_tecnica_separa_as_sondas_da_suite(tmp_path):
    s = _sprint(tmp_path)
    (s / "suites").mkdir()
    (s / "suites" / "main.json").write_text(json.dumps({
        "blocos": [{"nome": "Bloco A", "sub": "x", "ok": 5, "falhas": 0}], "total_ok": 5, "total_falhas": 0,
        "assercoes": [], "captura_erro": None, "preambulo": [],
        "sondas": {"blocos": [{"nome": "SONDA X", "sub": "y", "ok": 1, "falhas": 2}], "total_ok": 1,
                   "total_falhas": 2, "detalhes": []}}))
    html = pdf._html(contexto.montar(s, ITENS, [], None, None))
    assert "5 ok / 0 falhas em 1 blocos" in html and "sondas do agente: 1 ok / 2 falhas em 1 blocos" in html


def test_css_do_modelo_nao_e_escapado_mas_o_conteudo_e(tmp_path):
    s = _sprint(tmp_path)
    (s / "achados.json").write_text(json.dumps([dict(ACHADO, titulo='Título com "aspas" e <b>tag</b>')]))
    html = pdf._html(contexto.montar(s, ITENS, [], None, None, pasta_bancada=tmp_path / "x"))
    assert 'font-family: "Segoe UI"' in html  # o CSS é do próprio agente: sai como está
    assert "<b>tag</b>" not in html and "&lt;b&gt;tag&lt;/b&gt;" in html  # autoescape continua ligado


def test_titulo_do_pdf_e_do_roadie(tmp_path):
    s = _sprint(tmp_path)
    ctx = contexto.montar(s, ITENS, [], None, None, pasta_bancada=tmp_path / "x")
    assert ctx["titulo"] == "Roadie — relatório da Sprint 01" and ctx["subtitulo"] == "agente de testes da MAW"
    html = pdf._html(ctx)
    assert "<title>Roadie — relatório da Sprint 01</title>" in html
    assert "Roadie — relatório da Sprint 01" in pdf._rodape(ctx)
    destino = pdf.renderizar(ctx, s / "MAW-Sprint-01.pdf")  # o nome do arquivo não muda
    leitor = PdfReader(destino)
    texto = "\n".join(p.extract_text() for p in leitor.pages)
    assert "Roadie" in texto and "relatório da Sprint 01" in texto and "agente de testes da MAW" in texto
    assert leitor.metadata.title == "Roadie — relatório da Sprint 01"
