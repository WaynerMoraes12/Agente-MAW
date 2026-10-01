"""Testes do serviço de IA de um alvo que exige um token de quem chama: opções novas das checagens
(sem token, token errado, cabeçalhos próprios, corpo declarado e nunca enviado, Transfer-Encoding,
nonce com a prova HMAC, `nao_contem`, corpo gerado, repetições, saída num pipe que ninguém lê), a
leitura do token do arquivo do alvo (sem nunca deixá-lo em evidência, achado ou resultado) e a fase
`servico` cercando esse alvo com a trava, o backup e a restauração conferida do %APPDATA%\\MAW.

Tudo contra um servidor falso (stdlib) numa porta livre: nenhum nome de cabeçalho, arquivo, rota ou
texto de erro do alvo de verdade aparece aqui."""
from __future__ import annotations
import json
import os
import socket
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from maw_agent import catalogo, config, estado, fase_servico, fases, sandbox, servico, suite, trava_appdata
from maw_agent.servico import Checagem, ConfigToken, ServicoApp

COMMIT = "a" * 40
SCRIPT = "servidor_com_token.py"
CABECALHO = "X-Teste-Token"
NONCE = "X-Teste-Nonce"
ARQUIVO = "MAW/token-do-servico"  # relativo à pasta de dados do usuário (falsa, em tmp)
CFG = ConfigToken(marca=CABECALHO, cabecalho=CABECALHO, arquivo=ARQUIVO, cabecalho_nonce=NONCE,
                  campo_prova="prova", rota_gatilho="/")
LIMITE = 1024  # o "1 MB" do servidor falso

_SERVIDOR = textwrap.dedent('''
    import hashlib, hmac, http.server, json, os, re, secrets, stat, sys

    PORTA = __PORTA__
    # para servico.detectar_porta(): app.run(port=PORTA)
    CABECALHO = "X-Teste-Token"
    NONCE = "X-Teste-Nonce"
    ARQUIVO = os.path.join(os.environ["FAKE_PASTA_DADOS"], "MAW", "token-do-servico")
    HOSTS = ("127.0.0.1:%d" % PORTA, "localhost:%d" % PORTA)

    def token():
        try:
            with open(ARQUIVO, encoding="ascii") as f:
                lido = f.read().strip()
            if lido:
                return lido
        except OSError:
            pass
        if os.environ.get("FAKE_NUNCA_CRIA") == "1":
            return ""
        os.makedirs(os.path.dirname(ARQUIVO), exist_ok=True)
        novo = secrets.token_hex(32)
        with open(ARQUIVO, "x", encoding="ascii") as f:
            f.write(novo)
        return novo

    with open("captura.json", "w", encoding="utf-8") as _f:
        json.dump({"env": dict(os.environ), "stdout_pipe": stat.S_ISFIFO(os.fstat(1).st_mode)}, _f)
    if os.environ.get("FAKE_DESLIGA_PIPE") == "1" and stat.S_ISFIFO(os.fstat(1).st_mode):
        _nulo = open(os.devnull, "w")
        os.dup2(_nulo.fileno(), 1)
        os.dup2(_nulo.fileno(), 2)
    if os.environ.get("FAKE_CRIA_AO_SUBIR", "1") == "1":
        token()

    class H(http.server.BaseHTTPRequestHandler):
        def _resp(self, status, corpo):
            dados = json.dumps(corpo).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)

        def _conferir(self):
            if self.headers.get("Host", "").lower() not in HOSTS:
                self._resp(403, {"ok": False, "error": "Host recusado"})
                return False
            if self.path.split("?")[0] == "/health":
                return True
            t = token()
            if not t or not hmac.compare_digest(self.headers.get(CABECALHO, "").encode(), t.encode()):
                self._resp(401, {"ok": False, "error": "sem o token"})
                return False
            if "Transfer-Encoding" in self.headers:
                self._resp(411, {"ok": False, "error": "HTTP 411"})
                return False
            if int(self.headers.get("Content-Length") or 0) > 1024:
                self._resp(413, {"ok": False, "error": "HTTP 413"})
                return False
            return True

        def do_GET(self):
            if not self._conferir():
                return
            caminho = self.path.split("?")[0]
            if caminho == "/health":
                corpo = {"ok": True, "rotas": [], "versao": "fake"}
                n = self.headers.get(NONCE, "")
                if re.fullmatch("[0-9a-f]{16,128}", n):
                    chave = "0" * 64 if os.environ.get("FAKE_PROVA_ERRADA") == "1" else token()
                    corpo["prova"] = hmac.new(chave.encode(), n.encode(), hashlib.sha256).hexdigest()
                self._resp(200, corpo)
            elif caminho == "/eco-token":
                self._resp(500, {"ok": False, "error": "recebi " + self.headers.get(CABECALHO, "")})
            elif caminho == "/barulhento":
                sys.stderr.write("x" * 65536)
                sys.stderr.flush()
                self._resp(200, {"ok": True})
            else:
                self._resp(404, {"ok": False, "error": "rota inexistente"})

        def do_POST(self):
            if not self._conferir():
                return
            tam = int(self.headers.get("Content-Length") or 0)
            bruto = self.rfile.read(tam) if tam else b""
            try:
                corpo = json.loads(bruto) if bruto else {}
            except ValueError:
                corpo = None
            self._resp(200, {"ok": True, "recebido": corpo})

        def log_message(self, *a):
            pass

    http.server.HTTPServer(("127.0.0.1", PORTA), H).serve_forever()
''')


