"""Sobe o script do serviço de IA de um alvo (Python 3.10, Flask) e avalia as checagens HTTP dos
cenários. Nada de conhecimento específico do alvo mora aqui (nome do script, rotas, texto de erro,
nomes de pacote): isso vem sempre de `privado/cenarios/servico.yaml`, lido por `fase_servico.py`.

`ServicoApp` é o único jeito de rodar o serviço de verdade: sobe o processo com o python do serviço
(`work/py310`), espera `/health` responder e derruba a árvore de processos no fim — mesmo que a
espera por `/health` estoure ou que uma checagem levante uma exceção no meio do `with`.
"""
from __future__ import annotations
import io
import json
import os
import re
import socket
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import psutil
import requests

from . import config, sandbox

DEFAULT_TIMEOUT_SAUDE = 30.0
DEFAULT_TIMEOUT_ENCERRAR = 10.0
PORTA_PADRAO = 5000  # o que o Flask abriria sem nenhum app.run(port=...) reconhecível

PYTHON_SERVICO_PADRAO = config.WORK / "py310" / "Scripts" / "python.exe"
FFMPEG_BIN_PADRAO = config.FERRAMENTAS / "ffmpeg" / "bin"

_RX_APP_RUN = re.compile(r"app\.run\(\s*port\s*=\s*(\w+)\s*\)")
_RX_CONST_INT = re.compile(r"(?m)^\s*(\w+)\s*=\s*(\d+)\s*(?:#.*)?$")

# Qualquer variável de ambiente que pareça uma credencial nunca chega ao processo filho — genérico
# de propósito: o agente nunca precisa saber o NOME da variável de uma chave para não vazá-la.
_RX_SEGREDO_AMBIENTE = re.compile(r"(?i)(gemini|api[_-]?key|secret|token|password|senha)")

# Mensagens de erro do próprio SO/Python quando duas coisas tentam abrir a mesma porta — genéricas,
# não citam nada específico do alvo.
_RX_PORTA_EM_USO = re.compile(r"(?i)address already in use|only one usage of each socket address|"
                              r"winerror 10048|eaddrinuse")


class ServicoIndisponivel(Exception):
    """`/health` não respondeu dentro do timeout, ou o processo morreu antes disso."""


class PortaOcupada(ServicoIndisponivel):
    """A porta já estava (ou passou a estar, num race) ocupada por outro processo — nunca é um
    defeito do alvo: o agente não inicia nem mexe no processo que já está lá."""


def _ambiente_sem_segredos(base: dict[str, str]) -> dict[str, str]:
    """Cópia de `base` sem nenhuma variável cujo NOME pareça uma credencial (regra genérica —
    nenhum nome de variável específico do alvo é citado aqui nem em lugar nenhum do código
    público). O processo do serviço nunca recebe do agente nada que pareça uma chave."""
    return {k: v for k, v in base.items() if not _RX_SEGREDO_AMBIENTE.search(k)}


def processo_na_porta(porta: int, host: str = "127.0.0.1") -> tuple[int | None, str] | None:
    """(pid, nome do processo) de quem já escuta `porta`, ou `None` se ela estiver livre. Só
    verifica — nunca termina nem mexe no processo achado. Sem permissão para listar as conexões
    de outros processos, cai para um teste de conexão simples (sem pid nem nome)."""
    try:
        conexoes = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError, OSError):
        return (None, "?") if porta_aberta(porta, host) else None
    for c in conexoes:
        if c.laddr and c.laddr.port == porta and c.status == psutil.CONN_LISTEN:
            nome = "?"
            if c.pid:
                try:
                    nome = psutil.Process(c.pid).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    nome = "?"
            return (c.pid, nome)
    return None


def _mensagem_porta_ocupada(porta: int, ocupado: tuple[int | None, str]) -> str:
    pid, nome = ocupado
    if pid is None:
        return f"porta {porta} ocupada por outro processo (pid desconhecido, {nome})"
    return f"porta {porta} ocupada por outro processo (pid {pid}, {nome})"


def detectar_porta(script: Path, padrao: int = PORTA_PADRAO) -> int:
    """Porta que `app.run(port=...)` abre: lê o literal ou resolve o nome da constante que o
    antecede (`NOME = <número>`, em qualquer lugar do arquivo). Sem `app.run(port=...)`
    reconhecível, devolve `padrao` — cada alvo pode ter fixado a porta de um jeito diferente."""
    texto = Path(script).read_text(encoding="utf-8", errors="replace")
    m = _RX_APP_RUN.search(texto)
    if not m:
        return padrao
    token = m.group(1)
    if token.isdigit():
        return int(token)
    constantes = dict(_RX_CONST_INT.findall(texto))
    return int(constantes[token]) if token in constantes else padrao


