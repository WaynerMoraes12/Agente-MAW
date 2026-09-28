"""Integração da fase `e2e` com o estado da sprint: descoberta em privado/cenarios, resultados no
catálogo, achado bruto por 'problema', bancada.json e retomada por (alvo, cenário). Cenários são
arquivos falsos escritos em disco (nunca abre a MAW)."""
import json
import textwrap
from pathlib import Path

import pytest
import yaml

from maw_agent import achados, bancada, catalogo, config, estado, fases, sandbox
from maw_agent import fase_e2e

_BINARIO_DA_SPRINT = fase_e2e._binario_da_sprint  # a real, antes da fixture automática trocar

ITENS = [
    {"id": "area/x", "area": "area", "titulo": "t", "descricao": "d", "origem": ["x"], "verificacao": ["e2e"],
     "cenarios": [], "requisitos": [], "marco": "M2"},
    {"id": "area/y", "area": "area", "titulo": "t", "descricao": "d", "origem": ["x"], "verificacao": ["e2e"],
     "cenarios": [], "requisitos": [], "marco": "M2"},
    {"id": "area/z", "area": "area", "titulo": "t", "descricao": "d", "origem": ["x"], "verificacao": ["e2e"],
     "cenarios": [], "requisitos": [], "marco": "M2"},
]


class Args:
    def __init__(self, **kw):
        self.alvo = None
        self.cenario = []
        self.arquivo = []
        self.avulso = False
        self.limite = None
        self.__dict__.update(kw)


def _resultados(e):
    return catalogo.carregar_resultados(e.pasta)


def _escrever(pasta: Path, nome: str, texto: str) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / nome).write_text(textwrap.dedent(texto), encoding="utf-8")


@pytest.fixture(autouse=True)
def _sem_relancar(monkeypatch):
    """Aqui a fase roda no próprio processo (de dia ela se relançaria no desktop oculto)."""
    monkeypatch.setattr(fase_e2e, "_precisa_relancar", lambda args: False)


@pytest.fixture(autouse=True)
def _binario_da_sprint_valido(monkeypatch):
    """As sprints falsas destes testes não compilam nada: por padrão o Release conta como desta sprint.
    O caso do binário velho tem teste próprio (test_alvo_sem_release_desta_sprint_...)."""
    monkeypatch.setattr(fase_e2e, "_binario_da_sprint", lambda e, nome: (True, ""))