def _porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def dados_falsos(tmp_path, monkeypatch):
    """Pasta de dados do usuário falsa (em tmp): o token do servidor falso fica nela, e é nela que o
    agente o procura."""
    pasta = tmp_path / "Roaming"
    pasta.mkdir()
    monkeypatch.setattr(servico, "pasta_de_dados_do_usuario", lambda: pasta)
    monkeypatch.setenv("FAKE_PASTA_DADOS", str(pasta))
    return pasta


@pytest.fixture
def alvo_token(tmp_path, dados_falsos):
    porta = _porta_livre()
    pasta = tmp_path / "alvo"
    pasta.mkdir()
    (pasta / SCRIPT).write_text(_SERVIDOR.replace("__PORTA__", str(porta)), encoding="utf-8")
    return pasta, porta


def _app(pasta, **kw) -> ServicoApp:
    return ServicoApp(pasta, script=SCRIPT, python=Path(sys.executable), ffmpeg=None, timeout_saude=10,
                      token=CFG, **kw)


def _chk(**kw) -> Checagem:
    base = dict(id="x", rota="/rota", itens_catalogo=["a/b"])
    base.update(kw)
    return Checagem(**base)


def _token_no_disco(pasta_dados: Path) -> str:
    return (pasta_dados / ARQUIVO).read_text(encoding="ascii").strip()


# ---------- detecção e leitura do token ----------

def test_usa_token_pela_marca_no_script(tmp_path):
    com = tmp_path / "com.py"
    com.write_text(f'H = "{CABECALHO}"\n', encoding="utf-8")
    sem = tmp_path / "sem.py"
    sem.write_text("print('oi')\n", encoding="utf-8")
    assert servico.usa_token(com, CFG) is True
    assert servico.usa_token(sem, CFG) is False
    assert servico.usa_token(com, None) is False
    assert servico.usa_token(tmp_path / "nao-existe.py", CFG) is False


def test_caminho_do_token_fica_na_pasta_de_dados_do_usuario(dados_falsos):
    assert servico.caminho_do_token(CFG) == dados_falsos / ARQUIVO


def test_pasta_de_dados_do_usuario_e_um_caminho_absoluto():
    p = servico.pasta_de_dados_do_usuario()
    assert p.is_absolute()


def test_token_do_alvo_nunca_aparece_no_repr():
    t = servico.TokenDoAlvo(CFG, "f" * 64)
    assert "f" * 64 not in repr(t)


