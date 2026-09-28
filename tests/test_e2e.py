"""Motor de cenários E2E (decorator, descoberta, execução com timeout/exceção) — sem abrir a MAW:
os cenários usados aqui são falsos e o SessaoApp é sempre um dublê."""
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

import maw_agent
from maw_agent import achados, e2e

ALVO = {"nome": "main", "commit": "a" * 40}


def _ctx(tmp_path, alvo="main") -> e2e.Contexto:
    evid = tmp_path / "evidencias"
    evid.mkdir(parents=True, exist_ok=True)
    return e2e.Contexto(alvo=alvo, exe_release=tmp_path / "App" / "MAW_APP.exe",
                        pasta_alvo=tmp_path / "alvo", fixtures=tmp_path / "fixtures", evidencias=evid)


def _instalar_gui_falso(monkeypatch, modulo) -> None:
    """`from . import gui` (dentro de `maw_agent`) resolve pelo ATRIBUTO `maw_agent.gui`, não só
    por `sys.modules["maw_agent.gui"]`, assim que o `gui` de verdade tiver sido importado uma vez
    no processo (o que `e2e._entrada_real_necessaria` já faz a qualquer exceção). Só trocar
    `sys.modules` não bastava — qualquer teste depois do primeiro import real voltava a ver o
    `gui.py` de verdade. Por isso os dois pontos são trocados juntos aqui."""
    monkeypatch.setitem(sys.modules, "maw_agent.gui", modulo)
    monkeypatch.setattr(maw_agent, "gui", modulo, raising=False)
    monkeypatch.setattr(maw_agent, "gui", modulo, raising=False)


# ---------- Resultado ----------

def test_resultado_aceita_estados_validos():
    for estado in ("passou", "problema", "pulei"):
        assert e2e.Resultado(estado).estado == estado


def test_resultado_recusa_estado_invalido():
    with pytest.raises(ValueError):
        e2e.Resultado("talvez")


def test_resultado_limitacoes_e_kw_only():
    with pytest.raises(TypeError):
        e2e.Resultado("passou", "nota", "esperado", "obtido", [], "media", ["nao pode ser posicional"])
    assert e2e.Resultado("passou", limitacoes=["ok assim"]).limitacoes == ["ok assim"]


# ---------- Contexto ----------

def test_contexto_maw_importa_gui_de_forma_preguicosa(monkeypatch, tmp_path):
    chamadas = []
    modulo = types.ModuleType("maw_agent.gui")

    class SessaoFalsa:
        def __init__(self, exe, pasta_evidencias, **kw):
            chamadas.append((exe, pasta_evidencias, kw))

    modulo.SessaoApp = SessaoFalsa
    _instalar_gui_falso(monkeypatch, modulo)
    ctx = _ctx(tmp_path)
    sessao = ctx.maw(timeout_abrir=5)
    assert isinstance(sessao, SessaoFalsa)
    assert ctx.sessao_atual is sessao
    assert chamadas[0] == (ctx.exe_release, ctx.evidencias, {"timeout_abrir": 5, "estado": None})


def test_contexto_maw_passa_o_estado_da_sprint_para_a_sessao(monkeypatch, tmp_path):
    """O registro da restauração do %APPDATA%\\MAW precisa cair no MESMO Estado que a fase e2e já
    tem em mãos (e vai salvar depois) — senão a sessão registraria num Estado recarregado à parte, e
    o próximo `e.concluir()`/`e.salvar()` da fase sobrescreveria esse registro sem nunca o ter visto."""
    chamadas = []
    modulo = types.ModuleType("maw_agent.gui")

    class SessaoFalsa:
        def __init__(self, exe, pasta_evidencias, **kw):
            chamadas.append(kw)

    modulo.SessaoApp = SessaoFalsa
    _instalar_gui_falso(monkeypatch, modulo)
    estado_da_sprint = object()  # o objeto exato importa: precisa ser o MESMO, não um equivalente
    ctx = e2e.Contexto(alvo="main", exe_release=tmp_path / "x.exe", pasta_alvo=tmp_path,
                       fixtures=tmp_path, evidencias=tmp_path, estado=estado_da_sprint)
    ctx.maw()
    assert chamadas[0]["estado"] is estado_da_sprint


