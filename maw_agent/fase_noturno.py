"""Execução noturna: `python -m maw_agent noturno <acao>`.

- `instalar | remover | status`: a tarefa diária no Agendador do Windows (só com o usuário conectado,
  sem privilégio elevado) que chama `ferramentas/noturno.ps1`.
- `rodar`: a sprint inteira sem ninguém olhando. As fases determinísticas (compilar, suíte, E2E,
  serviço, calibração) rodam aqui, fora do LLM; o `claude -p` entra só nas fases de julgamento.
  Cada etapa com falha é registrada e a noite segue; o log (redigido) fica em `work/noturno/<data>.log`.
  O `claude -p` roda com `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` (um subagente em segundo plano não é
  cortado por inatividade), mas o `/sprint --noturno` despacha tudo em primeiro plano: comando Bash
  deixado em segundo plano morre logo depois da resposta final do `claude -p`.
- `bancada`: monta os resultados de `bancada.json` em lotes para a página da bancada do usuário
  (a URL da página fica no repositório privado, em `privado/bancada/config.yaml`).
- `limitacao`: declara uma limitação na sprint em andamento (`limitacoes-<origem>.json`).
"""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

import yaml

from . import bancada, config, estado, redacao, sandbox
from .cli import registrar

NOME_TAREFA = "Agente MAW - sprint noturna"
SCRIPT = config.RAIZ / "ferramentas" / "noturno.ps1"
LOGS = config.WORK / "noturno"
# a URL da página da bancada do usuário (nunca no repositório público); a variável de ambiente manda
CONFIG_BANCADA = config.PRIVADO / "bancada" / "config.yaml"
COLECAO_BANCADA = "resultados"
TAM_LOTE = 50  # máximo de escritas por chamada `batch` do ArtifactData
LIMITE_NOTA = 1000
PROMPT_REVISAO = "/sprint --retomar --ate revisao --noturno"
PROMPT_FINAL = "/sprint --retomar --noturno"
LIMITE_MANHA = "07:00"   # depois disso nada que abre a MAW começa (e a E2E com entrada real é cortada)
INICIO_NOITE = "20:00"   # antes disso, em modo automático, a entrada real fica desligada
PRAZO_FINAL = "11:00"    # a noite termina antes disso, com o relatório (bem antes do PT14H da tarefa)
TERMINA_NA_TAREFA = "PT14H"  # o Agendador encerra a execução que passar disso

HORA = 3600
RESERVA_RELATORIO = 1 * HORA  # do prazo final, o que fica guardado para consolidar, encerrar e relatar
NIVEL_SOM = 0.20  # volume da saída padrão durante a E2E: baixo, mas com sinal para o loopback (mudo o zera)
# o que o julgamento precisa marcar para ter feito o seu trabalho
ESPECIALISTAS_MAIN = ("testador-motor", "testador-edicao", "testador-midi", "testador-efeitos",
                      "testador-projeto", "testador-interface", "testador-ia", "guardiao-da-ideia")
MARCAS_FINAIS = ("executar", "verificar", "consolidar", "encerrar", "bancada", "textos", "relatorio")
# da CLI, o julgamento (claude -p) só roda estes; o resto é do script e a noite o proíbe na linha de comando
CLI_DO_JULGAMENTO = ("sprint status", "sprint marcar", "sprint consolidar", "sprint encerrar", "sprint relatorio",
                     "achado validar", "achado registrar", "catalogo validar", "catalogo publicar",
                     "noturno bancada", "noturno limitacao")
CLI_SO_DO_SCRIPT = ("sprint iniciar", "sprint preflight", "sprint preparar", "sprint compilar", "sprint suite",
                    "e2e", "servico", "calibrar", "sondas injetar", "noturno instalar", "noturno remover",
                    "noturno rodar")
_RX_ALERTA_CLAUDE = re.compile(r"not been trusted|Ignoring \d+ permissions?\.allow", re.IGNORECASE)
_RX_HORA = re.compile(r"(\d{1,2}):(\d{2})")
_RX_ALVO = re.compile(r"[A-Za-z0-9._\-]{1,100}")
_RX_ORIGEM = re.compile(r"[a-z0-9][a-z0-9\-]{0,40}")
# um segmento de id de documento do banco da página (e sem '__', que separa código/sistema/quem)
_RX_CODIGO = re.compile(r"[A-Za-z0-9_\-.~:@+]{1,150}")
# variáveis que marcam uma sessão do Claude Code em andamento: não passam para o `claude -p` da noite
_MARCAS_SESSAO_CLAUDE = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION",
                         "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                         "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
                         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
                         "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "CLAUDE_CODE_ENABLE_TASKS")
_NS_TAREFA = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _saida(obj: dict, ok: bool = True) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0 if ok else 1


# ---------- argumentos ----------