def _matar_arvore(processo: subprocess.Popen | None, timeout: float = DEFAULT_TIMEOUT_ENCERRAR) -> None:
    """Mata o processo e todos os filhos (o Flask em modo debug poderia ter religado um reloader;
    aqui não usamos debug=True, mas a árvore é morta por segurança). Nunca levanta exceção."""
    if processo is None or processo.poll() is not None:
        return
    vivos: list[psutil.Process] = []
    try:
        pai = psutil.Process(processo.pid)
        vivos = pai.children(recursive=True) + [pai]
    except psutil.NoSuchProcess:
        pass
    for p in vivos:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    if vivos:
        _, restantes = psutil.wait_procs(vivos, timeout=timeout)
        for p in restantes:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
    try:
        processo.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _morte_por_porta_ocupada(porta: int, log: Path | None) -> ServicoIndisponivel | None:
    """Se o processo morreu porque a porta já estava em uso (a mensagem do SO aparece no log),
    devolve o `PortaOcupada` certo — reconferindo quem está na porta agora, se possível — ou
    `None` se a morte não tem cara de conflito de porta."""
    if log is None or not log.exists():
        return None
    texto = log.read_text(encoding="utf-8", errors="replace")[-4000:]
    if not _RX_PORTA_EM_USO.search(texto):
        return None
    ocupado = processo_na_porta(porta)
    if ocupado is not None:
        return PortaOcupada(_mensagem_porta_ocupada(porta, ocupado))
    return PortaOcupada(f"porta {porta} ocupada por outro processo (conflito momentâneo ao subir)")


def _esperar_saude(base_url: str, porta: int, timeout: float, processo: subprocess.Popen,
                   log: Path | None = None) -> dict:
    fim = time.monotonic() + timeout
    ultimo_erro: Exception | None = None
    while time.monotonic() < fim:
        codigo = processo.poll()
        if codigo is not None:
            erro_porta = _morte_por_porta_ocupada(porta, log)
            if erro_porta is not None:
                raise erro_porta
            raise ServicoIndisponivel(f"o processo do serviço terminou sozinho (código {codigo}) "
                                      "antes de /health responder")
        try:
            r = requests.get(f"{base_url}/health", timeout=2)
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError) as ex:
            ultimo_erro = ex
        time.sleep(0.3)
    raise ServicoIndisponivel(f"/health não respondeu em {timeout}s (último erro: {ultimo_erro})")


