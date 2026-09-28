"""Testes do serviço de IA: detecção de porta, filtro de segredos do ambiente, checagem de porta
ocupada, avaliação de checagens (pura), ServicoApp com um servidor falso (sem Flask nem py310 — só
stdlib, para o processo ser leve e determinístico) e, marcados `lento`, contra o serviço de verdade
em work/py310 (Step 2 do brief). Nenhum nome de script, rota específica, texto de erro ou pacote
do alvo aparece neste arquivo — tudo isso vem de `privado/cenarios/servico.yaml`, lido em tempo de
teste quando existir (os testes reais são pulados sem ele: checkout só público)."""
from __future__ import annotations
import json
import os
import socket
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from maw_agent import catalogo, config, estado, fase_servico, sandbox, servico
from maw_agent.servico import Checagem, ChecagemComando, ChecagemPorta, ServicoApp, ServicoIndisponivel

COMMIT = "a" * 40
NOME_SCRIPT_FALSO = "servidor_falso.py"


# ---------- detectar_porta ----------

def test_detectar_porta_constante_antes_do_app_run(tmp_path):
    p = tmp_path / NOME_SCRIPT_FALSO
    p.write_text("PORTA = 5057\napp.run(port=PORTA)\n", encoding="utf-8")
    assert servico.detectar_porta(p) == 5057


def test_detectar_porta_literal_no_app_run(tmp_path):
    p = tmp_path / NOME_SCRIPT_FALSO
    p.write_text("app.run(port=5000)\n", encoding="utf-8")
    assert servico.detectar_porta(p) == 5000


def test_detectar_porta_sem_app_run_usa_padrao(tmp_path):
    p = tmp_path / NOME_SCRIPT_FALSO
    p.write_text("print('nada de servidor aqui')\n", encoding="utf-8")
    assert servico.detectar_porta(p, padrao=1234) == 1234


def test_detectar_porta_constante_nao_encontrada_usa_padrao(tmp_path):
    p = tmp_path / NOME_SCRIPT_FALSO
    p.write_text("app.run(port=PORTA_QUE_NAO_EXISTE)\n", encoding="utf-8")
    assert servico.detectar_porta(p, padrao=4321) == 4321


# ---------- _ambiente_sem_segredos ----------

def test_ambiente_sem_segredos_remove_variaveis_que_parecem_credencial():
    base = {"PATH": "/x", "FOO_API_KEY": "abc", "MEU_SECRET_TOKEN": "abc", "algo_gemini": "x",
           "SENHA_DO_BANCO": "x", "password123": "x", "OUTRA_COISA": "ok"}
    limpo = servico._ambiente_sem_segredos(base)
    assert limpo == {"PATH": "/x", "OUTRA_COISA": "ok"}


def test_ambiente_sem_segredos_nao_mexe_em_variaveis_comuns():
    base = {"PATH": "/x", "PYTHONPATH": "/y", "LANG": "pt_BR"}
    assert servico._ambiente_sem_segredos(base) == base


# ---------- processo_na_porta ----------

def _porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_processo_na_porta_detecta_quem_escuta():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        porta = s.getsockname()[1]
        ocupado = servico.processo_na_porta(porta)
    assert ocupado is not None
    pid, nome = ocupado
    assert pid == os.getpid() or nome != "?"


def test_processo_na_porta_livre_devolve_none():
    porta = _porta_livre()  # o socket já fechou ao sair do 'with': a porta está livre de novo
    assert servico.processo_na_porta(porta) is None


# ---------- _morte_por_porta_ocupada (log de uma morte antecipada) ----------