def test_contexto_maw_deixa_o_cenario_sobrescrever_o_estado(monkeypatch, tmp_path):
    chamadas = []
    modulo = types.ModuleType("maw_agent.gui")

    class SessaoFalsa:
        def __init__(self, exe, pasta_evidencias, **kw):
            chamadas.append(kw)

    modulo.SessaoApp = SessaoFalsa
    _instalar_gui_falso(monkeypatch, modulo)
    outro = object()
    ctx = e2e.Contexto(alvo="main", exe_release=tmp_path / "x.exe", pasta_alvo=tmp_path,
                       fixtures=tmp_path, evidencias=tmp_path, estado=object())
    ctx.maw(estado=outro)
    assert chamadas[0]["estado"] is outro


def test_contexto_audio_importa_modulo_de_audio(monkeypatch, tmp_path):
    modulo = types.ModuleType("maw_agent.audio")
    modulo.MARCADOR = "e sou eu"
    # mesmo motivo de `_instalar_gui_falso`: depois que outro teste importou o `audio` de verdade,
    # `from . import audio` resolve pelo atributo do pacote — os dois pontos são trocados juntos.
    monkeypatch.setitem(sys.modules, "maw_agent.audio", modulo)
    monkeypatch.setattr(maw_agent, "audio", modulo, raising=False)
    ctx = _ctx(tmp_path)
    assert ctx.audio.MARCADOR == "e sou eu"


def test_registrar_evidencia_acumula(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.registrar_evidencia(tmp_path / "a.png", "print da tela", True)
    ctx.registrar_evidencia(tmp_path / "b.png", "outro print", False)
    assert ctx.evidencias_registradas == [
        {"arquivo": str(tmp_path / "a.png"), "legenda": "print da tela", "embutir": True},
        {"arquivo": str(tmp_path / "b.png"), "legenda": "outro print", "embutir": False},
    ]


# ---------- decorator + descoberta ----------

_CENARIO_OK = '''
from maw_agent.e2e import cenario, Resultado

@cenario(id="{id}", itens=["area/x"])
def {fn}(ctx):
    return Resultado("passou", "ok")
'''


def _escrever(pasta, nome, texto):
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / nome).write_text(textwrap.dedent(texto), encoding="utf-8")


def test_descobrir_pasta_ausente_devolve_lista_vazia(tmp_path):
    assert e2e.descobrir(tmp_path / "nao-existe") == []


def test_descobrir_encontra_cenarios_em_varios_arquivos(tmp_path):
    raiz = tmp_path / "cenarios"
    _escrever(raiz / "area_a", "test_um.py", _CENARIO_OK.format(id="a/um", fn="test_um"))
    _escrever(raiz / "area_a", "test_dois.py",
             _CENARIO_OK.format(id="a/dois", fn="test_dois") +
             _CENARIO_OK.format(id="a/tres", fn="test_tres"))
    lista = e2e.descobrir(raiz)
    assert [c.id for c in lista] == ["a/dois", "a/tres", "a/um"]  # ordenado por (arquivo, id)
    assert all(callable(c.func) for c in lista)
    assert lista[0].itens == ["area/x"]


def test_descobrir_ignora_arquivos_comecando_com_underline(tmp_path):
    raiz = tmp_path / "cenarios"
    _escrever(raiz, "_ajuda.py", "raise RuntimeError('nao deveria importar')")
    assert e2e.descobrir(raiz) == []


def test_descobrir_nao_acumula_entre_chamadas(tmp_path):
    raiz = tmp_path / "cenarios"
    _escrever(raiz, "test_um.py", _CENARIO_OK.format(id="a/um", fn="test_um"))
    primeira = e2e.descobrir(raiz)
    segunda = e2e.descobrir(raiz)
    assert len(primeira) == 1 and len(segunda) == 1


def test_cenario_com_alvos_lista_fica_lista_e_nao_todos(tmp_path):
    raiz = tmp_path / "cenarios"
    _escrever(raiz, "test_branch.py", '''
        from maw_agent.e2e import cenario, Resultado

        @cenario(id="p/x", alvos=["ramo-b"], bancada=["p-ramo-b"], timeout=1)
        def test_branch(ctx):
            return Resultado("passou", "ok")
    ''')
    c = e2e.descobrir(raiz)[0]
    assert c.alvos == ["ramo-b"]
    assert c.bancada == ["p-ramo-b"]