class ServicoApp:
    """`with ServicoApp(pasta_alvo, script=..., python=..., ffmpeg=...) as app:` sobe o script do
    serviço de IA do alvo, espera /health e derruba a árvore de processos ao sair do `with`
    (sempre, mesmo em exceção). `script` é obrigatório e nunca tem um valor padrão aqui — quem
    chama lê o nome de verdade de `privado/cenarios/servico.yaml` (`fase_servico.py`); este módulo
    público nunca embute o nome do script de nenhum alvo.

    `porta=None` (o padrão) detecta a porta lendo o próprio script do alvo — ela não é configurável
    de fora — mas antes de subir o processo a porta é conferida: se algo já está escutando nela,
    `PortaOcupada` é levantada sem iniciar nem mexer nesse outro processo (nunca é um defeito do
    alvo). O mesmo vale para uma morte muito rápida cujo log mostre um erro de porta em uso.

    O ambiente do processo filho nunca carrega nenhuma variável cujo NOME pareça uma credencial
    (`_ambiente_sem_segredos`) — o agente não precisa saber o nome de nenhuma chave para não
    vazá-la.
    """

    def __init__(self, pasta_alvo: Path, *, script: str, python: Path | None = None,
                 ffmpeg: Path | None = None, porta: int | None = None,
                 timeout_saude: float = DEFAULT_TIMEOUT_SAUDE, ambiente_extra: dict[str, str] | None = None,
                 log: Path | None = None):
        # os padrões (python/ffmpeg) são lidos do módulo em tempo de chamada, não como valor
        # default do parâmetro, para os testes trocarem PYTHON_SERVICO_PADRAO/FFMPEG_BIN_PADRAO
        # com monkeypatch.
        self.pasta_alvo = Path(pasta_alvo)
        self.python = Path(python if python is not None else PYTHON_SERVICO_PADRAO)
        ffmpeg = ffmpeg if ffmpeg is not None else FFMPEG_BIN_PADRAO
        self.ffmpeg = Path(ffmpeg) if ffmpeg else None
        self.script = script
        self.porta = porta if porta is not None else detectar_porta(self.pasta_alvo / script)
        self.timeout_saude = timeout_saude
        self.ambiente_extra = dict(ambiente_extra or {})
        self.log = Path(log) if log else None
        self.processo: subprocess.Popen | None = None
        self.saude: dict | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    def __enter__(self) -> "ServicoApp":
        if not self.python.exists():
            raise FileNotFoundError(f"python do serviço não encontrado: {self.python}")
        if not (self.pasta_alvo / self.script).exists():
            raise FileNotFoundError(f"{self.script} não encontrado em {self.pasta_alvo}")
        ocupado = processo_na_porta(self.porta)
        if ocupado is not None:
            raise PortaOcupada(_mensagem_porta_ocupada(self.porta, ocupado))
        env = _ambiente_sem_segredos(dict(os.environ))
        if self.ffmpeg and Path(self.ffmpeg).is_dir():
            env["PATH"] = os.pathsep.join([str(self.ffmpeg), env.get("PATH", "")])
        env.update(self.ambiente_extra)
        # CREATE_NO_WINDOW: o serviço nunca abre uma janela de console, nem por um instante — a
        # máquina pode estar em uso ao mesmo tempo. CREATE_NEW_PROCESS_GROUP: mata a árvore sem
        # levar o processo do agente junto num sinal de grupo.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        saida = subprocess.DEVNULL
        if self.log is not None:
            sandbox.garantir_escrita(self.log)
            self.log.parent.mkdir(parents=True, exist_ok=True)
            saida = open(self.log, "wb")
        try:
            self.processo = subprocess.Popen(
                [str(self.python), self.script], cwd=str(self.pasta_alvo), env=env,
                stdout=saida, stderr=subprocess.STDOUT, creationflags=flags,
            )
        finally:
            if saida is not subprocess.DEVNULL:
                saida.close()
        try:
            self.saude = _esperar_saude(self.base_url, self.porta, self.timeout_saude, self.processo, self.log)
        except ServicoIndisponivel:
            _matar_arvore(self.processo)
            raise
        return self

    def __exit__(self, *exc) -> None:
        _matar_arvore(self.processo)


# ---------- checagens data-driven (privado/cenarios/servico.yaml) ----------

@dataclass
class Checagem:
    id: str
    rota: str
    itens_catalogo: list[str]
    metodo: str = "GET"
    corpo: dict | None = None
    corpo_bruto: str | None = None
    tipo_conteudo: str | None = None
    status_esperado: tuple[int, ...] = (200,)
    json_esperado: bool = True
    chaves_esperadas: tuple[str, ...] = ()
    contem: tuple[str, ...] = ()
    arquivos_esperados: tuple[str, ...] = ()
    arquivos_proibidos: tuple[str, ...] = ()
    esperado: str = ""
    principio: str | None = None
    bancada: str | None = None
    timeout: float = 30.0
    descricao: str = ""
    # 'pesado': baixa modelo ou demora muito (a checagem em si roda de qualquer jeito quando a fase
    # `servico` roda de verdade); só serve para os testes rápidos saberem quais pular.
    pesado: bool = False


@dataclass
class NaoTestavel:
    itens_catalogo: list[str]
    motivo: str


def _tupla(v) -> tuple:
    if v is None:
        return ()
    if isinstance(v, (list, tuple)):
        return tuple(v)
    return (v,)


