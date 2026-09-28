"""A única porta de escrita em disco do agente.

Recusa qualquer caminho dentro das pastas da MAW do usuário, resolvendo
caixa, barras, `..`, symlinks e junctions antes de comparar.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from . import config


class EscritaProibida(Exception):
    """Tentativa de escrever numa pasta da MAW do usuário."""


class RestauracaoFalhou(Exception):
    """A pasta restaurada não ficou idêntica ao backup."""


def _real(p: Path) -> Path:
    # os.path.realpath segue symlinks/junctions das partes que existem
    return Path(os.path.realpath(Path(p).absolute()))


def caminho_protegido(p: Path) -> bool:
    alvo = str(_real(p)).casefold().rstrip("\\/") + "\\"
    for prot in config.pastas_protegidas():
        base = str(_real(prot)).casefold().rstrip("\\/") + "\\"
        if alvo.startswith(base):
            return True
    return False


def garantir_escrita(p: Path) -> Path:
    if caminho_protegido(p):
        raise EscritaProibida(f"escrita recusada: {p} fica numa pasta da MAW do usuário")
    return Path(p)


def criar_pasta(p: Path) -> Path:
    garantir_escrita(p).mkdir(parents=True, exist_ok=True)
    return Path(p)


def escrever_bytes(p: Path, dados: bytes) -> Path:
    p = garantir_escrita(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(dados)
    os.replace(tmp, p)
    return p


def escrever_texto(p: Path, texto: str) -> Path:
    return escrever_bytes(p, texto.encode("utf-8"))


def escrever_json(p: Path, obj) -> Path:
    return escrever_texto(p, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def copiar(origem: Path, destino: Path) -> Path:
    garantir_escrita(destino)
    if Path(origem).is_dir():
        shutil.copytree(origem, destino, dirs_exist_ok=True)
    else:
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origem, destino)
    return Path(destino)


def remover(p: Path) -> None:
    garantir_escrita(p)
    p = Path(p)
    # Junctions: use os.rmdir to remove the link without touching target
    if os.path.isjunction(p):
        os.rmdir(p)
    elif p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()


def manifesto(pasta: Path) -> dict[str, str]:
    pasta = Path(pasta)
    if not pasta.exists():
        return {}
    out: dict[str, str] = {}
    for arq in sorted(pasta.rglob("*")):
        if arq.is_file():
            out[arq.relative_to(pasta).as_posix()] = hashlib.sha256(arq.read_bytes()).hexdigest()
    return out


def backup_pasta(origem: Path, destino_raiz: Path) -> Path:
    origem = Path(origem)
    destino = Path(destino_raiz) / time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while destino.exists():
        n += 1
        destino = Path(destino_raiz) / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    criar_pasta(destino)
    existia = origem.exists()
    if existia:
        copiar(origem, destino / "dados")
    escrever_json(destino / "manifesto.json",
                  {"origem": str(origem), "existia": existia, "arquivos": manifesto(origem)})
    return destino


def restaurar_pasta(backup: Path) -> None:
    info = json.loads((Path(backup) / "manifesto.json").read_text(encoding="utf-8"))
    origem = Path(info["origem"])
    try:
        if origem.exists():
            remover(origem)
        if info["existia"]:
            copiar(Path(backup) / "dados", origem)
    except EscritaProibida:
        raise
    except OSError as e:
        raise RestauracaoFalhou(f"erro ao restaurar {origem}: {e}") from e
    obtido = manifesto(origem)
    if obtido != info["arquivos"]:
        raise RestauracaoFalhou(f"{origem} não ficou idêntica ao backup {backup}")