def test_cenario_mira_main():
    c_todos = e2e.Cenario(id="x", func=lambda ctx: None, alvos="todos")
    c_lista_com_main = e2e.Cenario(id="y", func=lambda ctx: None, alvos=["main", "ramo-b"])
    c_so_branch = e2e.Cenario(id="z", func=lambda ctx: None, alvos=["ramo-b"])
    assert e2e.cenario_mira_main(c_todos) and e2e.cenario_mira_main(c_lista_com_main)
    assert not e2e.cenario_mira_main(c_so_branch)


# ---------- rodar_cenario: passou / problema / pulei / exceção / timeout / travamento ----------

def _cenario(id="x", func=None, **kw):
    return e2e.Cenario(id=id, func=func, **kw)


def test_rodar_cenario_passou(tmp_path):
    c = _cenario(func=lambda ctx: e2e.Resultado("passou", "tudo certo"))
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    assert r.estado == "passou" and r.nota == "tudo certo"


def test_rodar_cenario_prefixa_limitacoes_que_o_cenario_preencheu_por_conta_propria(tmp_path):
    """`limitacoes` é só de `rodar_cenario`; se um cenário preencher por conta própria (não deveria),
    a linha não se perde, só fica marcada como vinda do cenário."""
    c = _cenario(func=lambda ctx: e2e.Resultado("passou", "ok", limitacoes=["algo que o cenário notou"]))
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    assert r.limitacoes == ["(do cenário) algo que o cenário notou"]


def test_rodar_cenario_problema_passa_direto(tmp_path):
    c = _cenario(func=lambda ctx: e2e.Resultado("problema", "nao bateu", esperado="a", obtido="b"))
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    assert r.estado == "problema" and r.esperado == "a" and r.obtido == "b"


def test_rodar_cenario_pulado_por_requisito_ausente_nao_chama_func(tmp_path):
    chamou = []
    c = _cenario(func=lambda ctx: chamou.append(1), requisitos=["vb-cable"])
    r = e2e.rodar_cenario(c, _ctx(tmp_path), requisitos_ausentes={"vb-cable"})
    assert r.estado == "pulei" and "vb-cable" in r.nota
    assert chamou == []


def test_rodar_cenario_roda_quando_requisito_presente(tmp_path):
    c = _cenario(func=lambda ctx: e2e.Resultado("passou", "ok"), requisitos=["vb-cable"])
    r = e2e.rodar_cenario(c, _ctx(tmp_path), requisitos_ausentes=set())
    assert r.estado == "passou"


def test_rodar_cenario_excecao_e_erro_do_agente_nunca_problema(tmp_path):
    """Uma exceção é um bug do cenário/agente, não uma observação sobre a MAW: vira `pulei`, nunca
    `problema` (não pode virar achado bruto contra a MAW) — mas a evidência do traceback ainda é
    gravada em disco, e uma linha vai para `Resultado.limitacoes`."""
    def explode(ctx):
        raise ValueError("deu ruim de verdade")
    c = _cenario(id="area/explode", func=explode)
    ctx = _ctx(tmp_path)
    r = e2e.rodar_cenario(c, ctx)
    assert r.estado == "pulei"
    assert r.nota.startswith("erro do agente no cenário:")
    assert "ValueError" in r.nota and "deu ruim de verdade" in r.nota
    assert len(r.limitacoes) == 1
    assert "area/explode" in r.limitacoes[0] and "ValueError" in r.limitacoes[0]
    assert any("traceback" in ev["legenda"] for ev in ctx.evidencias_registradas)
    caminho_evidencia = ctx.evidencias_registradas[0]["arquivo"]
    assert "deu ruim de verdade" in open(caminho_evidencia, encoding="utf-8").read()


def test_rodar_cenario_entrada_real_necessaria_vira_pulei_sem_erro_de_agente(tmp_path):
    """`gui.EntradaRealNecessaria` é o cenário dizendo, do jeito certo, que só dá para rodar de
    verdade com teclado/mouse reais — não é um bug do agente, então não tem 'erro do agente' na
    nota nem linha de limitação (diferente de qualquer outra exceção)."""
    from maw_agent.gui import EntradaRealNecessaria

    def explode(ctx):
        raise EntradaRealNecessaria("'ctrl+z' tem modificador: só funciona com MAW_AGENTE_ENTRADA_REAL=1")
    c = _cenario(id="area/precisa-teclado", func=explode)
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    assert r.estado == "pulei"
    assert r.nota == ("exige teclado/mouse reais (roda na execução noturna): 'ctrl+z' tem "
                      "modificador: só funciona com MAW_AGENTE_ENTRADA_REAL=1")
    assert "erro do agente" not in r.nota
    assert r.limitacoes == []