def carregar_checagens(caminho: Path) -> dict[str, list]:
    """Lê `privado/cenarios/servico.yaml`: `script` (nome do script do serviço, único lugar onde
    esse nome existe — nunca em código público), `checagens` (HTTP), `comandos` (subprocesso
    externo), `portas` (TCP) e `nao_testavel` (itens do catálogo sem checagem automática, cada um
    com o motivo)."""
    import yaml
    dados = yaml.safe_load(Path(caminho).read_text(encoding="utf-8")) or {}
    checagens = []
    for c in dados.get("checagens", []):
        c = dict(c)
        c["status_esperado"] = _tupla(c.get("status_esperado", 200))
        c["chaves_esperadas"] = _tupla(c.get("chaves_esperadas"))
        c["contem"] = _tupla(c.get("contem"))
        c["arquivos_esperados"] = _tupla(c.get("arquivos_esperados"))
        c["arquivos_proibidos"] = _tupla(c.get("arquivos_proibidos"))
        c["itens_catalogo"] = list(c.get("itens_catalogo") or [])
        checagens.append(Checagem(**c))
    comandos = []
    for c in dados.get("comandos", []):
        c = dict(c)
        c["contem"] = _tupla(c.get("contem"))
        c["itens_catalogo"] = list(c.get("itens_catalogo") or [])
        comandos.append(ChecagemComando(**c))
    portas = []
    for p in dados.get("portas", []):
        p = dict(p)
        p["itens_catalogo"] = list(p.get("itens_catalogo") or [])
        portas.append(ChecagemPorta(**p))
    nao_testavel = [NaoTestavel(list(n["itens_catalogo"]), n["motivo"]) for n in dados.get("nao_testavel", [])]
    return {"script": dados.get("script"), "checagens": checagens, "comandos": comandos, "portas": portas,
            "nao_testavel": nao_testavel}


def resolver_corpo(corpo: dict | None, contexto: dict[str, str]) -> dict | None:
    """Substitui os tokens '{nome}' do corpo (strings) pelos caminhos reais em `contexto`
    (ex.: audio_valido, audio_inexistente, saida) — as fixtures são geradas por execução, não
    fixas no YAML, porque cada checagem usa a sua própria pasta de saída."""
    if corpo is None:
        return None
    def resolver(v):
        if isinstance(v, str):
            try:
                return v.format(**contexto)
            except (KeyError, IndexError):
                return v
        return v
    return {k: resolver(v) for k, v in corpo.items()}


def avaliar_resposta(chk: Checagem, status: int, corpo_json: dict | None, corpo_texto: str,
                     arquivos_existem: dict[str, bool] | None = None) -> tuple[bool, list[str]]:
    """Compara uma resposta HTTP de verdade (ou simulada, nos testes) contra o que `chk` espera.
    Função pura: não faz rede nem toca disco, só compara valores já obtidos. `arquivos_existem` é
    {caminho: existe?} para cada entrada de `arquivos_esperados`/`arquivos_proibidos`."""
    problemas: list[str] = []
    if status not in chk.status_esperado:
        problemas.append(f"status {status}, esperado {' ou '.join(map(str, chk.status_esperado))}")
    if chk.json_esperado:
        if corpo_json is None:
            problemas.append("a resposta não é um JSON válido")
        else:
            faltando = [k for k in chk.chaves_esperadas if k not in corpo_json]
            if faltando:
                problemas.append(f"chaves ausentes na resposta: {', '.join(faltando)}")
    for s in chk.contem:
        if s not in corpo_texto:
            problemas.append(f"texto esperado ausente na resposta: {s!r}")
    arquivos_existem = arquivos_existem or {}
    for caminho in chk.arquivos_esperados:
        if arquivos_existem.get(caminho) is False:
            problemas.append(f"arquivo esperado não foi criado: {caminho}")
    for caminho in chk.arquivos_proibidos:
        if arquivos_existem.get(caminho) is True:
            problemas.append(f"arquivo não deveria ter sido criado, mas existe: {caminho}")
    return (not problemas, problemas)


def executar_checagem(base_url: str, chk: Checagem, contexto: dict[str, str],
                      sessao: requests.Session | None = None) -> tuple[bool, list[str], dict]:
    """Manda a requisição de verdade e avalia com `avaliar_resposta`. `contexto` resolve os
    tokens do corpo (audio_valido, saida, ...). Devolve (ok, problemas, detalhe-para-evidência)."""
    s = sessao or requests
    corpo = resolver_corpo(chk.corpo, contexto)
    kwargs: dict = {"timeout": chk.timeout}
    if chk.corpo_bruto is not None:
        kwargs["data"] = resolver_corpo({"_": chk.corpo_bruto}, contexto)["_"].encode("utf-8")
        if chk.tipo_conteudo:
            kwargs["headers"] = {"Content-Type": chk.tipo_conteudo}
    elif corpo is not None:
        kwargs["json"] = corpo
    try:
        r = s.request(chk.metodo, f"{base_url}{chk.rota}", **kwargs)
    except requests.RequestException as ex:
        return False, [f"a requisição falhou: {ex}"], {"excecao": str(ex)}
    try:
        corpo_json = r.json()
    except ValueError:
        corpo_json = None
    caminhos = [resolver_corpo({"_": c}, contexto)["_"] for c in (*chk.arquivos_esperados, *chk.arquivos_proibidos)]
    arquivos_existem = {c: Path(c).exists() for c in caminhos}
    ok, problemas = avaliar_resposta(chk, r.status_code, corpo_json, r.text, arquivos_existem)
    detalhe = {"status": r.status_code, "corpo": corpo_json if corpo_json is not None else r.text[:2000]}
    return ok, problemas, detalhe