@pytest.fixture
def sprint(tmp_path, monkeypatch):
    raiz = tmp_path / "relatorios"
    monkeypatch.setattr(config, "RELATORIOS", raiz)
    monkeypatch.setattr(config, "PRIVADO", tmp_path / "privado")
    monkeypatch.setattr(config, "ALVOS_DIR", tmp_path / "alvos")
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    cat = tmp_path / "funcionalidades.yaml"
    cat.write_text(yaml.safe_dump(ITENS, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    e = estado.nova_sprint(raiz)
    monkeypatch.setattr(fases, "_sprint_atual", lambda: estado.carregar(e.pasta))
    sandbox.escrever_json(e.pasta / "alvos.json", {"alvos": [
        {"nome": "main", "branch": "main", "ref": "origin/main", "commit": "a" * 40, "origem": "github",
         "assinatura": "s", "compartilha_com": None},
        {"nome": "ramo-b", "branch": "ramo-b", "ref": "origin/ramo-b", "commit": "b" * 40, "origem": "github",
         "assinatura": "s2", "compartilha_com": None},
        {"nome": "docs-z", "branch": "docs/z", "ref": "origin/docs/z", "commit": "c" * 40, "origem": "github",
         "assinatura": "s", "compartilha_com": "main"},
    ], "avisos": []})
    return e


CENARIO_PASSA = '''
    from maw_agent.e2e import cenario, Resultado

    @cenario(id="area/passa", itens=["area/x"], bancada=["cod-passa"])
    def test_passa(ctx):
        if ctx.alvo != "main":
            return Resultado("problema", f"nao deveria contar pra bancada: {ctx.alvo}", obtido="branch")
        return Resultado("passou", "tudo ok")
'''

CENARIO_FALHA = '''
    from maw_agent.e2e import cenario, Resultado

    @cenario(id="area/falha", itens=["area/y"], bancada=["cod-falha"])
    def test_falha(ctx):
        return Resultado("problema", "quebrou", esperado="deveria abrir", obtido="ficou preto")
'''

CENARIO_PULA = '''
    from maw_agent.e2e import cenario, Resultado

    @cenario(id="area/pula", itens=["area/z"], bancada=["cod-pula"], requisitos=["midi-loopback"])
    def test_pula(ctx):
        raise AssertionError("nao deveria rodar: requisito ausente")
'''

CENARIO_RAMO_B = '''
    from maw_agent.e2e import cenario, Resultado

    @cenario(id="proxima/ramo-b", alvos=["ramo-b"], bancada=["p-ramo-b"])
    def test_ramo_b(ctx):
        return Resultado("passou", "ok-ramo-b")
'''

CENARIO_ERRO_AGENTE = '''
    from maw_agent.e2e import cenario, Resultado

    @cenario(id="area/quebra-agente", itens=["area/z"], bancada=["cod-erro-agente"], alvos=["main"])
    def test_quebra(ctx):
        raise RuntimeError("bug no cenario, nao na MAW")
'''

CENARIO_PRECISA_SOM = '''
    from maw_agent.e2e import cenario, Resultado

    @cenario(id="area/som", itens=["area/x"], requisitos=["som"], alvos=["main"])
    def test_som(ctx):
        return Resultado("passou", "tocou")
'''

CENARIO_PRECISA_ENTRADA_REAL = '''
    from maw_agent.e2e import cenario, Resultado

    @cenario(id="area/entrada-real", itens=["area/x"], requisitos=["entrada_real"], alvos=["main"])
    def test_entrada(ctx):
        return Resultado("passou", "clicou de verdade")
'''

CENARIO_PRECISA_LOOPBACK = '''
    from maw_agent.e2e import cenario, Resultado

    @cenario(id="area/loopback", requisitos=["loopback"], alvos=["main"])
    def test_loop(ctx):
        return Resultado("passou", "gravou")
'''


# ---------- registro no catálogo e achado bruto ----------

def test_registra_passou_e_falhou_por_alvo(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    _escrever(config.PRIVADO / "cenarios", "falha.py", CENARIO_FALHA)
    assert fase_e2e.cmd_e2e(Args()) == 1  # problema em algum cenário -> saída não-ok
    r = _resultados(sprint)
    assert r[("area/x", "main")]["resultado"] == "passou"
    assert r[("area/y", "main")]["resultado"] == "falhou" and r[("area/y", "main")]["motivo"] == "quebrou"
    # roda em todos os alvos (alvos="todos" é o padrão) menos o que compartilha árvore
    assert r[("area/x", "ramo-b")]["resultado"] == "falhou"  # a própria cenario_passa admite isso fora de main
    # docs-z nunca roda (compartilha árvore com main) mas herda a célula de 'e2e' de main
    assert r[("area/x", "docs-z")] == {"item": "area/x", "alvo": "docs-z", "resultado": "passou",
                                       "motivo": "mesma árvore de código de main", "achados": [], "fonte": "e2e"}


def test_achado_bruto_so_para_problema(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    _escrever(config.PRIVADO / "cenarios", "falha.py", CENARIO_FALHA)
    fase_e2e.cmd_e2e(Args())
    brutos = sprint.pasta / "achados-brutos"
    arq = brutos / "e2e-main-area-falha.json"
    assert arq.exists()
    dado = json.loads(arq.read_text(encoding="utf-8"))
    assert dado["fonte"] == "e2e" and dado["tipo"] == "bug" and dado["item_catalogo"] == "area/y"
    assert achados.validar(dado) == []
    assert not (brutos / "e2e-main-area-passa.json").exists()


def test_pulei_por_requisito_ausente_nao_roda_e_registra_no_catalogo(sprint, tmp_path):
    s = estado.carregar(sprint.pasta)
    s.concluir("preflight", {"requisitos_ausentes": ["midi-loopback"]})
    _escrever(config.PRIVADO / "cenarios", "pula.py", CENARIO_PULA)
    assert fase_e2e.cmd_e2e(Args()) == 0  # pulei não é 'problema': saída ok
    r = _resultados(sprint)
    assert r[("area/z", "main")]["resultado"] == "nao_testavel"
    assert "midi-loopback" in r[("area/z", "main")]["motivo"]
    assert bancada.carregar(sprint.pasta)["cod-pula"]["estado"] == "pulei"
    assert not (sprint.pasta / "achados-brutos").exists()


# ---------- bancada: só main conta para um cenário que mira main; branch-only usa o próprio alvo ----------

def test_bancada_so_recebe_da_execucao_em_main(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    fase_e2e.cmd_e2e(Args())
    assert bancada.carregar(sprint.pasta) == {"cod-passa": {"estado": "passou", "nota": "tudo ok", "alvo": "main"}}


def test_bancada_cenario_de_branch_usa_o_proprio_alvo(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "ramo-b.py", CENARIO_RAMO_B)
    fase_e2e.cmd_e2e(Args())
    assert bancada.carregar(sprint.pasta) == {"p-ramo-b": {"estado": "passou", "nota": "ok-ramo-b", "alvo": "ramo-b"}}
    s = estado.carregar(sprint.pasta)
    assert not any(p.startswith("e2e:main:") or p.startswith("e2e:docs-z:") for p in s.passos)


# ---------- retomada ----------

def test_retomada_nao_roda_de_novo(sprint, tmp_path):
    marcador = tmp_path / "chamadas.txt"
    _escrever(config.PRIVADO / "cenarios", "passa.py", f'''
        from pathlib import Path
        from maw_agent.e2e import cenario, Resultado

        @cenario(id="area/passa", itens=["area/x"])
        def test_passa(ctx):
            with open(r"{marcador}", "a", encoding="utf-8") as f:
                f.write(ctx.alvo + "\\n")
            return Resultado("passou", "ok")
    ''')
    fase_e2e.cmd_e2e(Args())
    primeira = marcador.read_text(encoding="utf-8").splitlines()
    assert sorted(primeira) == ["main", "ramo-b"]
    fase_e2e.cmd_e2e(Args())
    segunda = marcador.read_text(encoding="utf-8").splitlines()
    assert segunda == primeira  # nada rodou de novo
    s = estado.carregar(sprint.pasta)
    assert s.feito("e2e")


# ---------- filtro --alvo e alvos que compartilham árvore ----------

def test_agregado_so_fecha_quando_todos_os_alvos_da_sprint_rodaram(sprint, tmp_path):
    """Regressão: `--alvo` filtra quem roda, mas o passo agregado 'e2e' só pode fechar quando TODOS
    os alvos da sprint (sem contar quem compartilha árvore) já rodaram — nunca a partir da lista
    filtrada (mesmo padrão de `fases._compilar`)."""
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    fase_e2e.cmd_e2e(Args(alvo="ramo-b"))
    assert not estado.carregar(sprint.pasta).feito("e2e")  # main ainda não rodou
    fase_e2e.cmd_e2e(Args())  # sem filtro: cobre main (ramo-b já feito é retomado, não roda de novo)
    assert estado.carregar(sprint.pasta).feito("e2e")


def test_filtro_por_alvo_restringe_execucao_e_bancada(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    _escrever(config.PRIVADO / "cenarios", "ramo-b.py", CENARIO_RAMO_B)
    fase_e2e.cmd_e2e(Args(alvo="ramo-b"))
    r = _resultados(sprint)
    assert ("area/x", "main") not in r
    assert r[("area/x", "ramo-b")]["resultado"] == "falhou"  # rodou só em ramo-b, e ali "passa" dá problema
    assert "cod-passa" not in bancada.carregar(sprint.pasta)  # main nunca rodou: bancada não recebe nada dele
    assert bancada.carregar(sprint.pasta)["p-ramo-b"]["alvo"] == "ramo-b"


def test_alvo_que_compartilha_arvore_nunca_roda_mas_herda_a_celula(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    _escrever(config.PRIVADO / "cenarios", "falha.py", CENARIO_FALHA)
    fase_e2e.cmd_e2e(Args())
    s = estado.carregar(sprint.pasta)
    assert not any(p.startswith("e2e:docs-z:") for p in s.passos)  # nunca roda de novo
    r = _resultados(sprint)
    # herda tanto o que passou quanto o que falhou, com o motivo original de main anexado
    assert r[("area/x", "docs-z")]["resultado"] == "passou"
    assert r[("area/x", "docs-z")]["motivo"] == "mesma árvore de código de main"
    assert r[("area/y", "docs-z")]["resultado"] == "falhou"
    assert r[("area/y", "docs-z")]["motivo"] == "mesma árvore de código de main: quebrou"
    assert r[("area/y", "docs-z")]["fonte"] == "e2e"


def test_heranca_so_copia_celulas_de_fonte_e2e(sprint, tmp_path):
    """A herança de `_propagar_heranca` é só para as células que a própria fase e2e gravou nesta
    sprint — não deve pegar carona em resultados de outra fonte (ex.: 'suite')."""
    catalogo.registrar_resultado(sprint.pasta, "area/x", "main", "falhou", "erro de suite", fonte="suite")
    fase_e2e.cmd_e2e(Args())
    assert ("area/x", "docs-z") not in _resultados(sprint)


def _linhas_de(pasta, item, alvo):
    arq = pasta / "resultados.jsonl"
    if not arq.exists():
        return []
    return [l for l in arq.read_text(encoding="utf-8").splitlines()
            if json.loads(l).get("item") == item and json.loads(l).get("alvo") == alvo]


def test_heranca_e_idempotente_entre_chamadas(sprint, tmp_path):
    """Regressão: uma segunda `cmd_e2e` na mesma sprint (ex.: retomada, com tudo já `feito`) não pode
    acrescentar outra linha idêntica herdada a cada vez que roda."""
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    fase_e2e.cmd_e2e(Args())
    apos_primeira = _linhas_de(sprint.pasta, "area/x", "docs-z")
    assert len(apos_primeira) == 1
    fase_e2e.cmd_e2e(Args())  # tudo já feito: só a herança roda de novo
    fase_e2e.cmd_e2e(Args())  # e de novo, por garantia
    apos_repetir = _linhas_de(sprint.pasta, "area/x", "docs-z")
    assert apos_repetir == apos_primeira  # nenhuma linha nova


def test_sem_cenarios_nao_falha(sprint):
    assert fase_e2e.cmd_e2e(Args()) == 0


# ---------- erro do agente: nunca vira achado contra a MAW (regra 5 do CLAUDE.md) ----------

def test_erro_do_agente_vira_pulei_sem_achado_e_registra_limitacao(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "quebra.py", CENARIO_ERRO_AGENTE)
    assert fase_e2e.cmd_e2e(Args()) == 0  # erro do agente não é 'problema' da MAW: saída ok
    r = _resultados(sprint)
    assert r[("area/z", "main")]["resultado"] == "nao_testavel"
    assert r[("area/z", "main")]["motivo"].startswith("erro do agente no cenário:")
    assert bancada.carregar(sprint.pasta)["cod-erro-agente"] == {
        "estado": "pulei", "nota": r[("area/z", "main")]["motivo"], "alvo": "main"}
    assert not (sprint.pasta / "achados-brutos").exists()  # nenhuma ficha contra a MAW
    lim = json.loads((sprint.pasta / "limitacoes-e2e.json").read_text(encoding="utf-8"))
    assert len(lim) == 1
    assert "area/quebra-agente" in lim[0] and "RuntimeError" in lim[0] and "bug no cenario" in lim[0]


def test_problema_sem_obtido_nao_vira_achado(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "manco.py", '''
        from maw_agent.e2e import cenario, Resultado

        @cenario(id="area/manco", itens=["area/z"], alvos=["main"])
        def test_manco(ctx):
            return Resultado("problema", "quebrou mas nao digo onde")
    ''')
    fase_e2e.cmd_e2e(Args())
    assert _resultados(sprint)[("area/z", "main")]["resultado"] == "nao_testavel"
    assert not (sprint.pasta / "achados-brutos").exists()
    lim = json.loads((sprint.pasta / "limitacoes-e2e.json").read_text(encoding="utf-8"))
    assert any("sem 'obtido'" in l for l in lim)


# ---------- --cenario / --arquivo: filtro, e o agregado espera o conjunto inteiro ----------

def test_filtro_por_cenario_glob(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    _escrever(config.PRIVADO / "cenarios", "falha.py", CENARIO_FALHA)
    fase_e2e.cmd_e2e(Args(cenario=["area/pass*"]))
    r = _resultados(sprint)
    assert ("area/x", "main") in r
    assert ("area/y", "main") not in r


def test_filtro_por_arquivo(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    _escrever(config.PRIVADO / "cenarios", "falha.py", CENARIO_FALHA)
    fase_e2e.cmd_e2e(Args(arquivo=["falha.py"]))
    r = _resultados(sprint)
    assert ("area/y", "main") in r
    assert ("area/x", "main") not in r


def test_filtro_por_cenario_nao_fecha_o_agregado(sprint, tmp_path):
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    _escrever(config.PRIVADO / "cenarios", "falha.py", CENARIO_FALHA)
    fase_e2e.cmd_e2e(Args(cenario=["area/pass*"]))
    assert not estado.carregar(sprint.pasta).feito("e2e")  # 'falha' nunca rodou por causa do filtro


# ---------- ordem: main primeiro, mesmo que venha depois em alvos.json ----------

def test_main_roda_primeiro_mesmo_vindo_depois_no_alvos_json(sprint, tmp_path):
    sandbox.escrever_json(sprint.pasta / "alvos.json", {"alvos": [
        {"nome": "ramo-b", "branch": "ramo-b", "ref": "origin/ramo-b", "commit": "b" * 40, "origem": "github",
         "assinatura": "s2", "compartilha_com": None},
        {"nome": "main", "branch": "main", "ref": "origin/main", "commit": "a" * 40, "origem": "github",
         "assinatura": "s", "compartilha_com": None},
    ], "avisos": []})
    marcador = tmp_path / "ordem.txt"
    _escrever(config.PRIVADO / "cenarios", "ordem.py", f'''
        from maw_agent.e2e import cenario, Resultado

        @cenario(id="area/ordem", itens=["area/x"])
        def test_ordem(ctx):
            with open(r"{marcador}", "a", encoding="utf-8") as f:
                f.write(ctx.alvo + "\\n")
            return Resultado("passou", "ok")
    ''')
    fase_e2e.cmd_e2e(Args())
    assert marcador.read_text(encoding="utf-8").splitlines() == ["main", "ramo-b"]


# ---------- requisitos som/entrada_real (env) e loopback (preflight, no avulso) ----------

def test_som_ausente_por_padrao(sprint, tmp_path, monkeypatch):
    monkeypatch.delenv("MAW_AGENTE_SOM", raising=False)
    _escrever(config.PRIVADO / "cenarios", "som.py", CENARIO_PRECISA_SOM)
    fase_e2e.cmd_e2e(Args())
    r = _resultados(sprint)[("area/x", "main")]
    assert r["resultado"] == "nao_testavel" and "som" in r["motivo"]


def test_som_liberado_com_env(sprint, tmp_path, monkeypatch):
    monkeypatch.setenv("MAW_AGENTE_SOM", "1")
    _escrever(config.PRIVADO / "cenarios", "som.py", CENARIO_PRECISA_SOM)
    fase_e2e.cmd_e2e(Args())
    assert _resultados(sprint)[("area/x", "main")]["resultado"] == "passou"


def test_entrada_real_ausente_por_padrao(sprint, tmp_path, monkeypatch):
    monkeypatch.delenv("MAW_AGENTE_ENTRADA_REAL", raising=False)
    _escrever(config.PRIVADO / "cenarios", "entrada.py", CENARIO_PRECISA_ENTRADA_REAL)
    fase_e2e.cmd_e2e(Args())
    r = _resultados(sprint)[("area/x", "main")]
    assert r["resultado"] == "nao_testavel" and "entrada_real" in r["motivo"]


def test_entrada_real_liberada_com_env(sprint, tmp_path, monkeypatch):
    monkeypatch.setenv("MAW_AGENTE_ENTRADA_REAL", "1")
    _escrever(config.PRIVADO / "cenarios", "entrada.py", CENARIO_PRECISA_ENTRADA_REAL)
    fase_e2e.cmd_e2e(Args())
    assert _resultados(sprint)[("area/x", "main")]["resultado"] == "passou"


# ---------- --limite: para de começar cenários novos, e registra a limitação ----------

def test_limite_para_de_comecar_cenarios_novos_e_registra_limitacao(sprint, tmp_path, monkeypatch):
    from datetime import datetime
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)  # roda em main e ramo-b
    # 1ª chamada: calcula o prazo (05:59 -> prazo é hoje às 06:00); as duas seguintes (uma por alvo,
    # main e ramo-b) já estão depois do prazo -> nenhum cenário novo começa.
    momentos = iter([datetime(2026, 1, 1, 5, 59), datetime(2026, 1, 1, 6, 0, 1), datetime(2026, 1, 1, 6, 0, 2)])
    monkeypatch.setattr(fase_e2e, "_agora_dt", lambda: next(momentos))
    fase_e2e.cmd_e2e(Args(limite="06:00"))
    assert _resultados(sprint) == {}
    assert not estado.carregar(sprint.pasta).feito("e2e:main:area/passa")
    lim = json.loads((sprint.pasta / "limitacoes-e2e.json").read_text(encoding="utf-8"))
    assert any("cenários não rodados por falta de tempo: main: 1" in l for l in lim)
    assert any("cenários não rodados por falta de tempo: ramo-b: 1" in l for l in lim)


def test_limite_nao_atrapalha_quando_ha_tempo_de_sobra(sprint, tmp_path, monkeypatch):
    from datetime import datetime
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    monkeypatch.setattr(fase_e2e, "_agora_dt", lambda: datetime(2026, 1, 1, 5, 0))  # bem antes das 6h
    fase_e2e.cmd_e2e(Args(limite="06:00"))
    assert _resultados(sprint)[("area/x", "main")]["resultado"] == "passou"
    assert not (sprint.pasta / "limitacoes-e2e.json").exists()


# ---------- --avulso: fora de qualquer sprint, não grava nada nela ----------

@pytest.fixture
def ambiente_avulso(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PRIVADO", tmp_path / "privado")
    monkeypatch.setattr(config, "ALVOS_DIR", tmp_path / "alvos")
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")  # não deveria ser tocado
    return tmp_path


def test_avulso_nao_precisa_de_sprint_nem_grava_nada_nela(ambiente_avulso, capsys, monkeypatch):
    monkeypatch.delenv("MAW_AGENTE_SOM", raising=False)
    monkeypatch.delenv("MAW_AGENTE_ENTRADA_REAL", raising=False)
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    codigo = fase_e2e.cmd_e2e(Args(avulso=True))
    assert codigo == 0
    saida = json.loads(capsys.readouterr().out)
    assert len(saida["execucoes"]) == 1
    exe = saida["execucoes"][0]
    assert exe["id"] == "area/passa" and exe["alvo"] == "main" and exe["estado"] == "passou"
    assert exe["esperado"] is None and exe["obtido"] is None  # só quando 'problema'
    assert not config.RELATORIOS.exists()  # nenhuma sprint tocada
    pasta_evid = Path(saida["pasta_evidencias"])
    assert pasta_evid.is_dir() and str(pasta_evid).startswith(str(config.WORK / "e2e-avulso"))


def test_avulso_mostra_esperado_obtido_so_quando_problema(ambiente_avulso, capsys):
    _escrever(config.PRIVADO / "cenarios", "falha.py", CENARIO_FALHA)
    fase_e2e.cmd_e2e(Args(avulso=True))
    exe = json.loads(capsys.readouterr().out)["execucoes"][0]
    assert exe["estado"] == "problema"
    assert exe["esperado"] == "deveria abrir" and exe["obtido"] == "ficou preto"


def test_avulso_respeita_alvo_explicito(ambiente_avulso, capsys):
    _escrever(config.PRIVADO / "cenarios", "ramo-b.py", CENARIO_RAMO_B)
    fase_e2e.cmd_e2e(Args(avulso=True, alvo="ramo-b"))
    exe = json.loads(capsys.readouterr().out)["execucoes"][0]
    assert exe["alvo"] == "ramo-b" and exe["estado"] == "passou"


def test_avulso_loopback_ausente_vira_pulei(ambiente_avulso, capsys, monkeypatch):
    from maw_agent import preflight
    monkeypatch.setattr(preflight, "_loopback_padrao", lambda: None)
    _escrever(config.PRIVADO / "cenarios", "loop.py", CENARIO_PRECISA_LOOPBACK)
    fase_e2e.cmd_e2e(Args(avulso=True))
    exe = json.loads(capsys.readouterr().out)["execucoes"][0]
    assert exe["estado"] == "pulei" and "loopback" in exe["nota"]


def test_avulso_loopback_presente_deixa_rodar(ambiente_avulso, capsys, monkeypatch):
    from maw_agent import preflight
    monkeypatch.setattr(preflight, "_loopback_padrao", lambda: "Alto-falantes (Loopback)")
    _escrever(config.PRIVADO / "cenarios", "loop.py", CENARIO_PRECISA_LOOPBACK)
    fase_e2e.cmd_e2e(Args(avulso=True))
    exe = json.loads(capsys.readouterr().out)["execucoes"][0]
    assert exe["estado"] == "passou"


def test_avulso_respeita_limite(ambiente_avulso, capsys, monkeypatch):
    from datetime import datetime
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    momentos = iter([datetime(2026, 1, 1, 5, 59), datetime(2026, 1, 1, 6, 0, 1)])
    monkeypatch.setattr(fase_e2e, "_agora_dt", lambda: next(momentos))
    fase_e2e.cmd_e2e(Args(avulso=True, limite="06:00"))
    saida = json.loads(capsys.readouterr().out)
    assert saida["execucoes"] == []
    assert saida["pulados_por_tempo"] == {"main": 1}


# ---------- binário da sprint ----------

def test_alvo_sem_release_desta_sprint_vira_pulei_sem_achado(sprint, tmp_path, monkeypatch):
    """Build que falhou ou foi cortado: a E2E não abre o exe velho; cada cenário vira pulei com o motivo,
    célula não testável, uma limitação, nenhum achado bruto; na bancada, pulei com o mesmo motivo."""
    monkeypatch.setattr(fase_e2e, "_binario_da_sprint", lambda e, nome: (False, "build falhou nesta sprint"))
    _escrever(config.PRIVADO / "cenarios", "passa.py", CENARIO_PASSA)
    fase_e2e.cmd_e2e(Args())
    assert not list((sprint.pasta / "achados-brutos").glob("*.json")) if (sprint.pasta / "achados-brutos").exists() else True
    b = bancada.carregar(sprint.pasta)
    assert b["cod-passa"]["estado"] == "pulei" and "build falhou nesta sprint" in b["cod-passa"]["nota"]
    lim = json.loads((sprint.pasta / "limitacoes-e2e.json").read_text(encoding="utf-8"))
    assert any("não ficou pronto" in l for l in lim)


def test_binario_da_sprint_usa_a_regra_da_suite(tmp_path, monkeypatch):
    chamadas = []
    monkeypatch.setattr(fases, "binario_valido", lambda b, exe, ini: chamadas.append((b, ini)) or (False, "x"))

    class E:
        pasta = tmp_path
        passos = {"compilar:main:Release": {"inicio": "2026-09-28T22:05:00"}}

    assert _BINARIO_DA_SPRINT(E(), "main") == (False, "x")
    assert chamadas == [(None, "2026-09-28T22:05:00")]


# ---------- gravações da sala ----------

def test_varrer_gravacoes_apaga_so_audio_dentro_de_pastas_audio(sprint, tmp_path):
    ev = sprint.pasta / "evidencias" / "main" / "gravar"
    (ev / "proj_Audio").mkdir(parents=True)
    (ev / "proj_Audio" / "take1.wav").write_bytes(b"RIFF")
    (ev / "proj_Audio" / "sub").mkdir()
    (ev / "proj_Audio" / "sub" / "take2.flac").write_bytes(b"fLaC")
    (ev / "captura.png").write_bytes(b"PNG")
    (ev / "exportado.wav").write_bytes(b"RIFF")  # fora de *_Audio: exportação medida, fica
    assert fase_e2e._varrer_gravacoes(sprint.pasta / "evidencias") == 2
    assert not (ev / "proj_Audio" / "take1.wav").exists() and not (ev / "proj_Audio" / "sub" / "take2.flac").exists()
    assert (ev / "captura.png").exists() and (ev / "exportado.wav").exists()