def test_rodar_cenario_entrada_real_necessaria_por_nome_quando_gui_falha_ao_importar(monkeypatch, tmp_path):
    """Sem conseguir importar `gui` (hipotético — aqui ele existe, mas o código precisa aguentar a
    ausência), cai para casar pelo nome da classe."""
    # força ImportError em "from . import gui": None em sys.modules barra a reimportação, e tirar o
    # atributo do pacote (que pode já existir de um import real anterior no processo) tira o atalho
    # que o "from X import Y" tenta primeiro.
    monkeypatch.setitem(sys.modules, "maw_agent.gui", None)
    monkeypatch.delattr(maw_agent, "gui", raising=False)

    class EntradaRealNecessaria(Exception):
        pass

    def explode(ctx):
        raise EntradaRealNecessaria("sem entrada real")
    c = _cenario(func=explode)
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    assert r.estado == "pulei"
    assert r.nota == "exige teclado/mouse reais (roda na execução noturna): sem entrada real"
    assert r.limitacoes == []


def test_rodar_cenario_nao_devolve_resultado_e_erro_do_agente(tmp_path):
    c = _cenario(id="area/manco", func=lambda ctx: "isso nao e um Resultado")
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    assert r.estado == "pulei"
    assert r.nota.startswith("erro do agente no cenário:")
    assert len(r.limitacoes) == 1 and "area/manco" in r.limitacoes[0]


def test_rodar_cenario_problema_sem_obtido_e_erro_do_agente(tmp_path):
    """`problema` sem `obtido` não tem evidência do defeito: é o cenário que não fez seu trabalho,
    não a MAW que falhou — vira `pulei`, sem virar achado."""
    c = _cenario(id="area/manco2", func=lambda ctx: e2e.Resultado("problema", "quebrou", esperado="a", obtido=""))
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    assert r.estado == "pulei"
    assert r.nota.startswith("erro do agente no cenário:")
    assert len(r.limitacoes) == 1 and "sem 'obtido'" in r.limitacoes[0]


def test_rodar_cenario_timeout_sem_maw_aberta_e_do_agente(tmp_path):
    """Tempo esgotado sem a MAW nem ter aberto: lentidão do agente, nunca achado contra a MAW."""
    def trava(ctx):
        time.sleep(1)
        return e2e.Resultado("passou", "nunca chega aqui")
    c = _cenario(func=trava, timeout=0.2)
    inicio = time.monotonic()
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    duracao = time.monotonic() - inicio
    assert r.estado == "pulei" and "não terminou em 0.2s" in r.nota and "nem chegou a abrir" in r.nota
    assert len(r.limitacoes) == 1 and "não da MAW" in r.limitacoes[0]
    assert duracao < 3  # não esperou os 10s de cortesia (a thread termina em ~1s)


def test_rodar_cenario_timeout_thread_presa_vira_limitacao(tmp_path):
    """Quando nem os `tempo_apos_forcar` segundos de cortesia bastam, a thread continua presa
    (daemon — não trava o processo) e isso vira uma linha de limitação, não um erro silencioso."""
    def trava_de_verdade(ctx):
        time.sleep(0.3)
    c = _cenario(id="area/presa", func=trava_de_verdade, timeout=0.05)
    r = e2e.rodar_cenario(c, _ctx(tmp_path), tempo_apos_forcar=0.05)
    assert r.estado == "pulei"
    assert any("area/presa" in l and "cortesia" in l for l in r.limitacoes)


def test_rodar_cenario_timeout_forca_fechamento_da_sessao_aberta(monkeypatch, tmp_path):
    fechou = []
    modulo = types.ModuleType("maw_agent.gui")

    class SessaoFalsa:
        def __init__(self, exe, pasta_evidencias, **kw):
            pass

        def matar(self):
            fechou.append(True)

    modulo.SessaoApp = SessaoFalsa
    _instalar_gui_falso(monkeypatch, modulo)

    def trava(ctx):
        ctx.maw()
        time.sleep(3)

    # timeout maior que nos outros testes de timeout: dá folga ao agendador do SO para a thread
    # (que precisa chamar ctx.maw() antes do timeout disparar) rodar mesmo com a máquina ocupada.
    c = _cenario(func=trava, timeout=1)
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    # a sessão falsa não sabe dizer se responde: na dúvida, não é achado contra a MAW
    assert r.estado == "pulei" and fechou == [True]


