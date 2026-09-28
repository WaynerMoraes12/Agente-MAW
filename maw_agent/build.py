"""Compila um alvo com o MSBuild do VS 2022 Build Tools."""
from __future__ import annotations
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import redacao, sandbox

_RX_AVISO = re.compile(r"\b(?:warning|aviso)\s+([A-Z]{1,4}\d{3,5})\s*:", re.IGNORECASE)
_RX_ERRO = re.compile(r"\b(?:error|erro)\s+([A-Z]{1,4}\d{3,5})\s*:", re.IGNORECASE)


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
                           capture_output=True, text=True)
        for linha in p.stdout.splitlines():
            if linha.strip() and Path(linha.strip()).exists():
                return Path(linha.strip())
    raise FileNotFoundError("MSBuild não encontrado (VS 2022 Build Tools)")


def extrair_diagnosticos(texto: str) -> tuple[list[str], list[str]]:
    def coletar(rx: re.Pattern[str]) -> list[str]:
        vistos: dict[str, None] = {}
        for linha in texto.splitlines():
            if rx.search(linha):
                vistos.setdefault(linha.strip().split(" [")[0], None)
        return list(vistos)
    return coletar(_RX_AVISO), coletar(_RX_ERRO)


def caminho_exe(worktree: Path, config: str) -> Path:
    return Path(worktree) / "Builds" / "VisualStudio2022" / "x64" / config / "App" / "MAW_APP.exe"


def compilar(alvo: str, worktree: Path, config: str, pasta_logs: Path,
             msbuild: Path | None = None, timeout: int = 5400) -> ResultadoBuild:
    projeto = Path(worktree) / "Builds" / "VisualStudio2022" / "MAW_APP_App.vcxproj"
    log = Path(pasta_logs) / f"build-{alvo}-{config}.log"
    if not projeto.exists():
        return ResultadoBuild(alvo, config, False, 0.0, [], [f"projeto não existe: {projeto}"], None, str(log))
    msbuild = msbuild or localizar_msbuild()
    inicio = time.monotonic()
    try:
        p = subprocess.run([str(msbuild), str(projeto), f"/p:Configuration={config}", "/p:Platform=x64",
                            "/m", "/nologo", "/v:minimal"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        saida, codigo = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        saida, codigo = f"{e.stdout or ''}\nTIMEOUT depois de {timeout}s", -1
    segundos = round(time.monotonic() - inicio, 1)
    sandbox.escrever_texto(log, redacao.redigir(saida))
    avisos, erros = extrair_diagnosticos(saida)
    exe = caminho_exe(worktree, config)
    ok = codigo == 0 and exe.exists()
    if codigo != 0 and not erros:
        erros = [f"MSBuild saiu com código {codigo} sem erro reconhecível; ver {log.name}"]
    return ResultadoBuild(alvo, config, ok, segundos, avisos, erros, str(exe) if ok else None, str(log))
