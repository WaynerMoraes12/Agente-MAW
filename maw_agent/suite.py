"""Roda `--run-tests` / `--benchmark` do executável e interpreta o texto.

O exe é /SUBSYSTEM:Windows: subprocess.run espera o processo, e o resultado só
é "passou" quando o código de saída, a linha RESULTADO e os totais concordam.
O relatório sai no stdout, no fim; enquanto a suíte roda, a MAW escreve no stderr
"rodando: <área> / <bloco>" — a última dessas linhas diz onde um processo que caiu estava.
"""
from __future__ import annotations
import ctypes
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

from . import redacao

# herdados pelo processo filho: nenhum diálogo "o programa parou" ou "insira um disco" trava a madrugada
_SEM_DIALOGOS = 0x0001 | 0x0002  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX

_RX_BLOCO = re.compile(r"^\[(ok|FALHOU)\]\s+(.+?)\s{2,}->\s{2,}(.+?)\s+\((\d+) ok, (\d+) falha\(s\)\)\s*$")
_RX_NUM = {
    "blocos": re.compile(r"^Blocos de teste \.+ (\d+)"),
    "ok": re.compile(r"^Verificacoes que deram ok (\d+)"),
    "falhas": re.compile(r"^Verificacoes que falharam (\d+)"),
    "tempo": re.compile(r"^Tempo total \.+ (\d+) ms"),
}
_PROGRESSO = "rodando: "


@dataclass
class BlocoSuite:
    nome: str
    sub: str
    ok: int
    falhas: int


@dataclass
class ResultadoSuite:
    exit_code: int | None
    segundos: float
    blocos: list[BlocoSuite] = field(default_factory=list)
    total_ok: int | None = None
    total_falhas: int | None = None
    blocos_declarados: int | None = None
    tempo_ms: int | None = None
    declarado: str = "AUSENTE"
    detalhes: list[str] = field(default_factory=list)
    preambulo: list[str] = field(default_factory=list)
    incoerencias: list[str] = field(default_factory=list)
    bruto: str = ""  # o stdout (o relatório)
    stderr: str = ""  # o progresso ("rodando: ...") e o que mais a MAW escrever no stderr
    ultimo_bloco: str | None = None  # "<área> / <bloco>" da última linha de progresso

    @property
    def passou(self) -> bool:
        return (self.exit_code == 0 and self.declarado == "PASSOU" and self.total_falhas == 0
                and not self.incoerencias)

    def como_dict(self) -> dict:
        d = asdict(self)
        d["passou"] = self.passou
        return d


def _linhas(texto: str) -> list[str]:
    return [l.rstrip() for l in texto.replace("\r\n", "\n").replace("\r", "\n").split("\n") if l.strip()]


def ultimo_bloco(stderr: str) -> str | None:
    """O `<área> / <bloco>` da última linha "rodando: " do stderr (None se não houver nenhuma)."""
    blocos = [l.strip()[len(_PROGRESSO):].strip() for l in _linhas(stderr) if l.strip().startswith(_PROGRESSO)]
    return blocos[-1] if blocos else None


def interpretar_suite(texto: str, exit_code: int | None, segundos: float, stderr: str = "") -> ResultadoSuite:
    """`texto`: só o stdout (o relatório). `stderr` fica à parte e dá o último bloco que começou."""
    r = ResultadoSuite(exit_code=exit_code, segundos=segundos, bruto=texto, stderr=stderr,
                       ultimo_bloco=ultimo_bloco(stderr))
    no_detalhe = False
    viu_cabecalho = False
    for l in _linhas(texto):
        s = l.strip()
        if "suite de testes automatizados" in s:
            viu_cabecalho = True
            continue
        if not viu_cabecalho:
            r.preambulo.append(s)
            continue
        if set(s) <= {"="}:
            continue
        m = _RX_BLOCO.match(s)
        if m:
            r.blocos.append(BlocoSuite(m.group(2), m.group(3), int(m.group(4)), int(m.group(5))))
            continue
        casou = False
        for chave, rx in _RX_NUM.items():
            mm = rx.match(s)
            if mm:
                casou = True
                v = int(mm.group(1))
                if chave == "blocos":
                    r.blocos_declarados = v
                elif chave == "ok":
                    r.total_ok = v
                elif chave == "falhas":
                    r.total_falhas = v
                else:
                    r.tempo_ms = v
        if casou:
            continue
        if s.startswith("RESULTADO: TUDO PASSOU"):
            r.declarado = "PASSOU"
        elif s.startswith("RESULTADO: FALHOU"):
            r.declarado = "FALHOU"
        elif s.startswith("Detalhe das falhas"):
            no_detalhe = True
        elif no_detalhe:
            r.detalhes.append(s)
    if not viu_cabecalho and r.ultimo_bloco is None:
        # com progresso no stderr os testes rodaram: o relatório (e o cabeçalho) só não chegou ao fim,
        # e os "totais ausentes" abaixo já dizem isso
        r.incoerencias.append("cabeçalho da suíte ausente: o executável não rodou os testes")
    if r.total_ok is None or r.total_falhas is None or r.blocos_declarados is None:
        r.incoerencias.append("totais ausentes: a execução pode ter morrido antes do fim")
    else:
        if sum(b.ok for b in r.blocos) != r.total_ok or sum(b.falhas for b in r.blocos) != r.total_falhas:
            r.incoerencias.append("a soma dos blocos não bate com os totais declarados")
        if len(r.blocos) != r.blocos_declarados:
            r.incoerencias.append("o número de blocos listados não bate com o declarado")
    if r.declarado == "PASSOU" and exit_code not in (0, None):
        r.incoerencias.append(f"relatório diz TUDO PASSOU mas o código de saída foi {exit_code}")
    if r.declarado == "FALHOU" and exit_code == 0:
        r.incoerencias.append("relatório diz FALHOU mas o código de saída foi 0")
    return r


