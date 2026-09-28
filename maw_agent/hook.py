"""Pre-commit dos dois repositórios: nada de resultado, segredo ou conhecimento interno no público."""
from __future__ import annotations
import argparse
import re
import subprocess
import sys
from pathlib import Path

from . import config, redacao
from .cli import registrar

PREFIXOS_PUBLICO = ("privado/", "work/", ".venv/")
PARTES_PROIBIDAS_PUBLICO = ("relatorios/", "evidencias/")
PARTES_PROIBIDAS_PRIVADO = ("/evidencias/", "appdata-backup/", "work/")
NOMES_PROIBIDOS = ("gemini_api_key.txt", "MAW.settings", "estado.json")
EXT_PROIBIDAS_PUBLICO = (".pdf", ".wav", ".flac", ".ogg", ".mp3", ".maw", ".settings")


def carregar_termos(caminho: Path) -> list[str] | None:
    if not Path(caminho).exists():
        return None
    termos = []
    for linha in Path(caminho).read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#"):
            termos.append(linha)
    return termos


def _achar_termo(texto: str, termos: list[str]) -> str | None:
    for t in termos:
        if t.startswith("re:"):
            m = re.search(t[3:], texto)
            if m:
                return m.group(0)
        elif t in texto:
            return t
    return None


def verificar(arquivos: dict[str, str], repo: str, termos: list[str] | None) -> list[str]:
    violacoes: list[str] = []
    if repo == "publico" and termos is None:
        return ["lista de termos proibidos ausente (privado/catalogo/termos-proibidos.txt): commit recusado"]
    for caminho, conteudo in sorted(arquivos.items()):
        c = caminho.replace("\\", "/")
        nome = c.rsplit("/", 1)[-1]
        if nome in NOMES_PROIBIDOS:
            violacoes.append(f"{c}: arquivo que nunca vai para repositório")
            continue
        if repo == "publico":
            if c.startswith(PREFIXOS_PUBLICO) or any(p in c for p in PARTES_PROIBIDAS_PUBLICO) \
                    or c.lower().endswith(EXT_PROIBIDAS_PUBLICO):
                violacoes.append(f"{c}: caminho proibido no repositório público")
                continue
        else:
            if any(p in "/" + c for p in PARTES_PROIBIDAS_PRIVADO):
                violacoes.append(f"{c}: evidência bruta ou backup não vai nem para o privado")
                continue
        tipos = redacao.segredos_em(conteudo)
        if tipos:
            violacoes.append(f"{c}: contém segredo ({', '.join(tipos)})")
        if repo == "publico":
            termo = _achar_termo(c + "\n" + conteudo, termos or [])
            if termo:
                violacoes.append(f"{c}: contém termo interno da MAW ({termo})")
    return violacoes


def _arquivos_em_stage(raiz: Path) -> dict[str, str]:
    nomes = subprocess.run(["git", "-C", str(raiz), "diff", "--cached", "--name-only", "-z",
                            "--diff-filter=ACMR"], capture_output=True, check=True).stdout
    out: dict[str, str] = {}
    for nome in filter(None, nomes.decode("utf-8").split("\0")):
        dados = subprocess.run(["git", "-C", str(raiz), "show", f":{nome}"],
                               capture_output=True, check=True).stdout
        try:
            out[nome] = dados.decode("utf-8")
        except UnicodeDecodeError:
            out[nome] = ""
    return out


def _cfg_hook(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", choices=["publico", "privado"], required=True)


@registrar("hook-precommit", "verifica o que está em stage antes do commit", _cfg_hook)
def executar_hook(args: argparse.Namespace) -> int:
    raiz = config.RAIZ if args.repo == "publico" else config.PRIVADO
    termos = carregar_termos(config.TERMOS_PROIBIDOS) if args.repo == "publico" else None
    violacoes = verificar(_arquivos_em_stage(raiz), args.repo, termos)
    for v in violacoes:
        print(f"[pre-commit] {v}", file=sys.stderr)
    return 1 if violacoes else 0


@registrar("instalar-hooks", "liga os hooks de pre-commit nos dois repositórios", lambda p: None)
def instalar_hooks(args: argparse.Namespace) -> int:
    subprocess.run(["git", "-C", str(config.RAIZ), "config", "core.hooksPath", "ferramentas/hooks"], check=True)
    if (config.PRIVADO / ".git").exists():
        subprocess.run(["git", "-C", str(config.PRIVADO), "config", "core.hooksPath", "ferramentas/hooks"], check=True)
    print('{"ok": true}')
    return 0