def _sessao_com_estado(monkeypatch, viva: bool, responde: bool):
    modulo = types.ModuleType("maw_agent.gui")

    class SessaoFalsa:
        def __init__(self, exe, pasta_evidencias, **kw):
            pass

        def matar(self):
            pass

        def viva(self):
            return viva

        def respondendo(self):
            return responde

    modulo.SessaoApp = SessaoFalsa
    _instalar_gui_falso(monkeypatch, modulo)


def _cenario_que_trava():
    def trava(ctx):
        ctx.maw()
        time.sleep(3)
    return _cenario(func=trava, timeout=1)


def test_rodar_cenario_timeout_com_a_maw_sem_responder_e_achado(monkeypatch, tmp_path):
    _sessao_com_estado(monkeypatch, viva=True, responde=False)
    r = e2e.rodar_cenario(_cenario_que_trava(), _ctx(tmp_path))
    assert r.estado == "problema" and r.severidade == "alta"
    assert "parou de responder" in r.obtido


def test_rodar_cenario_timeout_com_a_maw_fechada_e_achado(monkeypatch, tmp_path):
    _sessao_com_estado(monkeypatch, viva=False, responde=False)
    r = e2e.rodar_cenario(_cenario_que_trava(), _ctx(tmp_path))
    assert r.estado == "problema" and "fechou sozinha" in r.obtido


def test_rodar_cenario_timeout_com_a_maw_respondendo_e_do_agente(monkeypatch, tmp_path):
    _sessao_com_estado(monkeypatch, viva=True, responde=True)
    r = e2e.rodar_cenario(_cenario_que_trava(), _ctx(tmp_path))
    assert r.estado == "pulei" and "com a MAW respondendo" in r.nota


def test_rodar_cenario_app_travou_vira_achado_com_a_captura(tmp_path):
    class AppTravou(Exception):
        def __init__(self, msg, captura=None, motivo="nao_responde"):
            super().__init__(msg)
            self.captura, self.motivo = captura, motivo

    png = tmp_path / "travou.png"
    png.write_bytes(b"PNG")

    def cai(ctx):
        raise AppTravou("a janela não responde há 10 s", captura=png)

    ctx = _ctx(tmp_path)
    r = e2e.rodar_cenario(_cenario(func=cai), ctx)
    assert r.estado == "problema" and r.severidade == "alta" and r.limitacoes == []
    assert "parou de responder" in r.obtido
    assert any(ev["arquivo"] == str(png) for ev in ctx.evidencias_registradas)


def test_rodar_cenario_app_travou_por_encerramento_diz_que_fechou(tmp_path):
    class AppTravou(Exception):
        motivo = "encerrou"

    def cai(ctx):
        raise AppTravou("o processo terminou")

    r = e2e.rodar_cenario(_cenario(func=cai), _ctx(tmp_path))
    assert r.estado == "problema" and "fechou sozinha" in r.obtido


def test_rodar_cenario_timeout_guarda_ultima_captura_quando_a_sessao_sabe_tirar(monkeypatch, tmp_path):
    modulo = types.ModuleType("maw_agent.gui")
    capturas = []

    class SessaoFalsa:
        def __init__(self, exe, pasta_evidencias, **kw):
            self.pasta_evidencias = pasta_evidencias

        def matar(self):
            pass

        def captura(self, nome):
            capturas.append(nome)
            caminho = Path(self.pasta_evidencias) / f"{nome}.png"
            caminho.write_bytes(b"PNG")
            return caminho

    modulo.SessaoApp = SessaoFalsa
    _instalar_gui_falso(monkeypatch, modulo)

    def trava(ctx):
        ctx.maw()
        time.sleep(3)

    c = _cenario(func=trava, timeout=1)
    ctx = _ctx(tmp_path)
    e2e.rodar_cenario(c, ctx)
    assert capturas == ["tempo_esgotado"]
    assert any("última captura" in ev["legenda"] for ev in ctx.evidencias_registradas)


