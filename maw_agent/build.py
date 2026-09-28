"""Compila um alvo com o MSBuild do VS 2022 Build Tools."""
from __future__ import annotations
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import redacao, sandbox

_RX_AVISO = re.compile(r"\b(?:warning|aviso)\s+([A-Z]{1,4}\d{3,5})\s*:", re.IGNORECASE)
_RX_ERRO = re.compile(r"\b(?:error|erro)\s+([A-Z]{1,4}\d{3,5})\s*:", re.IGNORECASE)
_RX_PREFIXO_NO = re.compile(r"^\s*\d+>")  # o "  1>" que o MSBuild põe nas linhas com /m
_RX_PROJETO_FINAL = re.compile(r"\s+\[[^\[\]]+\.(?:vcxproj|vcxitems|sln|proj|targets|props)\]\s*$",
                               re.IGNORECASE)  # o " [C:\...\App.vcxproj]" do fim da linha


@dataclass
class ResultadoBuild:
    alvo: str
    config: str
    ok: bool
    segundos: float
    avisos: list[str]
    erros: list[str]
    exe: str | None
    log: str

    def como_dict(self) -> dict:
        return asdict(self)


def localizar_msbuild() -> Path:
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if vswhere.exists():
        p = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                            "Microsoft.Component.MSBuild", "-find", r"MSBuild\**\Bin\MSBuild.exe"],
                           capture_output=True, text=True, timeout=60)
        for linha in p.stdout.splitlines():
            if linha.strip() and Path(linha.strip()).exists():
                return Path(linha.strip())
    raise FileNotFoundError("MSBuild não encontrado (VS 2022 Build Tools)")


def extrair_diagnosticos(texto: str) -> tuple[list[str], list[str]]:
    def coletar(rx: re.Pattern[str]) -> list[str]:
        vistos: dict[str, None] = {}
        for linha in texto.splitlines():
            if rx.search(linha):
                vistos.setdefault(_RX_PROJETO_FINAL.sub("", _RX_PREFIXO_NO.sub("", linha)).strip(), None)
        return list(vistos)
    return coletar(_RX_AVISO), coletar(_RX_ERRO)


def caminho_exe(worktree: Path, config: str) -> Path:
    return Path(worktree) / "Builds" / "VisualStudio2022" / "x64" / config / "App" / "MAW_APP.exe"


def _ler(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def compilar(alvo: str, worktree: Path, config: str, pasta_logs: Path,
             msbuild: Path | None = None, timeout: int = 5400) -> ResultadoBuild:
    """Compila com o MSBuild. O log vem do file logger (/flp) e a saída do console vai para um
    arquivo: com pipes, os nós do MSBuild herdariam a ponta e o processo "nunca terminaria".
    Os diagnósticos são extraídos do log já redigido."""
    projeto = Path(worktree) / "Builds" / "VisualStudio2022" / "MAW_APP_App.vcxproj"
    log = Path(pasta_logs) / f"build-{alvo}-{config}.log"
    if not projeto.exists():
        return ResultadoBuild(alvo, config, False, 0.0, [], [f"projeto não existe: {projeto}"], None, str(log))
    msbuild = msbuild or localizar_msbuild()
    sandbox.criar_pasta(Path(pasta_logs))
    sandbox.garantir_escrita(log)
    console = sandbox.garantir_escrita(log.with_name(log.name + ".console.txt"))
    log.unlink(missing_ok=True)  # um log de uma execução anterior não pode passar por este
    inicio = time.monotonic()
    extra = ""
    with open(console, "w", encoding="utf-8") as saida_console:
        try:
            p = subprocess.run([str(msbuild), str(projeto), f"/p:Configuration={config}", "/p:Platform=x64",
                                "/m", "/nologo", "/v:minimal", "/nodeReuse:false",
                                f"/flp:logfile={log};encoding=UTF-8;verbosity=normal"],
                               stdin=subprocess.DEVNULL, stdout=saida_console, stderr=subprocess.STDOUT,
                               timeout=timeout)
            codigo = p.returncode
        except subprocess.TimeoutExpired:
            codigo, extra = -1, f"\nTIMEOUT depois de {timeout}s"
    segundos = round(time.monotonic() - inicio, 1)
    texto_console = redacao.redigir(_ler(console))
    sandbox.remover(console)
    # sem log do file logger (ex.: o MSBuild recusou um argumento), o console é o que existe
    texto = redacao.redigir(_ler(log)) or texto_console
    sandbox.escrever_texto(log, texto + extra)
    avisos, erros = extrair_diagnosticos(texto)
    exe = caminho_exe(worktree, config)
    ok = codigo == 0 and exe.exists()
    if ok:
        # compilação incremental sem religar não toca o exe; o build desta sprint o confirmou,
        # então ele passa a contar como "desta compilação" (C2: suíte só roda sobre binário novo)
        sandbox.garantir_escrita(exe)
        os.utime(exe, None)
    if codigo != 0 and not erros:
        erros = [f"MSBuild saiu com código {codigo} sem erro reconhecível; ver {log.name}"]
    return ResultadoBuild(alvo, config, ok, segundos, avisos, erros, str(exe) if ok else None, str(log))