def test_morte_por_porta_ocupada_reconhece_o_padrao_do_so(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("Traceback ...\nOSError: [WinError 10048] Only one usage of each socket "
                   "address (protocol/network address/port) is normally permitted\n", encoding="utf-8")
    erro = servico._morte_por_porta_ocupada(9999, log)
    assert erro is not None and "9999" in str(erro)


def test_morte_por_porta_ocupada_sem_padrao_devolve_none(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("Traceback ...\nValueError: outra coisa qualquer\n", encoding="utf-8")
    assert servico._morte_por_porta_ocupada(9999, log) is None


def test_morte_por_porta_ocupada_sem_log_devolve_none():
    assert servico._morte_por_porta_ocupada(9999, None) is None


# ---------- plausibilidade_texto ----------

def test_plausibilidade_texto_identico_ignora_acento_e_caixa():
    assert servico.plausibilidade_texto("Isto É um Teste", "isto e um teste") == 1.0


def test_plausibilidade_texto_parcial():
    fracao = servico.plausibilidade_texto("gato gato gato", "gato cachorro passaro")
    assert fracao == pytest.approx(1 / 3)


def test_plausibilidade_texto_esperado_vazio_devolve_zero():
    assert servico.plausibilidade_texto("qualquer coisa", "") == 0.0


# ---------- avaliar_resposta (pura) ----------

def _chk(**kw) -> Checagem:
    base = dict(id="x", rota="/x", itens_catalogo=["a/x"], status_esperado=(200,))
    base.update(kw)
    return Checagem(**base)


def test_avaliar_resposta_ok():
    chk = _chk(chaves_esperadas=("ok",))
    ok, problemas = servico.avaliar_resposta(chk, 200, {"ok": True}, '{"ok": true}')
    assert ok and problemas == []


def test_avaliar_resposta_status_errado():
    chk = _chk(status_esperado=(400,))
    ok, problemas = servico.avaliar_resposta(chk, 200, {"ok": True}, "")
    assert not ok and "status 200" in problemas[0]


def test_avaliar_resposta_chave_ausente():
    chk = _chk(chaves_esperadas=("ok", "stems"))
    ok, problemas = servico.avaliar_resposta(chk, 200, {"ok": True}, "")
    assert not ok and "stems" in problemas[0]


def test_avaliar_resposta_json_esperado_mas_nao_e_json():
    chk = _chk()
    ok, problemas = servico.avaliar_resposta(chk, 200, None, "<html>erro</html>")
    assert not ok and "não é um JSON válido" in problemas[0]


def test_avaliar_resposta_json_nao_esperado_ignora_corpo():
    chk = _chk(status_esperado=(404,), json_esperado=False)
    ok, problemas = servico.avaliar_resposta(chk, 404, None, "<html>404</html>")
    assert ok and problemas == []


def test_avaliar_resposta_texto_ausente():
    chk = _chk(contem=("erro sintético de teste",))
    ok, problemas = servico.avaliar_resposta(chk, 200, {"ok": True}, '{"ok": true}')
    assert not ok and "erro sintético de teste" in problemas[0]


def test_avaliar_resposta_arquivo_esperado_ausente():
    chk = _chk(arquivos_esperados=("/tmp/saida/arquivo-a",))
    ok, problemas = servico.avaliar_resposta(chk, 200, {"ok": True}, "", {"/tmp/saida/arquivo-a": False})
    assert not ok and "não foi criado" in problemas[0]


def test_avaliar_resposta_arquivo_proibido_existe():
    chk = _chk(arquivos_proibidos=("/tmp/saida/nao-devia",))
    ok, problemas = servico.avaliar_resposta(chk, 200, {"ok": True}, "", {"/tmp/saida/nao-devia": True})
    assert not ok and "não deveria ter sido criado" in problemas[0]


# ---------- avaliar_comando / avaliar_porta ----------

def test_avaliar_comando_codigo_certo_mas_texto_ausente():
    chk = ChecagemComando(id="c", itens_catalogo=["a/x"], argumentos=["--ajuda"],
                          codigo_esperado=0, contem=("tudo certo",))
    ok, problemas = servico.avaliar_comando(chk, 0, "deu ruim mesmo com codigo 0")
    assert not ok and "tudo certo" in problemas[0]


def test_avaliar_comando_codigo_errado():
    chk = ChecagemComando(id="c", itens_catalogo=["a/x"], argumentos=["--ajuda"])
    ok, problemas = servico.avaliar_comando(chk, 1, "erro sintético qualquer")
    assert not ok and "código de saída 1" in problemas[0]


def test_avaliar_porta_bate_com_esperado():
    chk = ChecagemPorta(id="p", itens_catalogo=["a/x"], porta=5000, aberta_esperada=False)
    ok, problemas = servico.avaliar_porta(chk, False)
    assert ok and problemas == []


def test_avaliar_porta_nao_bate():
    chk = ChecagemPorta(id="p", itens_catalogo=["a/x"], porta=5000, aberta_esperada=True)
    ok, problemas = servico.avaliar_porta(chk, False)
    assert not ok and "5000" in problemas[0]


# ---------- resolver_corpo ----------

def test_resolver_corpo_substitui_tokens():
    corpo = {"audio_file": "{audio_valido}", "campo": "valor"}
    out = servico.resolver_corpo(corpo, {"audio_valido": "C:/x/fixture.wav"})
    assert out == {"audio_file": "C:/x/fixture.wav", "campo": "valor"}


def test_resolver_corpo_token_desconhecido_fica_literal():
    corpo = {"audio_file": "{nao_existe}"}
    assert servico.resolver_corpo(corpo, {}) == {"audio_file": "{nao_existe}"}


def test_resolver_corpo_none():
    assert servico.resolver_corpo(None, {}) is None


# ---------- carregar_checagens ----------

YAML_MINIMO = """
script: servidor_falso.py
checagens:
  - id: rota-a
    rota: /rota-a
    metodo: GET
    status_esperado: 200
    chaves_esperadas: [ok]
    itens_catalogo: [a/x]
comandos:
  - id: comando-a
    itens_catalogo: [a/y]
    argumentos: ["--ajuda"]
portas:
  - id: porta-a
    itens_catalogo: [a/z]
    porta: 5000
    aberta_esperada: true
nao_testavel:
  - itens_catalogo: [a/w]
    motivo: motivo sintético de teste
"""


def test_carregar_checagens(tmp_path):
    p = tmp_path / "servico.yaml"
    p.write_text(YAML_MINIMO, encoding="utf-8")
    d = servico.carregar_checagens(p)
    assert d["script"] == "servidor_falso.py"
    assert [c.id for c in d["checagens"]] == ["rota-a"]
    assert d["checagens"][0].chaves_esperadas == ("ok",)
    assert [c.id for c in d["comandos"]] == ["comando-a"]
    assert [c.id for c in d["portas"]] == ["porta-a"]
    assert d["nao_testavel"][0].itens_catalogo == ["a/w"]
    assert d["nao_testavel"][0].motivo == "motivo sintético de teste"


def test_carregar_checagens_arquivo_real_do_privado():
    """privado/cenarios/servico.yaml existe, carrega sem erro e declara um script (não valida
    conteúdo específico do alvo aqui)."""
    caminho = config.PRIVADO / "cenarios" / "servico.yaml"
    if not caminho.exists():
        pytest.skip("privado/cenarios/servico.yaml ausente nesta árvore")
    d = servico.carregar_checagens(caminho)
    assert d["checagens"]
    assert d["script"]


# ---------- cobertura ----------

def test_cobertura_sem_faltantes():
    dados = {"checagens": [_chk(itens_catalogo=["a/um"])], "comandos": [], "portas": [],
             "nao_testavel": [servico.NaoTestavel(["a/dois"], "motivo")]}
    cobertos, faltando = fase_servico.cobertura({"a/um", "a/dois"}, dados)
    assert faltando == []
    assert cobertos == {"a/um", "a/dois"}


def test_cobertura_acusa_item_sem_checagem():
    dados = {"checagens": [_chk(itens_catalogo=["a/um"])], "comandos": [], "portas": [], "nao_testavel": []}
    _cobertos, faltando = fase_servico.cobertura({"a/um", "a/dois"}, dados)
    assert faltando == ["a/dois"]


def test_cobertura_real_sem_gaps():
    """Todo item do catálogo real com 'servico' em verificacao tem checagem ou nao_testavel em
    privado/cenarios/servico.yaml — pulado sem privado (checkout só público)."""
    if not (config.CATALOGO.exists() and (config.PRIVADO / "cenarios" / "servico.yaml").exists()):
        pytest.skip("privado ausente nesta árvore (checkout só público)")
    itens = catalogo.carregar(config.CATALOGO)
    itens_servico = fase_servico._itens_servico(itens)
    dados = servico.carregar_checagens(config.PRIVADO / "cenarios" / "servico.yaml")
    _cobertos, faltando = fase_servico.cobertura(itens_servico, dados)
    assert faltando == []


# ---------- servidor falso (stdlib, sem Flask nem py310) ----------

_FAKE_SERVER = textwrap.dedent("""
    import http.server, json, os, sys

    PORTA = {porta}
    # comentário só para servico.detectar_porta() achar a porta como no script real do serviço:
    # app.run(port=PORTA)

    # captura o ambiente que o processo recebeu, para os testes de filtro de segredo conferirem
    with open("ambiente_capturado.json", "w", encoding="utf-8") as _f:
        json.dump(dict(os.environ), _f)

    class H(http.server.BaseHTTPRequestHandler):
        def _responder(self, status, corpo):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(corpo).encode("utf-8"))

        def do_GET(self):
            if self.path == "/health":
                if os.environ.get("FAKE_SAUDE_RUIM") == "1":
                    self._responder(500, {{"ok": False}})
                else:
                    self._responder(200, {{"ok": True, "rotas": ["/rota-a"], "versao": "fake"}})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            tam = int(self.headers.get("Content-Length", 0))
            bruto = self.rfile.read(tam) if tam else b""
            try:
                corpo = json.loads(bruto) if bruto else {{}}
            except ValueError:
                corpo = None
            if self.path == "/rota-a":
                self._responder(200, {{"ok": True, "recebido": corpo}})
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    if os.environ.get("FAKE_FALHAR_AO_SUBIR") == "1":
        sys.exit(3)
    http.server.HTTPServer(("127.0.0.1", PORTA), H).serve_forever()
""")


@pytest.fixture
def fake_alvo(tmp_path):
    """Uma pasta de alvo com um script de serviço falso (stdlib puro) numa porta livre."""
    porta = _porta_livre()
    pasta = tmp_path / "alvo"
    pasta.mkdir()
    (pasta / NOME_SCRIPT_FALSO).write_text(_FAKE_SERVER.format(porta=porta), encoding="utf-8")
    return pasta, porta


def psutil_ainda_vivo(pid: int) -> bool:
    import psutil
    try:
        return psutil.Process(pid).is_running()
    except psutil.NoSuchProcess:
        return False


def test_servico_app_sobe_espera_saude_e_derruba(fake_alvo):
    pasta, porta = fake_alvo
    with ServicoApp(pasta, script=NOME_SCRIPT_FALSO, python=Path(sys.executable), ffmpeg=None,
                    timeout_saude=10) as app:
        assert app.porta == porta
        assert app.saude == {"ok": True, "rotas": ["/rota-a"], "versao": "fake"}
        assert app.processo.poll() is None  # está vivo
        pid = app.processo.pid
    assert app.processo.poll() is not None  # __exit__ derrubou
    assert not psutil_ainda_vivo(pid)


def test_servico_app_processo_morre_sozinho(fake_alvo):
    pasta, _porta = fake_alvo
    with pytest.raises(ServicoIndisponivel, match="terminou sozinho"):
        with ServicoApp(pasta, script=NOME_SCRIPT_FALSO, python=Path(sys.executable), ffmpeg=None,
                        timeout_saude=10, ambiente_extra={"FAKE_FALHAR_AO_SUBIR": "1"}):
            pass


def test_servico_app_saude_nunca_fica_boa_estoura_timeout_e_mata(fake_alvo):
    pasta, _porta = fake_alvo
    with pytest.raises(ServicoIndisponivel, match="não respondeu"):
        with ServicoApp(pasta, script=NOME_SCRIPT_FALSO, python=Path(sys.executable), ffmpeg=None,
                        timeout_saude=2, ambiente_extra={"FAKE_SAUDE_RUIM": "1"}):
            pass  # não deveria chegar aqui


def test_servico_app_python_ausente(tmp_path):
    (tmp_path / NOME_SCRIPT_FALSO).write_text("pass", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        with ServicoApp(tmp_path, script=NOME_SCRIPT_FALSO, python=tmp_path / "nao-existe.exe", ffmpeg=None):
            pass


def test_servico_app_script_ausente(tmp_path):
    with pytest.raises(FileNotFoundError):
        with ServicoApp(tmp_path, script=NOME_SCRIPT_FALSO, python=Path(sys.executable), ffmpeg=None):
            pass


def test_executar_checagem_contra_servidor_falso(fake_alvo):
    pasta, _porta = fake_alvo
    chk = _chk(id="rota-a", rota="/rota-a", metodo="POST", corpo={"a": 1}, chaves_esperadas=("ok", "recebido"))
    with ServicoApp(pasta, script=NOME_SCRIPT_FALSO, python=Path(sys.executable), ffmpeg=None,
                    timeout_saude=10) as app:
        ok, problemas, detalhe = servico.executar_checagem(app.base_url, chk, {})
    assert ok, problemas
    assert detalhe["corpo"]["recebido"] == {"a": 1}


# ---------- C2: porta já ocupada ----------

def test_servico_app_porta_ja_ocupada_nao_inicia_nem_mexe_em_nada(fake_alvo):
    pasta, porta = fake_alvo
    ocupante = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        ocupante.bind(("127.0.0.1", porta))
        ocupante.listen(1)
        app = ServicoApp(pasta, script=NOME_SCRIPT_FALSO, python=Path(sys.executable), ffmpeg=None,
                         timeout_saude=5)
        with pytest.raises(servico.PortaOcupada, match=str(porta)):
            with app:
                pass
        assert app.processo is None  # nunca chegou a iniciar nada
        # o socket que já ocupava a porta continua vivo e é o mesmo — o agente não mexeu nele
        assert ocupante.getsockname()[1] == porta
    finally:
        ocupante.close()


# ---------- C3: o ambiente do processo filho nunca carrega o que parece uma credencial ----------

def test_servico_app_filtra_variaveis_que_parecem_credencial(fake_alvo, monkeypatch):
    pasta, _porta = fake_alvo
    monkeypatch.setenv("FOO_API_KEY", "nao-deveria-chegar-no-filho")
    monkeypatch.setenv("OUTRA_VARIAVEL_QUALQUER", "essa-pode-passar")
    with ServicoApp(pasta, script=NOME_SCRIPT_FALSO, python=Path(sys.executable), ffmpeg=None, timeout_saude=10):
        pass
    capturado = json.loads((pasta / "ambiente_capturado.json").read_text(encoding="utf-8"))
    assert "FOO_API_KEY" not in capturado
    assert capturado.get("OUTRA_VARIAVEL_QUALQUER") == "essa-pode-passar"


# ---------- fase_servico: orquestração completa contra o servidor falso ----------

class Args:
    def __init__(self, alvo=None):
        self.alvo = alvo


@pytest.fixture
def sprint_servico(tmp_path, monkeypatch, fake_alvo):
    pasta_alvo, porta = fake_alvo
    raiz = tmp_path / "relatorios"
    monkeypatch.setattr(config, "RELATORIOS", raiz)
    monkeypatch.setattr(config, "ALVOS_DIR", tmp_path / "alvos")
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    monkeypatch.setattr(config, "FERRAMENTAS", tmp_path / "ferramentas")
    monkeypatch.setattr(config, "PRIVADO", tmp_path / "privado")
    monkeypatch.setattr(config, "CATALOGO", tmp_path / "privado" / "catalogo" / "funcionalidades.yaml")
    monkeypatch.setattr(servico, "PYTHON_SERVICO_PADRAO", Path(sys.executable))
    monkeypatch.setattr(servico, "FFMPEG_BIN_PADRAO", tmp_path / "sem-ffmpeg")

    destino = config.ALVOS_DIR / "main"
    destino.mkdir(parents=True)
    (destino / NOME_SCRIPT_FALSO).write_text((pasta_alvo / NOME_SCRIPT_FALSO).read_text(encoding="utf-8"),
                                             encoding="utf-8")

    itens = [{"id": "servico/health", "area": "servico", "titulo": "t", "descricao": "d", "origem": ["x"],
             "verificacao": ["servico"], "cenarios": [], "requisitos": [], "marco": "M4"},
            {"id": "servico/eco", "area": "servico", "titulo": "t", "descricao": "d", "origem": ["x"],
             "verificacao": ["servico"], "cenarios": [], "requisitos": [], "marco": "M4"},
            {"id": "servico/duplo", "area": "servico", "titulo": "t", "descricao": "d", "origem": ["x"],
             "verificacao": ["servico"], "cenarios": [], "requisitos": [], "marco": "M4"}]
    sandbox.criar_pasta(config.CATALOGO.parent)
    sandbox.escrever_texto(config.CATALOGO, yaml.safe_dump(itens, allow_unicode=True))

    cenarios = tmp_path / "privado" / "cenarios" / "servico.yaml"
    sandbox.criar_pasta(cenarios.parent)
    sandbox.escrever_texto(cenarios, yaml.safe_dump({
        "script": NOME_SCRIPT_FALSO,
        "checagens": [
            # 'eco-quebrado' (falha) roda ANTES de 'health' (passa) de propósito: as duas citam
            # servico/duplo, e um 'last write wins' ingênuo mostraria 'passou' (a última a rodar);
            # a agregação correta tem que mostrar 'falhou' de qualquer jeito.
            {"id": "eco-quebrado", "rota": "/rota-a", "metodo": "POST", "corpo": {"a": 1},
             "status_esperado": 200, "chaves_esperadas": ["ok", "chave-que-nao-existe"],
             "itens_catalogo": ["servico/eco", "servico/duplo"], "esperado": "deveria ter 'chave-que-nao-existe'"},
            {"id": "health", "rota": "/health", "metodo": "GET", "status_esperado": 200,
             "chaves_esperadas": ["ok"], "itens_catalogo": ["servico/health", "servico/duplo"],
             "esperado": "200 ok", "bancada": "b-servico-health"},
        ],
        "comandos": [], "portas": [], "nao_testavel": [],
    }, allow_unicode=True))
    monkeypatch.setattr(fase_servico, "CAMINHO_CENARIOS", cenarios)

    e = estado.nova_sprint(raiz)
    monkeypatch.setattr(fase_servico, "_sprint_atual", lambda: estado.carregar(e.pasta))
    sandbox.escrever_json(e.pasta / "alvos.json", {"alvos": [
        {"nome": "main", "branch": "main", "ref": "origin/main", "commit": COMMIT, "origem": "github",
         "assinatura": "s", "compartilha_com": None},
        {"nome": "docs-z", "branch": "docs/z", "ref": "origin/docs/z", "commit": "d" * 40, "origem": "github",
         "assinatura": "s", "compartilha_com": "main"}], "avisos": []})
    return {"estado": e, "porta": porta}


def test_fase_servico_registra_resultados_e_achado(sprint_servico):
    e = sprint_servico["estado"]
    codigo = fase_servico.cmd_servico(Args())
    # o código de saída reflete o mecanismo (o serviço subiu, /health respondeu), não cada checagem —
    # como em `sprint suite`: falha de alvo é dado no catálogo, não no código de saída da fase.
    assert codigo == 0
    r = catalogo.carregar_resultados(e.pasta)
    assert r[("servico/health", "main")]["resultado"] == "passou"
    assert r[("servico/eco", "main")]["resultado"] == "falhou"
    assert "chave-que-nao-existe" in r[("servico/eco", "main")]["motivo"]
    # o alvo que compartilha a árvore herda o resultado do 'servico'
    assert r[("servico/eco", "docs-z")]["resultado"] == "falhou"
    achados_brutos = list((e.pasta / "achados-brutos").glob("servico-main-eco-quebrado.json"))
    assert len(achados_brutos) == 1
    ach = json.loads(achados_brutos[0].read_text(encoding="utf-8"))
    assert ach["fonte"] == "servico"
    assert ach["item_catalogo"] == "servico/eco"
    from maw_agent import achados as achados_mod
    assert achados_mod.validar(ach) == []


def test_fase_servico_item_citado_por_duas_checagens_falha_vence(sprint_servico):
    """servico/duplo é citado por 'eco-quebrado' (falha) e por 'health' (passa, e roda depois no
    YAML). Um 'last write wins' ingênuo mostraria 'passou'; o resultado tem que ser 'falhou' e
    guardar o motivo da checagem que falhou, nunca perdê-lo em silêncio."""
    e = sprint_servico["estado"]
    fase_servico.cmd_servico(Args())
    r = catalogo.carregar_resultados(e.pasta)
    assert r[("servico/duplo", "main")]["resultado"] == "falhou"
    assert "chave-que-nao-existe" in r[("servico/duplo", "main")]["motivo"]


def test_fase_servico_retomada_pula_alvo_ja_feito(sprint_servico):
    e = sprint_servico["estado"]
    fase_servico.cmd_servico(Args())
    passo_antes = estado.carregar(e.pasta).passos["servico:main"]
    fase_servico.cmd_servico(Args())  # retomada: não deveria subir o serviço de novo para 'main'
    passo_depois = estado.carregar(e.pasta).passos["servico:main"]
    assert passo_depois["inicio"] == passo_antes["inicio"]


def test_fase_servico_registra_bancada_quando_a_checagem_tem_codigo(sprint_servico):
    from maw_agent import bancada
    e = sprint_servico["estado"]
    fase_servico.cmd_servico(Args())
    dados = bancada.carregar(e.pasta)
    assert dados["b-servico-health"] == {"estado": "passou", "nota": "ok", "alvo": "main"}


def test_fase_servico_python_ausente_vira_nao_testavel_nao_achado(sprint_servico, monkeypatch):
    e = sprint_servico["estado"]
    monkeypatch.setattr(servico, "PYTHON_SERVICO_PADRAO", Path("C:/nao-existe/python.exe"))
    fase_servico.cmd_servico(Args())
    r = catalogo.carregar_resultados(e.pasta)
    assert r[("servico/health", "main")]["resultado"] == "nao_testavel"
    assert not (e.pasta / "achados-brutos").exists()
    limitacoes = json.loads((e.pasta / "limitacoes-servico.json").read_text(encoding="utf-8"))
    assert any("python do serviço ausente" in l for l in limitacoes)


def test_fase_servico_script_ausente_vira_nao_testavel_e_limitacao(sprint_servico):
    e = sprint_servico["estado"]
    (config.ALVOS_DIR / "main" / NOME_SCRIPT_FALSO).unlink()
    fase_servico.cmd_servico(Args())
    r = catalogo.carregar_resultados(e.pasta)
    assert r[("servico/health", "main")]["resultado"] == "nao_testavel"
    limitacoes = json.loads((e.pasta / "limitacoes-servico.json").read_text(encoding="utf-8"))
    assert any("script do serviço não existe" in l for l in limitacoes)


def test_fase_servico_porta_ocupada_vira_nao_testavel_nunca_achado(sprint_servico):
    """A porta do alvo já está ocupada por outro processo antes da fase subir o serviço: nunca é
    tratado como defeito da MAW (a fase não inicia nem mexe em nada), e a situação fica registrada
    tanto no catálogo quanto nas limitações."""
    e = sprint_servico["estado"]
    porta = sprint_servico["porta"]
    ocupante = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        ocupante.bind(("127.0.0.1", porta))
        ocupante.listen(1)
        fase_servico.cmd_servico(Args())
    finally:
        ocupante.close()
    r = catalogo.carregar_resultados(e.pasta)
    assert r[("servico/health", "main")]["resultado"] == "nao_testavel"
    assert "ocupada por outro processo" in r[("servico/health", "main")]["motivo"]
    assert not (e.pasta / "achados-brutos").exists()
    limitacoes = json.loads((e.pasta / "limitacoes-servico.json").read_text(encoding="utf-8"))
    assert any("ocupada por outro processo" in l for l in limitacoes)


def test_fase_servico_achado_invalido_nao_e_gravado_e_vira_limitacao(sprint_servico, monkeypatch):
    """Um achado que não passa no próprio esquema é erro do agente (regra 5 da constituição): vira
    limitação, nunca uma ficha ruim no disco."""
    e = sprint_servico["estado"]
    monkeypatch.setattr(fase_servico.achados, "validar", lambda achado: ["erro sintético de teste"])
    fase_servico.cmd_servico(Args())
    assert not (e.pasta / "achados-brutos").exists()
    limitacoes = json.loads((e.pasta / "limitacoes-servico.json").read_text(encoding="utf-8"))
    assert any("não passar no esquema" in l for l in limitacoes)


# ---------- Step 2: contra o serviço de verdade (work/py310 + work/alvos/main) ----------
# Marcados `lento`: sobem um processo Python 3.10 de verdade. Nenhuma rota, texto de erro ou nome
# de pacote do alvo aparece neste arquivo — tudo vem de privado/cenarios/servico.yaml, lido em
# tempo de teste; sem esse arquivo (checkout só público) os testes reais são pulados. Estes testes
# confirmam que o PIPELINE do agente (subir o serviço, mandar a checagem, avaliar) funciona de
# ponta a ponta contra o processo de verdade — não que o alvo passa em tudo: isso é o trabalho de
# `MA servico`, que vira achado quando não bate (privado/relatorios/, nunca aqui). Nenhum destes lê,
# imprime ou grava a chave de API do serviço — o ambiente do processo filho nunca carrega uma
# variável que pareça uma credencial (servico._ambiente_sem_segredos).

def _dados_privados_servico() -> dict | None:
    caminho = config.PRIVADO / "cenarios" / "servico.yaml"
    if not caminho.exists():
        return None
    return servico.carregar_checagens(caminho)


def _rota_da_checagem(dados: dict | None, chk_id: str) -> str | None:
    """Rota de uma checagem do YAML privado pelo id que ESTE arquivo escolheu (não o nome de
    nenhuma rota do alvo) — evita hardcodar a rota de verdade num teste público."""
    return next((c.rota for c in (dados or {}).get("checagens", []) if c.id == chk_id), None)


_DADOS_PRIVADOS = _dados_privados_servico()
_NOME_SCRIPT_REAL = (_DADOS_PRIVADOS or {}).get("script")
ALVO_MAIN = config.ALVOS_DIR / "main"
PY310 = servico.PYTHON_SERVICO_PADRAO

pytestmark_privado_ausente = pytest.mark.skipif(
    not _DADOS_PRIVADOS or not _NOME_SCRIPT_REAL,
    reason="privado/cenarios/servico.yaml ausente ou sem 'script' (checkout só público)")
pytestmark_py310_ausente = pytest.mark.skipif(not PY310.exists(), reason="work/py310 ausente nesta máquina")
pytestmark_alvo_ausente = pytest.mark.skipif(
    not _NOME_SCRIPT_REAL or not (ALVO_MAIN / _NOME_SCRIPT_REAL).exists(),
    reason="script do serviço ausente em work/alvos/main nesta máquina")


@pytest.mark.lento
@pytestmark_privado_ausente
@pytestmark_py310_ausente
@pytestmark_alvo_ausente
def test_real_pipeline_roda_de_ponta_a_ponta_sem_excecao(tmp_path):
    """Roda contra o serviço de verdade todas as checagens HTTP/comando/porta de
    privado/cenarios/servico.yaml que não estão marcadas 'pesado' (as pesadas baixam modelo e
    ficam para a sprint noturna). Confirma que o pipeline do agente devolve valores bem formados
    sem lançar exceção — não que o alvo passa em tudo (ver comentário do bloco)."""
    leves = [c for c in _DADOS_PRIVADOS["checagens"] if not c.pesado]
    audio_valido = servico.gerar_wav_curto(tmp_path / "fixture.wav")
    contexto = {"audio_valido": str(audio_valido), "audio_inexistente": str(tmp_path / "nao-existe.wav"),
               "saida": str(tmp_path / "saida")}
    algum_resultado = False
    with ServicoApp(ALVO_MAIN, script=_NOME_SCRIPT_REAL) as app:
        assert app.saude.get("ok") is True
        for chk in leves:
            ok, problemas, detalhe = servico.executar_checagem(app.base_url, chk, contexto)
            assert isinstance(ok, bool) and isinstance(problemas, list) and isinstance(detalhe, dict)
            algum_resultado = True
        for chk in _DADOS_PRIVADOS["comandos"]:
            codigo, saida = servico.rodar_comando(PY310, chk.argumentos, chk.timeout, cwd=ALVO_MAIN)
            ok, problemas = servico.avaliar_comando(chk, codigo, saida)
            assert isinstance(ok, bool) and isinstance(problemas, list)
            algum_resultado = True
        for chk in _DADOS_PRIVADOS["portas"]:
            aberta = servico.porta_aberta(chk.porta)
            ok, problemas = servico.avaliar_porta(chk, aberta)
            assert isinstance(ok, bool) and isinstance(problemas, list)
            algum_resultado = True
    assert algum_resultado  # sanidade do próprio teste: o YAML privado não está vazio


@pytest.mark.lento
@pytestmark_privado_ausente
@pytestmark_py310_ausente
@pytestmark_alvo_ausente
def test_real_transcricao_de_fala_de_verdade_e_plausivel(tmp_path):
    """Diferente das checagens de transcrição do YAML (tom sintético, só o formato importa): manda
    uma fala de verdade e confirma que a transcrição é plausível — pelo menos 40% das palavras do
    texto esperado (work/fixtures/manifesto.json) aparecem no texto devolvido. A voz da fixture é
    uma TTS em inglês (en-US) lendo um texto em português: sotaque estrangeiro reduz a precisão,
    por isso o limiar é 40%, bem abaixo de uma transcrição perfeita — não 90% nem 100%."""
    fixture = config.WORK / "fixtures" / "fala_ptbr.wav"
    manifesto_caminho = config.WORK / "fixtures" / "manifesto.json"
    if not fixture.exists() or not manifesto_caminho.exists():
        pytest.skip("fixture de fala ou manifesto ausente (work/fixtures/) nesta máquina")
    manifesto = json.loads(manifesto_caminho.read_text(encoding="utf-8"))
    texto_esperado = (manifesto.get(fixture.name) or {}).get("texto_esperado")
    if not texto_esperado:
        pytest.skip("manifesto sem 'texto_esperado' para a fixture de fala")
    rota = _rota_da_checagem(_DADOS_PRIVADOS, "transcribe")
    if not rota:
        pytest.skip("cenário 'transcribe' ausente em privado/cenarios/servico.yaml")
    chk = _chk(id="transcricao-fala-real", rota=rota, metodo="POST",
              corpo={"audio_file": str(fixture)}, status_esperado=(200,),
              chaves_esperadas=("ok", "text", "language", "segments"), timeout=1800)
    with ServicoApp(ALVO_MAIN, script=_NOME_SCRIPT_REAL) as app:
        ok, problemas, detalhe = servico.executar_checagem(app.base_url, chk, {})
    assert ok, problemas
    obtido = detalhe["corpo"].get("text", "") if isinstance(detalhe["corpo"], dict) else ""
    fracao = servico.plausibilidade_texto(obtido, texto_esperado)
    assert fracao >= 0.4, (f"transcrição pouco plausível ({fracao:.0%} das palavras esperadas; voz "
                          f"TTS en-US lendo um texto em pt-BR, limiar 40%): texto obtido {obtido!r}")