# ---------- checagens de comando e de porta (fora do HTTP) ----------

@dataclass
class ChecagemComando:
    id: str
    itens_catalogo: list[str]
    argumentos: list[str]
    codigo_esperado: int = 0
    contem: tuple[str, ...] = ()
    esperado: str = ""
    timeout: float = 60.0


@dataclass
class ChecagemPorta:
    id: str
    itens_catalogo: list[str]
    porta: int
    aberta_esperada: bool
    esperado: str = ""


def rodar_comando(python: Path, argumentos: list[str], timeout: float = 60.0,
                  cwd: Path | None = None) -> tuple[int | None, str]:
    """Roda `[python] + argumentos` e devolve (código de saída, stdout+stderr). Nunca levanta por
    timeout: devolve código None e a saída parcial (com o aviso de timeout no fim)."""
    try:
        p = subprocess.run([str(python), *argumentos], cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        saida = e.stdout or ""
        if isinstance(saida, bytes):
            saida = saida.decode("utf-8", errors="replace")
        return None, saida + f"\nTIMEOUT depois de {timeout}s"


def avaliar_comando(chk: ChecagemComando, codigo: int | None, saida: str) -> tuple[bool, list[str]]:
    problemas = []
    if codigo != chk.codigo_esperado:
        problemas.append(f"código de saída {codigo}, esperado {chk.codigo_esperado}")
    for s in chk.contem:
        if s not in saida:
            problemas.append(f"texto esperado ausente na saída: {s!r}")
    return (not problemas, problemas)


def porta_aberta(porta: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, porta), timeout=timeout):
            return True
    except OSError:
        return False


def avaliar_porta(chk: ChecagemPorta, aberta: bool) -> tuple[bool, list[str]]:
    if aberta == chk.aberta_esperada:
        return True, []
    estado = "aberta" if aberta else "fechada"
    esperado = "aberta" if chk.aberta_esperada else "fechada"
    return False, [f"porta {chk.porta} está {estado}, esperada {esperado}"]


def gerar_wav_curto(caminho: Path, segundos: float = 1.0, frequencia: float = 440.0, sr: int = 16000) -> Path:
    """Gera um WAV mono curtíssimo (tom puro) para as checagens que só precisam de um arquivo de
    áudio válido — nenhuma delas depende do conteúdo reconhecido, só de o serviço aceitar o arquivo.
    Escrito em memória e gravado por `sandbox.escrever_bytes` (nenhum arquivo do agente é escrito
    por fora do sandbox)."""
    import numpy as np
    import soundfile as sf
    t = np.linspace(0, segundos, int(sr * segundos), endpoint=False)
    onda = (0.2 * np.sin(2 * np.pi * frequencia * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, onda, sr, subtype="PCM_16", format="WAV")
    return sandbox.escrever_bytes(Path(caminho), buf.getvalue())


_RX_PALAVRA = re.compile(r"\w+", re.UNICODE)


def _palavras_normalizadas(texto: str) -> set[str]:
    """Minúsculas e sem acento, para comparar transcrições sem exigir grafia idêntica."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", texto.casefold())
                         if not unicodedata.combining(c))
    return set(_RX_PALAVRA.findall(sem_acento))


def plausibilidade_texto(obtido: str, esperado: str) -> float:
    """Fração das palavras de `esperado` (sem acento, minúsculas) que também aparecem em `obtido`
    — uma medida frouxa de "essa transcrição é plausível", não uma comparação exata. `0.0` se
    `esperado` não tiver nenhuma palavra. Função pura e genérica: não sabe nada sobre o alvo."""
    palavras_esperadas = _palavras_normalizadas(esperado)
    if not palavras_esperadas:
        return 0.0
    return len(palavras_esperadas & _palavras_normalizadas(obtido)) / len(palavras_esperadas)