@dataclass
class LinhaBenchmark:
    trilhas: int
    medio_ms: float
    max_ms: float
    carga_media: float
    carga_max: float


def interpretar_benchmark(texto: str) -> tuple[list[LinhaBenchmark], list[str]]:
    linhas: list[LinhaBenchmark] = []
    cab: list[str] = []
    for l in _linhas(texto):
        partes = [p.strip() for p in l.split("|")]
        if len(partes) == 5 and partes[0].isdigit():
            linhas.append(LinhaBenchmark(int(partes[0]), *(float(p.replace(",", ".")) for p in partes[1:])))
        elif not linhas and "trilhas" not in l and not set(l.strip()) <= set("-+"):
            cab.append(l.strip())
    return linhas, cab


def maw_aberta() -> bool:
    """Há um MAW_APP.exe rodando (a MAW do usuário)?"""
    return any((p.info.get("name") or "").lower() == "maw_app.exe" for p in psutil.process_iter(["name"]))


def ambiente_com_ffmpeg(base: dict[str, str], ffmpeg_bin: Path) -> dict[str, str]:
    """Cópia de `base` com a pasta do ffmpeg portátil na frente do PATH, quando ela existe."""
    env = dict(base)
    if Path(ffmpeg_bin).is_dir():
        env["PATH"] = os.pathsep.join([str(ffmpeg_bin), env.get("PATH", "")])
    return env


def _texto(dados) -> str:
    if isinstance(dados, bytes):
        return dados.decode("utf-8", errors="replace")
    return dados or ""


def executar(exe: Path, argumento: str, cwd: Path, timeout: int,
             env: dict[str, str] | None = None) -> tuple[int | None, str, str, float]:
    """(código de saída — None no tempo esgotado —, stdout, stderr, segundos), as duas saídas redigidas."""
    k32 = ctypes.windll.kernel32
    modo_anterior = k32.GetErrorMode()
    k32.SetErrorMode(modo_anterior | _SEM_DIALOGOS)
    inicio = time.monotonic()
    try:
        p = subprocess.run([str(exe), argumento], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
        codigo, saida, erro = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        erro = _texto(e.stderr)
        codigo, saida = None, _texto(e.stdout)
        erro = (erro + "\n" if erro else "") + f"TIMEOUT depois de {timeout}s"
    finally:
        k32.SetErrorMode(modo_anterior)
    return codigo, redacao.redigir(saida), redacao.redigir(erro), round(time.monotonic() - inicio, 1)


def rodar_suite(exe: Path, cwd: Path, timeout: int = 1800, env: dict[str, str] | None = None) -> ResultadoSuite:
    codigo, saida, erro, seg = executar(exe, "--run-tests", cwd, timeout, env)
    return interpretar_suite(saida, codigo, seg, stderr=erro)


def rodar_benchmark(exe: Path, cwd: Path, timeout: int = 1800, env: dict[str, str] | None = None) -> dict:
    # a tabela sai no stdout; o stderr fica à parte e não entra no cabeçalho da tabela
    codigo, saida, erro, seg = executar(exe, "--benchmark", cwd, timeout, env)
    linhas, cab = interpretar_benchmark(saida)
    return {"exit_code": codigo, "segundos": seg, "linhas": [asdict(l) for l in linhas],
            "cabecalho": cab, "bruto": saida, "stderr": erro}