def test_carregar_token_e_variaveis_do_yaml(tmp_path):
    p = tmp_path / "servico.yaml"
    p.write_text(yaml.safe_dump({
        "script": SCRIPT, "checagens": [],
        "token": {"marca_no_script": CABECALHO, "cabecalho": CABECALHO, "arquivo": ARQUIVO,
                  "cabecalho_nonce": NONCE, "campo_prova": "prova", "rota_gatilho": "/"},
        "variaveis": [{"nome": "VAR_COMUM", "valor": "{pasta_alvo}/x"},
                      {"nome": "VAR_NOVA", "valor": "1", "requer_token": True}]}), encoding="utf-8")
    d = servico.carregar_checagens(p)
    assert d["token"] == CFG
    assert servico.variaveis_do_processo(d["variaveis"], False, {"pasta_alvo": "C:/a"}) == {"VAR_COMUM": "C:/a/x"}
    assert servico.variaveis_do_processo(d["variaveis"], True, {"pasta_alvo": "C:/a"}) == {
        "VAR_COMUM": "C:/a/x", "VAR_NOVA": "1"}


def test_yaml_sem_token_continua_carregando(tmp_path):
    p = tmp_path / "servico.yaml"
    p.write_text(yaml.safe_dump({"script": SCRIPT, "checagens": [
        {"id": "a", "rota": "/a", "itens_catalogo": ["a/b"]}]}), encoding="utf-8")
    d = servico.carregar_checagens(p)
    assert d["token"] is None and d["variaveis"] == []
    c = d["checagens"][0]
    assert c.token == "certo" and c.cabecalhos == {} and c.nao_contem == () and c.repeticoes == 1
    assert not c.requer_token and not c.saida_em_pipe


