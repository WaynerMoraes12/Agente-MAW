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


# Esperas entre as 5 tentativas de uma operação que o Windows recusa com PermissionError
# (antivírus, indexador ou a própria MAW segurando o arquivo por um instante).
ESPERAS = (0.2, 0.5, 1.0, 2.0)


def _com_tentativas(operacao):
    for espera in ESPERAS:
        try:
            return operacao()
        except PermissionError:
            time.sleep(espera)
    return operacao()


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
    try:
        _com_tentativas(lambda: os.replace(tmp, p))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
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


def _hash(arq: Path) -> str:
    return hashlib.sha256(Path(arq).read_bytes()).hexdigest()


def manifesto(pasta: Path) -> dict[str, str]:
    pasta = Path(pasta)
    if not pasta.exists():
        return {}
    out: dict[str, str] = {}
    for arq in sorted(pasta.rglob("*")):
        if arq.is_file():
            out[arq.relative_to(pasta).as_posix()] = _hash(arq)
    return out


def backup_pasta(origem: Path, destino_raiz: Path) -> Path:
    origem = Path(origem)
    destino = Path(destino_raiz) / time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while destino.exists():
        n += 1
        destino = Path(destino_raiz) / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    criar_pasta(destino)
    try:
        existia = origem.exists()
        if existia:
            copiar(origem, destino / "dados")
        escrever_json(destino / "manifesto.json",
                      {"origem": str(origem), "existia": existia, "arquivos": manifesto(origem)})
    except BaseException:
        remover(destino)  # backup pela metade não serve e pode conter segredos
        raise
    return destino


def _restaurar_arquivo(de: Path, para: Path) -> None:
    """Copia `de` sobre `para` por um temporário na mesma pasta e troca com os.replace."""
    garantir_escrita(para)
    para.parent.mkdir(parents=True, exist_ok=True)
    tmp = para.with_name(para.name + ".restaurando.tmp")
    shutil.copy2(de, tmp)
    try:
        _com_tentativas(lambda: os.replace(tmp, para))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _apagar(p: Path) -> None:
    garantir_escrita(p)
    if p.is_dir() and not p.is_symlink() and not os.path.isjunction(p):
        _com_tentativas(lambda: os.rmdir(p))
    else:
        _com_tentativas(lambda: p.unlink())


def restaurar_pasta(backup: Path) -> None:
    """Devolve a pasta de origem ao estado do backup sem nunca apagá-la inteira antes:
    regrava por cima só os arquivos que mudaram, depois apaga o que sobrou. Cada operação tem
    até 5 tentativas contra PermissionError; qualquer falha final vira RestauracaoFalhou, e o
    resultado é conferido contra o manifesto do backup."""
    info = json.loads((Path(backup) / "manifesto.json").read_text(encoding="utf-8"))
    origem = Path(info["origem"])
    esperado: dict[str, str] = info["arquivos"] if info["existia"] else {}
    dados = Path(backup) / "dados"
    try:
        garantir_escrita(origem)
        if info["existia"]:
            origem.mkdir(parents=True, exist_ok=True)
            for rel, h in esperado.items():
                destino = origem / rel
                if destino.is_file() and _hash(destino) == h:
                    continue
                if destino.is_dir():
                    remover(destino)
                _restaurar_arquivo(dados / rel, destino)
        if origem.exists():
            # o que sobrou: arquivos fora do manifesto, depois pastas vazias (as mais fundas primeiro)
            for arq in sorted(origem.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                rel = arq.relative_to(origem).as_posix()
                if arq.is_file() or arq.is_symlink():
                    if rel not in esperado:
                        _apagar(arq)
                elif arq.is_dir() and not any(arq.iterdir()) and not (dados / rel).is_dir() and not any(
                        k.startswith(rel + "/") for k in esperado):
                    _apagar(arq)
            if not info["existia"]:
                _apagar(origem)
    except EscritaProibida:
        raise
    except OSError as e:
        raise RestauracaoFalhou(f"erro ao restaurar {origem}: {e}") from e
    obtido = manifesto(origem)
    if obtido != esperado:
        raise RestauracaoFalhou(f"{origem} não ficou idêntica ao backup {backup}")


def descartar_backup(backup: Path) -> None:
    """Apaga um backup (ele pode conter segredos), com as mesmas tentativas contra PermissionError."""
    _com_tentativas(lambda: remover(backup))


def restaurar_e_descartar(backup: Path) -> None:
    """Restaura, confere o manifesto e só então apaga o backup."""
    restaurar_pasta(backup)
    descartar_backup(backup)