def hora_valida(texto: str) -> str:
    """'HH:MM' (0–23, 0–59) normalizado com dois dígitos; tipo do argparse."""
    m = _RX_HORA.fullmatch(str(texto).strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise argparse.ArgumentTypeError(f"hora inválida: {texto!r} (use HH:MM, ex.: 22:00)")
    return f"{int(m[1]):02d}:{m[2]}"


def _alvo_valido(texto: str) -> str:
    if not _RX_ALVO.fullmatch(texto):
        raise argparse.ArgumentTypeError(f"nome de alvo inválido: {texto!r}")
    return texto


def _origem_valida(texto: str) -> str:
    if not _RX_ORIGEM.fullmatch(texto):
        raise argparse.ArgumentTypeError(f"origem inválida: {texto!r} (letras minúsculas, dígitos e '-')")
    return texto


@dataclass(frozen=True)
class Opcoes:
    ensaio: bool = False
    calibrar: bool = False
    so_alvo: str | None = None
    sem_julgamento: bool = False
    retomar: bool = False
    limite: str = LIMITE_MANHA
    entrada_real: str = "auto"  # auto | sim | nao
    prazo_final: str = PRAZO_FINAL


def opcoes_de(args: argparse.Namespace) -> Opcoes:
    return Opcoes(ensaio=args.ensaio, calibrar=args.calibrar, so_alvo=args.so_alvo,
                  sem_julgamento=args.sem_julgamento, retomar=args.retomar, limite=args.limite,
                  entrada_real=args.entrada_real, prazo_final=args.prazo_final)


def _hm(texto: str) -> tuple[int, int]:
    h, m = hora_valida(texto).split(":")
    return int(h), int(m)


def decidir_entrada_real(modo: str, inicio: datetime, limite: str) -> bool:
    """Teclado e mouse reais só de noite: `auto` liga entre INICIO_NOITE e o limite da manhã."""
    if modo in ("sim", "nao"):
        return modo == "sim"
    agora = (inicio.hour, inicio.minute)
    return agora >= _hm(INICIO_NOITE) or agora < _hm(limite)


def prazo_manha(inicio: datetime, limite: str) -> datetime:
    """O próximo `limite` (HH:MM) depois do início da execução."""
    h, m = _hm(limite)
    prazo = inicio.replace(hour=h, minute=m, second=0, microsecond=0)
    return prazo if prazo > inicio else prazo + timedelta(days=1)


def ambiente_filhos(base: dict, entrada_real: bool) -> dict:
    """Ambiente de todo processo da noite: saída em UTF-8, entrada real decidida aqui, subagentes em
    segundo plano do `claude -p` sem o corte por inatividade e sem as marcas de uma sessão aberta do
    Claude Code."""
    env = {k: v for k, v in base.items() if k not in _MARCAS_SESSAO_CLAUDE}
    env.update({"MAW_AGENTE_ENTRADA_REAL": "1" if entrada_real else "0", "MAW_AGENTE_NOTURNO": "1",
                "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def decodificar(dados: bytes) -> str:
    """Saída de ferramenta do Windows: UTF-8 se for, senão a página de código OEM do console."""
    try:
        return dados.decode("utf-8")
    except UnicodeDecodeError:
        try:
            cp = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except (AttributeError, OSError):
            cp = "cp850"
        return dados.decode(cp, errors="replace")


# ---------- tarefa agendada ----------

def usuario_atual() -> str:
    return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")


def argumentos_powershell(script: Path) -> str:
    # janela oculta, não minimizada: fechar por engano a janela minimizada matava a noite inteira
    return f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script}"'


def xml_tarefa(hora: str, script: Path, raiz: Path, usuario: str, dia: date) -> str:
    """Definição da tarefa: diária na `hora`, só com o usuário conectado (a fase de GUI precisa da
    área de trabalho), sem elevação, também na bateria, uma execução por vez e com prazo máximo."""
    e = lambda s: escape(str(s))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{_NS_TAREFA}">
  <RegistrationInfo>
    <Author>{e(usuario)}</Author>
    <Description>Sprint noturna do Agente MAW (ferramentas\\noturno.ps1). Instalada por: python -m maw_agent noturno instalar</Description>
    <URI>\\{e(NOME_TAREFA)}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{dia.isoformat()}T{hora_valida(hora)}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{e(usuario)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{TERMINA_NA_TAREFA}</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>{e(argumentos_powershell(script))}</Arguments>
      <WorkingDirectory>{e(raiz)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def comando_instalar(xml: Path) -> list[str]:
    return ["schtasks", "/Create", "/TN", NOME_TAREFA, "/XML", str(xml), "/F"]


def comando_remover() -> list[str]:
    return ["schtasks", "/Delete", "/TN", NOME_TAREFA, "/F"]


def comando_consultar() -> list[str]:
    return ["schtasks", "/Query", "/TN", NOME_TAREFA, "/XML"]


def ler_xml_tarefa(xml: str) -> dict:
    """O que importa da definição registrada (o XML não depende do idioma do Windows)."""
    raiz = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", xml))
    ns = {"t": _NS_TAREFA}
    txt = lambda caminho: (raiz.findtext(caminho, default="", namespaces=ns) or "").strip()
    argumentos = txt("t:Actions/t:Exec/t:Arguments")
    m = re.search(r'-File\s+"([^"]+)"', argumentos)
    inicio = txt("t:Triggers/t:CalendarTrigger/t:StartBoundary")
    return {"hora": inicio[11:16] or None,
            "diaria": txt("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval") == "1",
            "somente_conectado": txt("t:Principals/t:Principal/t:LogonType") == "InteractiveToken",
            "elevada": txt("t:Principals/t:Principal/t:RunLevel") == "HighestAvailable",
            "habilitada": txt("t:Settings/t:Enabled") != "false",
            "roda_na_bateria": txt("t:Settings/t:DisallowStartIfOnBatteries") == "false",
            "comando": txt("t:Actions/t:Exec/t:Command"), "argumentos": argumentos,
            "script": m.group(1) if m else None, "pasta": txt("t:Actions/t:Exec/t:WorkingDirectory")}


_PS_EXECUCAO = (
    "$t = Get-ScheduledTask -TaskName '{nome}' -ErrorAction Stop; $i = $t | Get-ScheduledTaskInfo; "
    "[pscustomobject]@{{estado = [string]$t.State; "
    "proxima_execucao = $(if ($i.NextRunTime) {{ $i.NextRunTime.ToString('s') }} else {{ $null }}); "
    "ultima_execucao = $(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) "
    "{{ $i.LastRunTime.ToString('s') }} else {{ $null }}); "
    "ultimo_resultado = $i.LastTaskResult}} | ConvertTo-Json -Compress")


def _execucao_da_tarefa() -> dict:
    """Próxima e última execução (o `schtasks` só as mostra em texto traduzido; o PowerShell não)."""
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            _PS_EXECUCAO.format(nome=NOME_TAREFA)], capture_output=True, timeout=120)
        if p.returncode == 0 and p.stdout.strip():
            return json.loads(decodificar(p.stdout))
        return {"erro_execucao": decodificar(p.stderr or p.stdout).strip()[:300]}
    except (OSError, subprocess.TimeoutExpired, ValueError) as ex:
        return {"erro_execucao": str(ex)}


def status_tarefa() -> dict:
    try:
        p = subprocess.run(comando_consultar(), capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {"instalada": False, "nome": NOME_TAREFA, "erro": str(ex)}
    if p.returncode != 0:
        return {"instalada": False, "nome": NOME_TAREFA, "detalhe": decodificar(p.stderr or p.stdout).strip()}
    try:
        info = ler_xml_tarefa(decodificar(p.stdout))
    except ET.ParseError as ex:
        return {"instalada": True, "nome": NOME_TAREFA, "erro": f"definição ilegível: {ex}"}
    return {"instalada": True, "nome": NOME_TAREFA, **info, **_execucao_da_tarefa()}


def _instalar(args) -> int:
    xml = xml_tarefa(args.hora, SCRIPT, config.RAIZ, usuario_atual(), date.today())
    destino = sandbox.escrever_bytes(LOGS / "tarefa.xml", xml.encode("utf-16"))  # o schtasks lê UTF-16
    p = subprocess.run(comando_instalar(destino), capture_output=True, timeout=120)
    if p.returncode != 0:
        return _saida({"ok": False, "erro": decodificar(p.stderr or p.stdout).strip()}, False)
    st = status_tarefa()
    return _saida({"ok": bool(st.get("instalada")), **st}, bool(st.get("instalada")))


def _remover(args) -> int:
    p = subprocess.run(comando_remover(), capture_output=True, timeout=120)
    ok = p.returncode == 0
    return _saida({"ok": ok, "removida": ok, "detalhe": decodificar(p.stdout if ok else (p.stderr or p.stdout)).strip()},
                  ok)


def _status(args) -> int:
    st = status_tarefa()
    return _saida(st, bool(st.get("instalada")))


# ---------- limitações e bancada ----------

def acrescentar_limitacao(pasta: Path, texto: str, origem: str = "noturno") -> None:
    """Uma linha em `limitacoes-<origem>.json` da sprint (o PDF lê todos), sem repetir."""
    p = Path(pasta) / f"limitacoes-{origem}.json"
    atuais = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    texto = redacao.redigir(str(texto).strip())
    if texto and texto not in atuais:
        sandbox.escrever_json(p, atuais + [texto])


def bancada_desativada() -> bool:
    """`desativada: true` em `privado/bancada/config.yaml`: o usuário escolheu não usar a página da bancada —
    os resultados ficam só em `bancada.json` e no PDF, sem envio e sem limitação."""
    try:
        d = yaml.safe_load(Path(CONFIG_BANCADA).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    return bool(isinstance(d, dict) and d.get("desativada"))


def url_bancada() -> str | None:
    """A página da bancada: `MAW_AGENTE_BANCADA_URL`, senão `url` de `privado/bancada/config.yaml`."""
    if os.environ.get("MAW_AGENTE_BANCADA_URL"):
        return os.environ["MAW_AGENTE_BANCADA_URL"]
    try:
        d = yaml.safe_load(Path(CONFIG_BANCADA).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    url = d.get("url") if isinstance(d, dict) else None
    return str(url).strip() if url else None


def agora_iso() -> str:
    """Mesmo formato que a página grava (`new Date().toISOString()`)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _nota_bancada(nota, sprint: str, alvo) -> str:
    origem = f"({sprint}, alvo {alvo})" if alvo and alvo != "main" else f"({sprint})"
    base = redacao.redigir(str(nota or "").strip())  # redige antes de cortar: nada de meia chave
    espaco = LIMITE_NOTA - len(origem) - 1
    if len(base) > espaco:
        base = base[:espaco - 1].rstrip() + "…"
    return f"{base} {origem}" if base else origem


def plataformas_da_bancada(pasta: Path | None = None) -> dict[str, list[str]]:
    """`{código: [plataformas]}` a partir da cópia local da bancada (`work/bancada/testes/<código>.json`).
    Ausente ou ilegível: dicionário vazio (todo código vai como `windows`)."""
    pasta = Path(pasta) if pasta else config.WORK / "bancada" / "testes"
    saida: dict[str, list[str]] = {}
    for arq in sorted(pasta.glob("*.json")) if pasta.is_dir() else []:
        try:
            plats = json.loads(arq.read_text(encoding="utf-8")).get("plataformas") or []
        except (OSError, ValueError, AttributeError):
            continue
        saida[arq.stem] = [p for p in plats if p in ("windows", "linux", "macos")]
    return saida


def lotes_bancada(dados: dict, sprint: str, em: str, tam: int = TAM_LOTE,
                  plataformas: dict[str, list[str]] | None = None) -> tuple[list[list[dict]], list[str]]:
    """`{código: {estado, nota, alvo}}` → lotes de escritas `set` para o `ArtifactData` (ação `batch`),
    no formato da página: `resultados/<código>__<sistema>__agente`. O sistema é `windows` quando o item vale
    para o Windows; um item só de Linux e/ou macOS vai para cada um desses sistemas (esta máquina é Windows,
    então ali ele é sempre `pulei`)."""
    escritas: list[dict] = []
    ignorados: list[str] = []
    for codigo in sorted(dados):
        r = dados[codigo] if isinstance(dados[codigo], dict) else {}
        est = r.get("estado")
        if est not in bancada.ESTADOS:
            ignorados.append(f"{codigo}: estado inválido {est!r}")
            continue
        if not _RX_CODIGO.fullmatch(codigo) or "__" in codigo or codigo in (".", ".."):
            ignorados.append(f"{codigo!r}: código inválido para a bancada")
            continue
        plats = (plataformas or {}).get(codigo) or ["windows"]
        for plat in (["windows"] if "windows" in plats else plats):
            escritas.append({"op": "set", "collection": COLECAO_BANCADA, "doc_id": f"{codigo}__{plat}__agente",
                             "data": {"teste": codigo, "plataforma": plat, "quem": "agente",
                                      "estado": est if plat == "windows" else "pulei",
                                      "nota": _nota_bancada(r.get("nota"), sprint, r.get("alvo")), "em": em}})
    return [escritas[i:i + tam] for i in range(0, len(escritas), tam)], ignorados


def _pasta_da_sprint(args) -> Path | None:
    if getattr(args, "pasta", None):
        return Path(args.pasta)
    e = estado.em_andamento()
    return e.pasta if e else None


def _bancada(args) -> int:
    pasta = _pasta_da_sprint(args)
    if pasta is None:
        return _saida({"ok": False, "erro": "nenhuma sprint em andamento: use --pasta"}, False)
    if bancada_desativada():
        return _saida({"ok": False, "desativada": True,
                       "motivo": "envio à página da bancada desativado: os resultados ficam em bancada.json e no PDF"},
                      False)
    try:
        dados = bancada.carregar(pasta)
        motivo = None if dados else ("a sprint não gerou bancada.json (a fase E2E não rodou ou não produziu "
                                     "resultados)" if not (pasta / "bancada.json").exists()
                                     else "bancada.json está vazio")
    except (ValueError, OSError) as ex:
        dados, motivo = {}, f"bancada.json ilegível: {ex}"
    if motivo:
        texto = f"resultados da bancada não enviados à página da bancada: {motivo}"
        acrescentar_limitacao(pasta, texto, "bancada")
        return _saida({"ok": False, "motivo": texto}, False)
    url = url_bancada()
    if not url:
        texto = ("resultados da bancada não enviados à página da bancada: URL da página não configurada "
                 "(privado/bancada/config.yaml, chave url, ou MAW_AGENTE_BANCADA_URL)")
        acrescentar_limitacao(pasta, texto, "bancada")
        return _saida({"ok": False, "motivo": texto}, False)
    lotes, ignorados = lotes_bancada(dados, pasta.name, agora_iso(), plataformas=plataformas_da_bancada())
    if ignorados:
        acrescentar_limitacao(pasta, f"{len(ignorados)} resultado(s) da bancada fora do formato, não enviados: "
                              + "; ".join(ignorados[:5]), "bancada")
    return _saida({"ok": True, "url": url, "colecao": COLECAO_BANCADA,
                   "total": sum(len(l) for l in lotes), "lotes": lotes, "ignorados": ignorados})


def _limitacao(args) -> int:
    pasta = _pasta_da_sprint(args)
    if pasta is None:
        return _saida({"ok": False, "erro": "nenhuma sprint em andamento: use --pasta"}, False)
    acrescentar_limitacao(pasta, args.texto, args.origem)
    return _saida({"ok": True, "arquivo": str(pasta / f"limitacoes-{args.origem}.json")})


# ---------- execução de etapas ----------

@dataclass
class Etapa:
    nome: str
    comando: list[str]
    timeout: float
    mexe_na_maw: bool = False    # abre a MAW: não começa depois do limite da manhã; se estourar, restaura
    entrada_real: bool = False   # usa teclado e mouse reais: cortada no limite da manhã
    guardar: int = 2000          # linhas finais guardadas em Resultado.saida
    mecanica: bool = False       # fase determinística que o pré-voo bloqueado pula
    analisar: bool = False       # saída do `claude -p`: procura avisos de confiança e a linha final
    parar: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)


@dataclass
class Resultado:
    codigo: int | None
    segundos: float
    estourou: bool = False
    erro: str | None = None      # a etapa nem começou, foi interrompida ou quebrou no próprio agente
    saida: str = ""
    alertas: list[str] = field(default_factory=list)  # avisos do Claude Code (confiança, regras ignoradas)
    final: dict | None = None    # resumo da linha {"type": "result"} do stream-json

    @property
    def ok(self) -> bool:
        return self.codigo == 0 and not self.estourou and self.erro is None


class Registro:
    """Log da noite: cada linha redigida e com hora, gravada na hora (serve mesmo se a execução cair)."""

    def __init__(self, caminho: Path, eco: bool = True):
        self.caminho = sandbox.garantir_escrita(Path(caminho))
        sandbox.criar_pasta(self.caminho.parent)
        self.eco = eco
        self._trava = threading.Lock()

    def linha(self, texto: str, etapa: str = "") -> None:
        limpo = redacao.redigir(str(texto).rstrip("\r\n"))
        prefixo = time.strftime("%H:%M:%S") + (f" [{etapa}]" if etapa else "")
        with self._trava:
            with open(self.caminho, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"{prefixo} {limpo}\n")
            if self.eco:  # no console, cortado: o log completo é o arquivo
                try:
                    print(f"{prefixo} {limpo[:300]}", file=sys.stderr, flush=True)
                except (OSError, ValueError):
                    pass


def matar_arvore(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _duracao(segundos: float) -> str:
    return f"{segundos / HORA:.1f} h" if segundos >= HORA else f"{segundos / 60:.0f} min"


def _resumo_final(obj: dict) -> dict:
    """Da linha final do stream-json, só o que diz se o julgamento terminou bem."""
    negadas = obj.get("permission_denials") or []
    return {"subtype": obj.get("subtype"), "is_error": bool(obj.get("is_error")), "num_turns": obj.get("num_turns"),
            "permission_denials": [{"tool_name": d.get("tool_name")} for d in negadas if isinstance(d, dict)],
            "result": redacao.redigir(str(obj.get("result") or ""))[:300]}


def executar_etapa(etapa: Etapa, registro: Registro, env: dict, cwd: Path, timeout: float | None = None) -> Resultado:
    """Roda um processo, passando cada linha (redigida) para o log. Espera pelo processo, não pelo
    fim do pipe (um neto que herdou o pipe não segura a noite); no tempo limite ou quando alguém
    pede `etapa.parar`, mata a árvore de processos."""
    limite = etapa.timeout if timeout is None else timeout
    registro.linha("começa: " + " ".join(etapa.comando), etapa.nome)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(etapa.comando, cwd=str(cwd), env=env or None, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as ex:
        registro.linha(f"não começou: {ex}", etapa.nome)
        return Resultado(None, 0.0, False, str(ex), "")
    linhas: deque[str] = deque(maxlen=etapa.guardar)
    alertas: list[str] = []
    final: list[dict] = []

    def ler() -> None:
        for bruta in proc.stdout:
            txt = redacao.redigir(decodificar(bruta).rstrip("\r\n"))
            registro.linha(txt, etapa.nome)
            linhas.append(txt)
            if etapa.analisar:
                if _RX_ALERTA_CLAUDE.search(txt) and len(alertas) < 20:
                    alertas.append(txt[:300])
                if txt.startswith("{") and '"result"' in txt:
                    try:
                        obj = json.loads(txt)
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict) and obj.get("type") == "result":
                        final[:] = [_resumo_final(obj)]

    leitor = threading.Thread(target=ler, daemon=True, name=f"leitor-{etapa.nome}")
    leitor.start()
    fim = time.monotonic() + limite
    estourou = interrompida = False
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if etapa.parar.is_set():
                interrompida = True
                registro.linha("interrompida pela execução noturna: encerrando a árvore de processos", etapa.nome)
            elif time.monotonic() >= fim:
                estourou = True
                registro.linha(f"passou do tempo limite ({_duracao(limite)}): encerrando a árvore de processos",
                               etapa.nome)
            else:
                continue
            matar_arvore(proc.pid)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=60)
            break
    leitor.join(timeout=30)
    return Resultado(proc.returncode, time.monotonic() - t0, estourou,
                     "interrompida pela execução noturna" if interrompida else None, "\n".join(linhas),
                     alertas=alertas, final=final[0] if final else None)


# ---------- o julgamento (claude -p) ----------

def regra_home(sufixo: str) -> str:
    """`Read(//c/Users/<usuário><sufixo>)`: caminho absoluto na forma das regras do Claude Code."""
    casa = Path.home().as_posix()
    if len(casa) > 1 and casa[1] == ":":
        casa = "/" + casa[0].lower() + casa[2:]
    return f"Read(/{casa}{sufixo})"


def regras_proibidas_noite() -> list[str]:
    """Negadas só na linha de comando da noite: o que é do script (no uso interativo, essas pedem
    confirmação em vez de serem negadas) e a leitura da configuração do Claude Code."""
    regras = []
    for c in CLI_SO_DO_SCRIPT:
        regras.append(f"Bash(.venv/Scripts/python* -m maw_agent {c}*)")
        regras.append(f"Bash(PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python* -m maw_agent {c}*)")
    return regras + [regra_home("/.claude/**")]


def comando_claude(claude: str, prompt: str) -> list[str]:
    # sem --bare (ele ignora .claude/commands e agents); sem --max-turns; nunca pergunta nada
    return [claude, "-p", prompt, "--permission-mode", "dontAsk", "--output-format", "stream-json", "--verbose",
            "--disallowedTools", *regras_proibidas_noite()]


def diagnosticar_julgamento(r: Resultado) -> list[str]:
    """A causa real de um `claude -p` que não fez o seu trabalho (lista vazia = terminou bem)."""
    causas: list[str] = []
    if any("trusted" in a.lower() for a in r.alertas):
        causas.append("o Claude Code não aceitou este workspace como confiável e ignorou as permissões do "
                      "projeto (aceite a confiança do workspace: rode `claude` uma vez nesta pasta)")
    ignoradas = [a for a in r.alertas if re.search(r"Ignoring \d+ permissions?\.allow", a, re.IGNORECASE)]
    if ignoradas:
        causas.append(f"o Claude Code ignorou regras de permissions.allow do projeto ({ignoradas[0][:160]})")
    if r.erro:
        causas.append(f"não terminou: {r.erro}")
    elif r.estourou:
        causas.append("passou do tempo limite")
    elif r.final is None:
        causas.append(f"terminou (código {r.codigo}) sem a linha final de resultado do stream-json")
    else:
        if r.final.get("is_error") or r.final.get("subtype") not in (None, "success"):
            texto = f": {r.final['result']}" if r.final.get("result") else ""
            causas.append(f"terminou com erro ({r.final.get('subtype')}){texto}")
        negadas = r.final.get("permission_denials") or []
        if negadas:
            ferramentas = sorted({str(d.get("tool_name")) for d in negadas})
            causas.append(f"{len(negadas)} permissão(ões) negada(s): {', '.join(ferramentas)}")
    if r.codigo not in (0, None) and not r.estourou and not r.erro:
        causas.append(f"código {r.codigo}")
    return causas


def marcas_faltando(nome: str, e: estado.Estado) -> list[str]:
    """Passos que a fase de julgamento deveria ter marcado e não marcou."""
    if nome == "julgamento-revisao":
        esperadas = ["catalogar"]
        arq = e.pasta / "alvos.json"
        if arq.exists():
            try:
                alvos = json.loads(arq.read_text(encoding="utf-8")).get("alvos", [])
            except ValueError:
                alvos = []
            for a in alvos:
                if a.get("nome") == "main":
                    esperadas += [f"revisao:main:{ag}" for ag in ESPECIALISTAS_MAIN]
                elif a.get("nome"):
                    esperadas.append(f"revisao:{a['nome']}:guardiao-da-ideia")
    elif nome == "julgamento-final":
        esperadas = list(MARCAS_FINAIS)
    else:
        return []
    return [m for m in esperadas if not e.feito(m)]


def workspace_confiavel(raiz: Path, arquivo: Path | None = None) -> bool | None:
    """Só a marca `hasTrustDialogAccepted` da entrada do PRÓPRIO projeto em `~/.claude.json` — o
    `claude -p` exige essa entrada exata: uma pasta acima confiável não basta (medido na noite de 28/09:
    o ensaio dizia "sim" por causa de uma pasta-mãe e o `claude -p` ignorou as permissões). Nada mais
    desse arquivo é devolvido, impresso ou gravado. None = arquivo ausente ou ilegível."""
    arquivo = Path(arquivo) if arquivo else Path.home() / ".claude.json"
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    projetos = dados.get("projects") if isinstance(dados, dict) else None
    if not isinstance(projetos, dict):
        return False
    norm = lambda c: str(c).replace("\\", "/").rstrip("/").casefold()
    marcas = {norm(k): bool(isinstance(v, dict) and v.get("hasTrustDialogAccepted")) for k, v in projetos.items()}
    return bool(marcas.get(norm(Path(raiz))))


OCIOSO_MINIMO = 15 * 60  # teclado/mouse reais só com o PC parado há pelo menos isso


def segundos_ocioso() -> float | None:
    """Há quantos segundos não chega teclado nem mouse nesta sessão (GetLastInputInfo). Só consulta.
    None = não deu para saber (quem chama trata como "usuário ativo")."""
    try:
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        agora = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((agora - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except (AttributeError, OSError, ValueError):
        return None


def sessao_bloqueada() -> bool | None:
    """True se a área de trabalho de entrada não é a do usuário (tela bloqueada, UAC, sessão
    desconectada). Só consulta: não troca de área de trabalho nem abre janela. None = não deu para saber."""
    try:
        u = ctypes.windll.user32
        u.OpenInputDesktop.restype = ctypes.c_void_p
        u.OpenInputDesktop.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
        u.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                                                ctypes.POINTER(ctypes.c_uint)]
        u.CloseDesktop.argtypes = [ctypes.c_void_p]
        h = u.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not h:
            return True
        try:
            nome = ctypes.create_unicode_buffer(256)
            preciso = ctypes.c_uint(0)
            if not u.GetUserObjectInformationW(h, 2, nome, ctypes.sizeof(nome), ctypes.byref(preciso)):  # UOI_NAME
                return None
            return nome.value.casefold() != "default"
        finally:
            u.CloseDesktop(h)
    except (AttributeError, OSError, ValueError):
        return None


# ---------- plano da noite ----------

def localizar_claude() -> str | None:
    """O executável nativo do Claude Code (evita o `claude.cmd`, que passa pelo cmd.exe)."""
    if os.environ.get("MAW_AGENTE_CLAUDE"):
        return os.environ["MAW_AGENTE_CLAUDE"]
    candidatos = [Path(p) for p in (shutil.which("claude"),) if p]
    if os.environ.get("APPDATA"):
        candidatos.append(Path(os.environ["APPDATA"]) / "npm" / "claude.cmd")
    candidatos.append(Path.home() / ".local" / "bin" / "claude.exe")
    for c in candidatos:
        nativo = c.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if nativo.exists():
            return str(nativo)
    for c in candidatos:
        if c.exists():
            return str(c)
    return None


def _tem_sondas() -> bool:
    return any((config.PRIVADO / "sondas").glob("*.cpp"))


def _indisponivel(fase: str, efeito: str) -> str:
    return f"fase `{fase}` indisponível nesta versão do agente (subcomando ausente): {efeito}"


def planejar(op: Opcoes, disponiveis: set[str], hoje: date, python: str, claude: str | None,
             tem_sondas: bool) -> tuple[list[tuple[str, Etapa]], list[str]]:
    """Ações da noite, em ordem: ('rodar', etapa), ('fundo', etapa) começa sem esperar e
    ('esperar', etapa) junta a que foi para o fundo. Devolve também as limitações a declarar."""
    ma = [python, "-m", "maw_agent"]
    alvo = ["--alvo", op.so_alvo] if op.so_alvo else []
    acoes: list[tuple[str, Etapa]] = []
    lims: list[str] = []
    rodar = lambda et: acoes.append(("rodar", et))

    rodar(Etapa("iniciar", ma + ["sprint", "iniciar", "--retomar" if op.retomar else "--nova"], 0.5 * HORA))
    rodar(Etapa("preflight", ma + ["sprint", "preflight"], 0.25 * HORA))
    rodar(Etapa("preparar", ma + ["sprint", "preparar"], 1.5 * HORA))
    if "sondas" in disponiveis:
        rodar(Etapa("sondas", ma + ["sondas", "injetar"] + alvo, 0.25 * HORA, mecanica=True))
    elif tem_sondas:
        lims.append(_indisponivel("sondas", "as sondas C++ de privado/sondas não foram injetadas pela "
                                            "execução noturna"))
    revisao = None
    if not op.sem_julgamento:
        if claude:
            revisao = Etapa("julgamento-revisao", comando_claude(claude, PROMPT_REVISAO), 6 * HORA, guardar=40,
                            analisar=True)
            acoes.append(("fundo", revisao))  # catalogar e revisar enquanto compila
        else:
            lims.append("Claude Code não encontrado: as fases de julgamento (catálogo, revisão de código, "
                        "verificação adversarial e textos) não rodaram")
    rodar(Etapa("compilar", ma + ["sprint", "compilar"] + alvo, 4 * HORA, mecanica=True))
    rodar(Etapa("suite", ma + ["sprint", "suite"] + alvo, 3 * HORA, mexe_na_maw=True, mecanica=True))
    if op.calibrar or hoje.weekday() == 0:  # segunda-feira: antes da E2E, que ocupa o resto da janela
        if "calibrar" in disponiveis:
            rodar(Etapa("calibrar", ma + ["calibrar"], 4 * HORA, mexe_na_maw=True, mecanica=True))
        else:
            lims.append(_indisponivel("calibrar", "a taxa de detecção de defeitos plantados não foi medida"))
    if "e2e" in disponiveis:
        rodar(Etapa("e2e", ma + ["e2e"] + alvo, 5 * HORA, mexe_na_maw=True, entrada_real=True, mecanica=True))
    else:
        lims.append(_indisponivel("e2e", "os testes de ponta a ponta na interface (e os resultados da bancada) "
                                         "não rodaram"))
    if "servico" in disponiveis:
        rodar(Etapa("servico", ma + ["servico"] + alvo, 2 * HORA, mecanica=True))
    else:
        lims.append(_indisponivel("servico", "o serviço de IA não foi testado por HTTP"))
    if revisao is not None:
        acoes.append(("esperar", revisao))
        rodar(Etapa("julgamento-final", comando_claude(claude, PROMPT_FINAL), 6 * HORA, guardar=40,
                    analisar=True))
    return acoes, lims


def ler_subcomandos(saida: str) -> set[str] | None:
    try:
        return set(json.loads(saida[saida.index("{"):])["subcomandos"])
    except (ValueError, KeyError, TypeError):
        return None


def _json_da_saida(saida: str) -> dict | None:
    try:
        obj, _ = json.JSONDecoder().raw_decode(saida[saida.index("{"):])
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError, AttributeError):
        return None


def pasta_da_saida(saida: str) -> Path | None:
    """A pasta da sprint no JSON que `sprint iniciar` imprime."""
    obj = _json_da_saida(saida) or {}
    return Path(obj["pasta"]) if obj.get("pasta") else None


def _manter_acordado(ligar: bool) -> None:
    """Sem suspensão nem tela apagada enquanto a noite roda (a GUI precisa da área de trabalho)."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if ligar else 0))
    except (AttributeError, OSError):
        pass


def _energia() -> str:
    try:
        import psutil
        b = psutil.sensors_battery()
    except Exception:  # noqa: BLE001 — só informativo
        return "energia: desconhecida"
    if b is None:
        return "energia: sem bateria"
    return f"energia: bateria {b.percent:.0f}%, {'na tomada' if b.power_plugged else 'SEM TOMADA'}"


def _pegar_trava(trava: Path, inicio: datetime) -> int | None:
    """None se a trava é nossa; senão o pid da outra execução noturna viva."""
    if trava.exists():
        try:
            pid = int(json.loads(trava.read_text(encoding="utf-8"))["pid"])
            import psutil
            if psutil.pid_exists(pid) and psutil.Process(pid).name().lower().startswith("python"):
                return pid
        except Exception:  # noqa: BLE001 — trava ilegível ou processo sumiu entre as consultas: é velha
            pass
    sandbox.escrever_json(trava, {"pid": os.getpid(), "desde": inicio.isoformat(timespec="seconds")})
    return None


def _executar_protegido(executar, etapa, registro, env, cwd, timeout=None) -> Resultado:
    try:
        return executar(etapa, registro, env, cwd, timeout)
    except Exception as ex:  # noqa: BLE001 — um erro do agente numa etapa não derruba a noite
        registro.linha(f"erro do agente: {type(ex).__name__}: {ex}", etapa.nome)
        return Resultado(None, 0.0, False, f"{type(ex).__name__}: {ex}", "")


class _Fundo:
    """Uma etapa rodando numa thread enquanto a noite segue (o `claude -p` da revisão)."""

    def __init__(self, executar, etapa: Etapa, registro: Registro, env: dict, cwd: Path, timeout: float):
        self.etapa = etapa
        self.resultado: Resultado | None = None
        self._t = threading.Thread(target=self._rodar, args=(executar, etapa, registro, env, cwd, timeout),
                                   daemon=True, name=f"fundo-{etapa.nome}")
        self._t.start()

    def _rodar(self, executar, etapa, registro, env, cwd, timeout) -> None:
        self.resultado = _executar_protegido(executar, etapa, registro, env, cwd, timeout)

    def esperar(self) -> Resultado:
        self._t.join()
        return self.resultado

    def cancelar(self) -> None:
        """Pede a parada (a etapa mata a árvore de processos) e espera um pouco por ela."""
        self.etapa.parar.set()
        self._t.join(timeout=120)


class VolumeSaida:
    """Mudo e volume (0–1) do dispositivo de saída padrão, pelo IAudioEndpointVolume (pycaw)."""

    def __init__(self):
        from pycaw.pycaw import AudioUtilities
        self._v = AudioUtilities.GetSpeakers().EndpointVolume

    def ler(self) -> tuple[bool, float]:
        return bool(self._v.GetMute()), float(self._v.GetMasterVolumeLevelScalar())

    def ajustar(self, mudo: bool, nivel: float) -> None:
        # volume antes do mudo: nem ao ligar o som nem ao devolver o original sai um pico no nível errado
        self._v.SetMasterVolumeLevelScalar(float(nivel), None)
        self._v.SetMute(int(bool(mudo)), None)


def volume_padrao() -> VolumeSaida | None:
    try:
        return VolumeSaida()
    except Exception:  # noqa: BLE001 — sem saída de áudio ou sem COM: a E2E roda assim mesmo
        return None


class _SomDaNoite:
    """Durante a E2E, a saída padrão fica com som a NIVEL_SOM (mudo zera a captura por loopback); depois
    volta ao mudo e ao volume que o usuário tinha. Tudo registrado no log."""

    def __init__(self, fabrica: Callable[[], object], reg: Registro, limitar: Callable[[str], None]):
        self.fabrica, self.reg, self.limitar = fabrica, reg, limitar
        self.volume = None
        self.original: tuple[bool, float] | None = None

    def preparar(self) -> None:
        try:
            self.volume = self.fabrica()
            if self.volume is None:
                self.reg.linha("som: saída de áudio padrão indisponível; volume não conferido", "e2e")
                return
            mudo, nivel = self.volume.ler()
            self.original = (mudo, nivel)
            self.volume.ajustar(False, NIVEL_SOM)
            self.reg.linha(f"som: a saída padrão estava {'MUDA' if mudo else 'com som'} a {nivel:.0%}; agora com "
                           f"som a {NIVEL_SOM:.0%} até o fim da E2E", "e2e")
        except Exception as ex:  # noqa: BLE001 — sem volume, a E2E roda e os cenários de áudio dizem o que viram
            self.reg.linha(f"som: não foi possível conferir o volume ({type(ex).__name__}: {ex})", "e2e")

    def restaurar(self) -> None:
        if self.original is None or self.volume is None:
            return
        mudo, nivel = self.original
        self.original = None
        try:
            self.volume.ajustar(mudo, nivel)
            self.reg.linha(f"som: volume devolvido ({'mudo' if mudo else 'com som'} a {nivel:.0%})", "e2e")
        except Exception as ex:  # noqa: BLE001
            self.limitar(f"o volume da saída padrão não voltou ao original ({'mudo' if mudo else 'com som'} a "
                         f"{nivel:.0%}): {type(ex).__name__}: {ex}")


_AUTO = object()


def rodar(op: Opcoes, *, executar: Callable = executar_etapa, agora: Callable[[], datetime] = datetime.now,
          logs: Path = LOGS, python: str | None = None, claude=_AUTO, tem_sondas: bool | None = None,
          manter_acordado: Callable[[bool], None] = _manter_acordado, verificar: Callable | None = None,
          status: Callable[[], dict] | None = None, confianca: Callable[[], bool | None] | None = None,
          sessao: Callable[[], bool | None] = sessao_bloqueada, volume: Callable[[], object] | None = None,
          raiz: Path = config.RAIZ, ocioso: Callable[[], float | None] = segundos_ocioso) -> int:
    volume = volume or volume_padrao
    inicio = agora()
    reg = Registro(Path(logs) / f"{inicio:%Y-%m-%d}{'-ensaio' if op.ensaio else ''}.log")
    real = (not op.ensaio) and decidir_entrada_real(op.entrada_real, inicio, op.limite)
    env = ambiente_filhos(dict(os.environ), real)
    prazo = prazo_manha(inicio, op.limite)
    prazo_final = prazo_manha(inicio, op.prazo_final)
    py = python or sys.executable
    cl = localizar_claude() if claude is _AUTO else claude
    confianca = confianca or (lambda: workspace_confiavel(raiz))
    reg.linha(f"=== execução noturna{' (ENSAIO)' if op.ensaio else ''} começou em {inicio:%Y-%m-%d %H:%M} ===")
    reg.linha(f"opções: {json.dumps(asdict(op), ensure_ascii=False)}")
    reg.linha(f"teclado e mouse reais: {'LIGADOS' if real else 'desligados'} (MAW_AGENTE_ENTRADA_REAL="
              f"{env['MAW_AGENTE_ENTRADA_REAL']}); nada que abre a MAW começa depois de {prazo:%d/%m %H:%M}; "
              f"a noite termina até {prazo_final:%d/%m %H:%M}")
    reg.linha(f"{_energia()}; python: {py}; claude: {cl or 'NÃO ENCONTRADO'}")

    trava = Path(logs) / "rodando.json"
    if not op.ensaio:
        outro = _pegar_trava(trava, inicio)
        if outro is not None:
            reg.linha(f"outra execução noturna está rodando (pid {outro}): esta não começa")
            return _saida({"ok": False, "erro": f"outra execução noturna em andamento (pid {outro})",
                           "log": str(reg.caminho)}, False)
    try:
        r = _executar_protegido(executar, Etapa("subcomandos", [py, "-m", "maw_agent", "--help-json"], 300),
                                reg, env, raiz)
        disponiveis = ler_subcomandos(r.saida)
        if disponiveis is None:
            from .cli import _SUBCOMANDOS  # a própria CLI já carregou tudo o que existe
            disponiveis = set(_SUBCOMANDOS)
            reg.linha("--help-json ilegível: usando os subcomandos desta própria execução")
        conf = _seguro(confianca)
        # só um "não" certo desliga o julgamento: None (~/.claude.json ilegível por um instante, por ex.
        # gravado por uma sessão aberta) não tira o julgamento da noite
        sem_confianca = conf is False and not op.sem_julgamento
        if sem_confianca:
            op = replace(op, sem_julgamento=True)
            reg.linha("workspace sem confiança no Claude Code: rodando sem julgamento (o `claude -p` só "
                      "falharia)")
        acoes, lims = planejar(op, disponiveis, inicio.date(), py, cl,
                               _tem_sondas() if tem_sondas is None else tem_sondas)
        if sem_confianca:
            lims.insert(0, MSG_SEM_CONFIANCA)
        if op.ensaio:
            return _ensaio(reg, executar, env, raiz, cl, disponiveis, acoes, lims,
                           verificar or _preflight_sem_sprint, status or status_tarefa, confianca, sessao)
        reg.linha(f"confiança do workspace no Claude Code: {_sim_nao(conf)}"
                  + ("" if conf else " (o `claude -p` ignoraria as permissões do projeto)"))
        manter_acordado(True)
        try:
            noite = _Noite(op, reg, executar, env, raiz, py, lims, real, prazo,
                           prazo_final - timedelta(seconds=RESERVA_RELATORIO), agora, sessao, volume, ocioso,
                           sem_confianca=sem_confianca)
            return noite.tudo(acoes)
        finally:
            manter_acordado(False)
    finally:
        if not op.ensaio:
            try:
                sandbox.remover(trava)
            except OSError:
                pass


def _seguro(funcao: Callable[[], bool | None]) -> bool | None:
    try:
        return funcao()
    except Exception:  # noqa: BLE001 — só consulta: sem resposta vira "não sei"
        return None


def _sim_nao(valor: bool | None) -> str:
    return {True: "sim", False: "NÃO"}.get(valor, "não foi possível saber")


def _preflight_sem_sprint():
    from . import preflight
    return preflight.verificar_tudo()


MSG_CONFIANCA = "aceite a confiança do workspace: rode `claude` uma vez nesta pasta"
MSG_SEM_CONFIANCA = ("o workspace não é confiável para o Claude Code: o julgamento não rodou pelo claude -p "
                     "(rode `claude` uma vez na pasta e aceite; ou o julgamento é feito pela sessão interativa)")


def _ensaio(reg, executar, env, raiz, cl, disponiveis, acoes, lims, verificar, status, confianca, sessao) -> int:
    """Confere sem mexer em nada: subcomandos, pré-voo (só leitura), Claude Code, confiança do workspace,
    sessão e tarefa agendada."""
    pendencias: list[str] = []
    ausentes = sorted({"e2e", "servico", "calibrar", "sondas"} - disponiveis)
    reg.linha("subcomandos: " + ", ".join(sorted(disponiveis)))
    if ausentes:
        reg.linha("de outras trilhas, ainda ausentes: " + ", ".join(ausentes))
    if not disponiveis:
        pendencias.append("a CLI do agente não listou os subcomandos (--help-json)")
    bloqueios = []
    for v in verificar():
        falta = "" if v.ok else (" (BLOQUEIA)" if v.bloqueia else " (vira limitação)")
        reg.linha(f"{'ok   ' if v.ok else 'FALTA'} {v.nome}: {v.detalhe}{falta}", "preflight")
        if v.bloqueia and not v.ok:
            bloqueios.append(f"{v.nome}: {v.detalhe}")
    pendencias += [f"pré-voo bloqueia: {b}" for b in bloqueios]
    versao = None
    if cl:
        rv = _executar_protegido(executar, Etapa("claude-versao", [cl, "--version"], 120), reg, env, raiz)
        versao = rv.saida.strip() if rv.codigo == 0 else None
    if versao is None:
        pendencias.append("Claude Code não encontrado ou sem resposta a --version")
    conf = _seguro(confianca)
    if conf is not True:
        pendencias.append(MSG_CONFIANCA)
    reg.linha(f"confiança do workspace no Claude Code: {_sim_nao(conf)}"
              + ("" if conf is True else f" — {MSG_CONFIANCA}"))
    bloq = _seguro(sessao)
    if bloq:
        reg.linha("aviso: sessão bloqueada agora — de noite, com a sessão bloqueada, a E2E roda sem teclado e "
                  "mouse reais")
    else:
        reg.linha(f"sessão bloqueada: {_sim_nao(bloq)}")
    st = status()
    if not st.get("instalada"):
        pendencias.append("tarefa agendada não instalada (python -m maw_agent noturno instalar)")
    reg.linha(f"tarefa agendada: {json.dumps(st, ensure_ascii=False)}")
    reg.linha("plano desta noite (nada disto rodou no ensaio):")
    for acao, et in acoes:
        reg.linha(f"  {acao:8} {et.nome}: {' '.join(et.comando)}")
    for l in lims:
        reg.linha(f"  limitação que a noite declararia: {l}")
    ok = not pendencias
    for p in pendencias:
        reg.linha(f"PENDÊNCIA: {p}")
    reg.linha(f"=== ensaio terminou: {'ok' if ok else 'COM PENDÊNCIAS'} ===")
    return _saida({"ensaio": True, "ok": ok, "log": str(reg.caminho), "pendencias": pendencias,
                   "subcomandos_ausentes": ausentes, "preflight_bloqueios": bloqueios, "claude": versao,
                   "confianca": conf, "sessao_bloqueada": bloq, "tarefa": st,
                   "plano": [f"{a}:{e.nome}" for a, e in acoes], "limitacoes": lims}, ok)


def _estado(pasta: Path | None) -> estado.Estado | None:
    if pasta is None or not (Path(pasta) / "estado.json").exists():
        return None
    return estado.carregar(pasta)


def _em_andamento_pasta() -> Path | None:
    e = estado.em_andamento()
    return e.pasta if e else None


class _Noite:
    """A sequência da noite, com o que ela lembra no caminho (sprint, limitações, julgamentos, prazos)."""

    def __init__(self, op, reg, executar, env, raiz, py, lims, real, prazo, corte, agora, sessao, volume,
                 ocioso=segundos_ocioso, sem_confianca: bool = False):
        self.op, self.reg, self._executar, self.env, self.raiz = op, reg, executar, env, raiz
        self.sem_confianca = sem_confianca  # sem julgamento por falta de confiança, não pela opção
        self.ma = [py, "-m", "maw_agent"]
        self.real, self.prazo, self.corte, self.agora, self.sessao = real, prazo, corte, agora, sessao
        self.ocioso = ocioso
        self.som = _SomDaNoite(volume, reg, self.limitar)
        self.pasta: Path | None = None
        self.pendentes = list(lims)
        self.resumo: list[dict] = []
        self.fundos: dict[str, _Fundo] = {}
        self.falhas_julgamento: list[str] = []
        self.bloqueio_preflight: str | None = None
        self.pulados_por_prazo: list[str] = []

    # --- registro ---
    def limitar(self, texto: str) -> None:
        self.reg.linha(f"limitação declarada: {texto}")
        if self.pasta is not None:
            acrescentar_limitacao(self.pasta, texto)
        else:
            self.pendentes.append(texto)

    def falhar_no_estado(self, passo: str, motivo: str) -> None:
        e = _estado(self.pasta)
        if e is not None:  # vai para "erros do agente" no PDF
            e.falhar(passo, motivo)

    def pular(self, et: Etapa, motivo: str) -> None:
        self.reg.linha(f"não começa: {motivo}", et.nome)
        self.resumo.append({"etapa": et.nome, "pulada": motivo})

    def executar(self, et: Etapa, env: dict | None = None, timeout: float | None = None) -> Resultado:
        return _executar_protegido(self._executar, et, self.reg, env or self.env, self.raiz, timeout)

    def depois(self, et: Etapa, r: Resultado, corte: str | None = None) -> None:
        situacao = "ok" if r.ok else "FALHOU" + (" (tempo limite)" if r.estourou else "") + (
            f": {r.erro}" if r.erro else f" com código {r.codigo}")
        self.reg.linha(f"terminou em {_duracao(r.segundos)}: {situacao}", et.nome)
        self.resumo.append({"etapa": et.nome, "codigo": r.codigo, "minutos": round(r.segundos / 60, 1),
                            "estourou": r.estourou, "erro": r.erro})
        if r.estourou and corte == "janela":
            self.limitar(f"a etapa `{et.nome}` foi interrompida às {self.op.limite}, fim da janela da noite "
                         "(teclado e mouse reais não são usados de dia)")
        elif r.estourou and corte == "prazo":
            self.limitar(f"a etapa `{et.nome}` foi interrompida no prazo final da noite ({self.op.prazo_final}, "
                         f"com {_duracao(RESERVA_RELATORIO)} guardada para o relatório)")
        elif r.estourou:
            self.limitar(f"a etapa `{et.nome}` passou do tempo limite ({_duracao(et.timeout)}) e foi "
                         "interrompida pela execução noturna")
        elif r.erro and not et.nome.startswith("julgamento"):
            self.limitar(f"a etapa `{et.nome}` não rodou: {r.erro}")
        if et.nome.startswith("julgamento"):
            self.julgado(et, r)

    def julgado(self, et: Etapa, r: Resultado) -> None:
        """O `claude -p` terminou: a causa real de qualquer problema vira limitação e erro do agente."""
        causas = diagnosticar_julgamento(r)
        e = _estado(self.pasta)
        faltando = marcas_faltando(et.nome, e) if e is not None else []
        if faltando:
            causas.append("terminou sem marcar: " + ", ".join(faltando))
        if not causas:
            return
        texto = "; ".join(causas)
        self.falhas_julgamento.append(f"{et.nome}: {texto}")
        self.limitar(f"julgamento ({et.nome}): {texto}")
        self.falhar_no_estado(f"noturno:{et.nome}", texto)

    # --- a sequência ---
    def tudo(self, acoes) -> int:
        try:
            for acao, et in acoes:
                self.passo(acao, et)
        except Exception as ex:  # noqa: BLE001 — erro do próprio agente: registra, para o fundo e relata
            for linha in traceback.format_exc().splitlines():
                self.reg.linha(linha, "erro-do-agente")
            motivo = f"{type(ex).__name__}: {ex}"
            self.limitar(f"erro do agente na orquestração da noite ({motivo}): o resto das etapas não rodou e "
                         "o relatório saiu com o que havia")
            self.falhar_no_estado("noturno:orquestracao", motivo)
            for f in self.fundos.values():
                f.cancelar()
        finally:
            self.som.restaurar()  # o volume do usuário volta mesmo se a noite quebrar no meio
        for nome, f in list(self.fundos.items()):  # nada fica rodando para trás
            self.fundos.pop(nome)
            self.depois(f.etapa, f.esperar())
        if self.pulados_por_prazo:
            self.limitar(f"prazo final da noite alcançado ({self.op.prazo_final}, com "
                         f"{_duracao(RESERVA_RELATORIO)} guardada para o relatório): não rodaram "
                         + ", ".join(self.pulados_por_prazo))
        e = _estado(self.pasta)
        if e is not None and not e.feito("relatorio"):
            self.rede_de_seguranca(e)
        e = _estado(self.pasta)
        pdf = bool(e and e.feito("relatorio"))
        self.reg.linha(f"=== execução noturna terminou: {'PDF gerado' if pdf else 'SEM PDF'} ===")
        return _saida({"ok": pdf, "sprint": e.nome if e else None, "log": str(self.reg.caminho),
                       "etapas": self.resumo}, pdf)

    def passo(self, acao: str, et: Etapa) -> None:
        if acao == "esperar":
            f = self.fundos.pop(et.nome, None)
            if f is not None:
                self.reg.linha("esperando o julgamento em segundo plano terminar", et.nome)
                self.depois(et, f.esperar())
            return
        if et.mecanica and self.bloqueio_preflight:
            self.pular(et, f"o pré-voo bloqueou ({self.bloqueio_preflight})")
            return
        restante = (self.corte - self.agora()).total_seconds()
        if restante < 60:
            self.pulados_por_prazo.append(et.nome)
            self.pular(et, "prazo final da noite")
            return
        limite, corte = et.timeout, None
        if restante < limite:
            limite, corte = restante, "prazo"
        if et.mexe_na_maw and self.agora() >= self.prazo - timedelta(seconds=60):
            self.limitar(f"a etapa `{et.nome}` não começou: passou das {self.op.limite}, fim da janela da noite "
                         "(nada que abre a MAW começa de dia)")
            self.pular(et, "fora da janela da noite")
            return
        if et.mexe_na_maw:  # nada que abre a MAW ou toca som passa do limite da manhã, em nenhum modo
            janela = (self.prazo - self.agora()).total_seconds()
            if janela < limite:
                limite, corte = janela, "janela"
        env = self.env
        ajuda = ""
        if et.nome == "e2e":  # o que esta versão da fase E2E aceita (--limite, --oculto)
            ajuda = self.executar(Etapa("e2e-opcoes", self.ma + ["e2e", "--help"], 120)).saida
            env = dict(env, MAW_AGENTE_SOM="1", MAW_AGENTE_MICROFONE="1")  # som e microfone: só à noite
            if "--limite" in ajuda:  # a fase para de começar cenários no mesmo limite das etapas da MAW
                et = replace(et, comando=et.comando + ["--limite", self.op.limite])
        if et.entrada_real:
            # no desktop normal só com teclado/mouse reais E a sessão sabidamente desbloqueada; em todo
            # outro caso (entrada real desligada, sessão bloqueada ou em estado desconhecido), oculto
            bloqueada = _seguro(self.sessao) if self.real else None
            parado = _seguro(self.ocioso) if self.real and bloqueada is False else None
            usuario_ativo = parado is None or parado < OCIOSO_MINIMO
            if self.real and bloqueada is False and not usuario_ativo:
                self.reg.linha(f"PC parado há {parado / 60:.0f} min: {et.nome} com teclado/mouse reais", et.nome)
            else:
                env = dict(env, MAW_AGENTE_ENTRADA_REAL="0")
                if not self.real:
                    porque = "teclado/mouse reais desligados"
                elif bloqueada:
                    porque = f"sessão bloqueada às {self.agora():%H:%M}"
                elif bloqueada is False:
                    quanto = "desconhecida" if parado is None else f"{parado / 60:.0f} min"
                    porque = (f"usuário ativo no PC às {self.agora():%H:%M} (ociosidade {quanto}, mínimo "
                              f"{OCIOSO_MINIMO // 60} min)")
                else:
                    porque = f"sessão em estado desconhecido às {self.agora():%H:%M}"
                if "--oculto" in ajuda:
                    et = replace(et, comando=et.comando + ["--oculto"])
                    self.limitar(f"{porque}: {et.nome} no desktop oculto, sem teclado/mouse reais")
                elif self.real:
                    self.limitar(f"{porque}: {et.nome} sem teclado/mouse reais")
        if acao == "fundo":
            self.fundos[et.nome] = _Fundo(self._executar, et, self.reg, env, self.raiz, limite)
            return
        if et.nome == "e2e":
            self.som.preparar()
        try:
            r = self.executar(et, env, limite)
            self.depois(et, r, corte)
        finally:
            if et.nome == "e2e":
                self.som.restaurar()
        if et.nome == "iniciar":
            self.pasta = pasta_da_saida(r.saida) or _em_andamento_pasta()
            self.reg.linha(f"sprint: {self.pasta if self.pasta else 'NENHUMA (as limitações ficam só neste log)'}")
            if self.pasta is not None:
                for texto in self.pendentes:
                    acrescentar_limitacao(self.pasta, texto)
                self.pendentes.clear()
        elif et.nome == "preflight" and r.codigo not in (0, None):
            bloqueios = (_json_da_saida(r.saida) or {}).get("bloqueios") or []
            if bloqueios:
                self.bloqueio_preflight = "; ".join(str(b) for b in bloqueios)
                self.limitar(f"o pré-voo bloqueou ({self.bloqueio_preflight}): sondas, compilar, suíte, E2E, "
                             "serviço e calibração não rodaram")
        if r.estourou and et.mexe_na_maw:
            self.reg.linha("restaurando o %APPDATA%\\MAW depois da interrupção", et.nome)
            rest = Etapa("restaurar", self.ma + ["sprint", "iniciar", "--retomar"], 0.5 * HORA)
            self.depois(rest, self.executar(rest))

    def rede_de_seguranca(self, e: estado.Estado) -> None:
        """O julgamento não chegou ao PDF: o script consolida, encerra e gera o relatório com o que há."""
        if self.op.sem_julgamento:
            if not self.sem_confianca:  # sem confiança, a causa já foi declarada no começo (MSG_SEM_CONFIANCA)
                self.limitar("execução sem as fases de julgamento (-SemJulgamento): sem catálogo, revisão de "
                             "código, verificação adversarial nem textos; o relatório saiu só com as fases mecânicas")
        else:
            if self.falhas_julgamento:
                motivo = " | ".join(self.falhas_julgamento)
            elif "julgamento-final" in self.pulados_por_prazo:
                motivo = "o prazo final da noite chegou antes do julgamento final"
            else:
                motivo = "o `claude -p` terminou sem gerar o PDF"
            self.limitar(f"o julgamento da execução noturna não chegou ao relatório ({motivo}): o relatório saiu "
                         "pela rede de segurança do script, com o que já estava pronto")
        if (e.pasta / "bancada.json").exists() and not e.feito("bancada") and not bancada_desativada():
            self.limitar("resultados da bancada não enviados à página da bancada: ficaram em bancada.json e neste PDF")
        for fase in ("consolidar", "encerrar", "relatorio"):
            et = Etapa(fase, self.ma + ["sprint", fase], 0.5 * HORA)
            self.depois(et, self.executar(et))
        if self.op.sem_julgamento:
            return  # ensaio parcial: fica só no PC
        git = ["git", "-C", str(config.PRIVADO)]
        # todos rodam: "nada para commitar" não impede o push do que já estava commitado
        for nome, args in (("privado-add", ["add", "relatorios", "historico", "catalogo"]),
                           ("privado-commit", ["commit", "-m", f"sprint {e.numero:02d}: relatorio (rede de "
                                                                "seguranca da execucao noturna)"]),
                           ("privado-push", ["push"])):
            et = Etapa(nome, git + args, 0.25 * HORA)
            self.depois(et, self.executar(et))


# ---------- CLI ----------

def configurar(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="acao", required=True)
    i = sub.add_parser("instalar", help="registra a tarefa diária no Agendador do Windows")
    i.add_argument("--hora", type=hora_valida, default="22:00", help="HH:MM (padrão 22:00)")
    sub.add_parser("remover", help="apaga a tarefa do Agendador")
    sub.add_parser("status", help="mostra a tarefa registrada e a próxima execução")
    r = sub.add_parser("rodar", help="roda a sprint noturna (é o que a tarefa chama)")
    r.add_argument("--ensaio", action="store_true", help="só confere: pré-voo, subcomandos, claude e tarefa")
    r.add_argument("--calibrar", action="store_true", help="calibra mesmo fora da segunda-feira")
    r.add_argument("--so-alvo", type=_alvo_valido, help="compila e testa só este alvo")
    r.add_argument("--sem-julgamento", action="store_true", help="não chama o claude -p (relatório parcial)")
    r.add_argument("--retomar", action="store_true", help="retoma a sprint em andamento em vez de começar outra")
    r.add_argument("--limite", type=hora_valida, default=LIMITE_MANHA,
                   help="depois desta hora a E2E com teclado e mouse reais não começa")
    r.add_argument("--entrada-real", choices=["auto", "sim", "nao"], default="auto",
                   help="teclado e mouse reais: auto = só de noite")
    r.add_argument("--prazo-final", type=hora_valida, default=PRAZO_FINAL,
                   help="a noite termina antes desta hora, com o relatório (padrão 11:00)")
    b = sub.add_parser("bancada", help="lotes de resultados para a página da bancada (ArtifactData)")
    b.add_argument("--pasta", help="pasta da sprint (padrão: a em andamento)")
    lim = sub.add_parser("limitacao", help="declara uma limitação na sprint em andamento")
    lim.add_argument("texto")
    lim.add_argument("--pasta", help="pasta da sprint (padrão: a em andamento)")
    lim.add_argument("--origem", type=_origem_valida, default="noturno",
                     help="vai para limitacoes-<origem>.json (padrão: noturno)")


@registrar("noturno", "execução noturna: agenda a tarefa, roda a sprint e prepara a bancada", configurar)
def cmd_noturno(args: argparse.Namespace) -> int:
    if args.acao == "rodar":
        return rodar(opcoes_de(args))
    return {"instalar": _instalar, "remover": _remover, "status": _status, "bancada": _bancada,
            "limitacao": _limitacao}[args.acao](args)