def test_variavel_com_nome_de_credencial_e_valor_de_chave_de_verdade_e_recusada(tmp_path):
    p = tmp_path / "servico.yaml"
    p.write_text(yaml.safe_dump({"script": SCRIPT, "checagens": [], "variaveis": [
        {"nome": "ALGUMA_API_KEY", "valor": "AIza" + "x" * 35}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="credencial"):
        servico.carregar_checagens(p)


def test_opcao_de_token_desconhecida_e_recusada(tmp_path):
    p = tmp_path / "servico.yaml"
    p.write_text(yaml.safe_dump({"script": SCRIPT, "checagens": [
        {"id": "a", "rota": "/a", "itens_catalogo": ["a/b"], "token": "talvez"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="token"):
        servico.carregar_checagens(p)


def test_redigir_troca_o_segredo_registrado():
    servico.registrar_segredo("segredo-de-teste-123456")
    assert servico.redigir("antes segredo-de-teste-123456 depois") == "antes [REDACTED] depois"


# ---------- corpo gerado e nao_contem (puras) ----------

def test_resolver_corpo_gera_texto_repetido():
    corpo = servico.resolver_corpo({"prompt": {"repetir": "ab", "vezes": 3}, "x": "{a}"}, {"a": "1"})
    assert corpo == {"prompt": "ababab", "x": "1"}


def test_avaliar_resposta_nao_contem():
    chk = _chk(nao_contem=("chave-secreta",))
    ok, problemas = servico.avaliar_resposta(chk, 200, {"ok": True}, '{"error": "a chave-secreta voltou"}')
    assert not ok and any("não deveria" in p for p in problemas)
    assert servico.avaliar_resposta(chk, 200, {"ok": True}, '{"ok": true}')[0]


# ---------- ServicoApp com token ----------

def test_servico_app_le_o_token_criado_pelo_servidor_e_anota_que_nao_existia(alvo_token, dados_falsos):
    pasta, _ = alvo_token
    with _app(pasta) as app:
        assert app.token_existia is False
        assert app.token is not None and app.token.valor == _token_no_disco(dados_falsos)


def test_servico_app_anota_token_que_ja_existia(alvo_token, dados_falsos):
    pasta, _ = alvo_token
    (dados_falsos / "MAW").mkdir()
    (dados_falsos / ARQUIVO).write_text("ab" * 32, encoding="ascii")
    with _app(pasta) as app:
        assert app.token_existia is True and app.token.valor == "ab" * 32


def test_servico_app_pede_uma_rota_protegida_para_o_servidor_criar_o_token(alvo_token, dados_falsos):
    pasta, _ = alvo_token
    with _app(pasta, ambiente_extra={"FAKE_CRIA_AO_SUBIR": "0"}) as app:
        assert app.token.valor == _token_no_disco(dados_falsos)


def test_servico_app_sem_token_legivel_fica_sem_valor(alvo_token):
    pasta, _ = alvo_token
    with _app(pasta, ambiente_extra={"FAKE_CRIA_AO_SUBIR": "0", "FAKE_NUNCA_CRIA": "1"}) as app:
        assert app.token is not None and app.token.valor is None


def test_servico_app_sem_config_de_token_nao_procura_token(alvo_token):
    pasta, _ = alvo_token
    with ServicoApp(pasta, script=SCRIPT, python=Path(sys.executable), ffmpeg=None, timeout_saude=10) as app:
        assert app.token is None and app.token_existia is None


# ---------- executar_checagem: as opções novas ----------

@pytest.fixture
def no_ar(alvo_token):
    pasta, _ = alvo_token
    with _app(pasta) as app:
        yield app


def test_token_certo_por_padrao_passa_do_porteiro(no_ar):
    chk = _chk(rota="/rota", metodo="POST", corpo={"a": 1}, chaves_esperadas=("recebido",))
    ok, problemas, detalhe = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    assert ok, problemas
    assert detalhe["corpo"]["recebido"] == {"a": 1}


def test_sem_token_e_401(no_ar):
    chk = _chk(rota="/rota", metodo="POST", corpo={}, token="sem", status_esperado=(401,), chaves_esperadas=("ok", "error"))
    ok, problemas, _ = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    assert ok, problemas


def test_token_errado_e_401(no_ar):
    chk = _chk(rota="/rota", metodo="POST", corpo={}, token="errado", status_esperado=(401,))
    ok, problemas, _ = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    assert ok, problemas


def test_alvo_sem_token_nunca_recebe_o_cabecalho(no_ar):
    """token=None (alvo sem token): o pedido sai como antes, sem cabeçalho nenhum — aqui o servidor
    falso exige o token, então dá 401."""
    chk = _chk(rota="/rota", metodo="POST", corpo={}, status_esperado=(200,))
    ok, problemas, detalhe = servico.executar_checagem(no_ar.base_url, chk, {})
    assert not ok and detalhe["status"] == 401


def test_cabecalho_host_proprio_com_a_porta_do_contexto(no_ar):
    estranho = _chk(rota="/health", cabecalhos={"Host": "rebinding.invalid:{porta}"}, status_esperado=(403,))
    aceito = _chk(rota="/health", cabecalhos={"Host": "localhost:{porta}"}, status_esperado=(200,),
                  chaves_esperadas=("ok",))
    ctx = {"porta": str(no_ar.porta)}
    assert servico.executar_checagem(no_ar.base_url, estranho, ctx, token=no_ar.token)[0]
    assert servico.executar_checagem(no_ar.base_url, aceito, ctx, token=no_ar.token)[0]


def test_corpo_declarado_maior_que_o_limite_sem_mandar_o_corpo(no_ar):
    """O corpo nunca é enviado: um servidor que tentasse lê-lo antes de recusar ficaria esperando e
    a checagem falharia por tempo — é isso que ela confere."""
    chk = _chk(rota="/rota", metodo="POST", content_length_declarado=LIMITE + 1, tipo_conteudo="application/json",
               status_esperado=(413,), chaves_esperadas=("ok", "error"), timeout=5)
    ok, problemas, detalhe = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    assert ok, problemas
    assert detalhe["status"] == 413


def test_transfer_encoding_sem_mandar_o_corpo(no_ar):
    for valor in ("chunked", "Chunked"):
        chk = _chk(rota="/rota", metodo="POST", transfer_encoding=valor, content_length_declarado=10,
                   status_esperado=(411,), timeout=5)
        ok, problemas, _ = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
        assert ok, (valor, problemas)


def test_pedido_cru_que_o_servidor_espera_ler_falha_por_tempo(no_ar):
    """Corpo declarado dentro do limite e nunca enviado: o servidor falso tenta lê-lo e trava — a
    checagem tem de falhar (por tempo), nunca passar nem levantar."""
    chk = _chk(rota="/rota", metodo="POST", content_length_declarado=10, status_esperado=(413,), timeout=1)
    ok, problemas, detalhe = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    assert not ok and "falhou" in problemas[0] and "excecao" in detalhe


def test_nonce_aleatorio_e_prova_conferida(no_ar):
    chk = _chk(rota="/health", nonce="aleatorio", chaves_esperadas=("ok", "prova"))
    ok, problemas, _ = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    assert ok, problemas


def test_prova_errada_e_problema(alvo_token):
    pasta, _ = alvo_token
    with _app(pasta, ambiente_extra={"FAKE_PROVA_ERRADA": "1"}) as app:
        chk = _chk(rota="/health", nonce="aleatorio")
        ok, problemas, _ = servico.executar_checagem(app.base_url, chk, {}, token=app.token)
    assert not ok and any("não confere" in p for p in problemas)


def test_nonce_sem_prova_na_resposta_e_problema(no_ar):
    chk = _chk(rota="/health", nonce="nao-e-hex")  # o servidor falso só prova nonce em hex
    ok, problemas, _ = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    assert not ok and any("ausente" in p for p in problemas)


def test_repeticoes_todas_passam(no_ar):
    chk = _chk(rota="/health", repeticoes=20, chaves_esperadas=("ok",))
    ok, problemas, detalhe = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    assert ok, problemas
    assert detalhe["pedidos"] == 20


def test_token_nunca_sai_na_evidencia_nem_nos_problemas(no_ar, dados_falsos):
    chk = _chk(rota="/eco-token", status_esperado=(200,))
    ok, problemas, detalhe = servico.executar_checagem(no_ar.base_url, chk, {}, token=no_ar.token)
    valor = _token_no_disco(dados_falsos)
    assert not ok
    assert valor not in json.dumps(detalhe) and valor not in json.dumps(problemas)
    assert "[REDACTED]" in json.dumps(detalhe)


# ---------- saída num pipe que ninguém lê ----------

def test_servico_app_com_saida_em_pipe(alvo_token, tmp_path):
    pasta, _ = alvo_token
    with _app(pasta, saida_em_pipe=True) as app:
        assert app.saude["ok"] is True
    captura = json.loads((pasta / "captura.json").read_text(encoding="utf-8"))
    assert captura["stdout_pipe"] is True


def test_repeticoes_acusam_servidor_que_trava_com_o_pipe_cheio(alvo_token):
    pasta, _ = alvo_token
    chk = _chk(rota="/barulhento", repeticoes=30, timeout=2)
    with _app(pasta, saida_em_pipe=True) as app:
        ok, problemas, _ = servico.executar_checagem(app.base_url, chk, {}, token=app.token)
    assert not ok and "pedido" in problemas[0]


def test_repeticoes_passam_quando_o_servidor_desliga_o_pipe(alvo_token):
    pasta, _ = alvo_token
    chk = _chk(rota="/barulhento", repeticoes=30, timeout=2)
    with _app(pasta, saida_em_pipe=True, ambiente_extra={"FAKE_DESLIGA_PIPE": "1"}) as app:
        ok, problemas, _ = servico.executar_checagem(app.base_url, chk, {}, token=app.token)
    assert ok, problemas