def test_rodar_cenario_timeout_sem_captura_nao_quebra(tmp_path):
    """Sessão falsa sem `.captura` (a maioria dos dublês de teste): sem evidência extra, sem erro."""
    def trava(ctx):
        time.sleep(1)
    c = _cenario(func=trava, timeout=0.2)
    r = e2e.rodar_cenario(c, _ctx(tmp_path))
    assert r.estado == "pulei"


# ---------- achado_de_problema ----------

def test_achado_de_problema_e_valido_pelo_esquema(tmp_path):
    c = _cenario(id="area/cenario-x", func=lambda ctx: None, itens=["area/item-x"])
    resultado = e2e.Resultado("problema", "quebrou", esperado="deveria abrir", obtido="ficou preto\nlinha 2")
    achado = e2e.achado_de_problema(c, ALVO, resultado, [{"arquivo": "e.png", "legenda": "tela", "embutir": False}])
    assert achados.validar(achado) == []
    assert achado["tipo"] == "bug" and achado["fonte"] == "e2e" and achado["severidade"] == "media"
    assert achado["item_catalogo"] == "area/item-x"
    assert achado["assinatura"] == "e2e::area/cenario-x"  # estável entre sprints (sem o valor medido)
    assert achado["alvos"] == [{"alvo": "main", "commit": "a" * 40}]


def test_achado_de_problema_sem_itens_usa_interface_geral(tmp_path):
    c = _cenario(id="x", func=lambda ctx: None, itens=[])
    achado = e2e.achado_de_problema(c, ALVO, e2e.Resultado("problema", "quebrou", obtido="algo"), [])
    assert achado["item_catalogo"] == "interface/geral"


def test_achado_de_problema_respeita_severidade_do_resultado(tmp_path):
    c = _cenario(id="x", func=lambda ctx: None)
    r = e2e.Resultado("problema", "quebrou", obtido="algo", severidade="critica")
    achado = e2e.achado_de_problema(c, ALVO, r, [])
    assert achado["severidade"] == "critica"


# ---------- guardas da rodada de segurança (desktop oculto) ----------

def test_tempo_esgotado_esperando_a_trava_vira_pulei_nunca_problema(monkeypatch, tmp_path):
    modulo = types.ModuleType("maw_agent.gui")
    mortas = []

    class SessaoFalsa:
        def __init__(self, exe, pasta_evidencias, **kw):
            self.esperando_trava = True  # outra sessão do agente com a MAW

        def matar(self):
            mortas.append(True)

        def captura(self, nome):
            raise AssertionError("não tira captura de uma MAW que nem abriu")

    modulo.SessaoApp = SessaoFalsa
    _instalar_gui_falso(monkeypatch, modulo)

    def espera(ctx):
        ctx.maw()
        time.sleep(3)

    r = e2e.rodar_cenario(_cenario(func=espera, timeout=1), _ctx(tmp_path))
    assert r.estado == "pulei" and r.nota == "a MAW estava ocupada por outra sessão do agente"
    assert mortas == [True] and any("ocupada por outra sessão do agente" in l for l in r.limitacoes)


def test_app_do_usuario_aberto_durante_o_teste_vira_pulei_com_limitacao(monkeypatch, tmp_path):
    modulo = types.ModuleType("maw_agent.gui")

    class SessaoFalsa:
        def __init__(self, exe, pasta_evidencias, **kw):
            self.maw_estrangeira = {"pid": 4321, "quando": "x"}

    modulo.SessaoApp = SessaoFalsa
    _instalar_gui_falso(monkeypatch, modulo)

    def cenario(ctx):
        ctx.maw()
        raise RuntimeError("a sessão acabou no meio")

    r = e2e.rodar_cenario(_cenario(func=cenario), _ctx(tmp_path))
    assert r.estado == "pulei" and r.nota == "o usuário abriu a MAW durante o teste"
    assert any("pid 4321" in l for l in r.limitacoes)


def test_som_necessario_vira_pulei_sem_limitacao(tmp_path):
    class SomNecessario(Exception):
        pass

    def cenario(ctx):
        raise SomNecessario("a pergunta 'substituir?' toca som")

    r = e2e.rodar_cenario(_cenario(func=cenario), _ctx(tmp_path))
    assert r.estado == "pulei" and "exige som" in r.nota and r.limitacoes == []
