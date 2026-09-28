"""`python -m maw_agent sondas <acao>`: sondas C++ do agente nas worktrees dos alvos.

- `injetar [--alvo X]`: sincroniza as sondas do privado com `Source/Tests/Sondas/` e o `.vcxproj`
  (só Debug) de cada alvo da sprint em andamento (sem sprint: de cada worktree em work/alvos).
  Roda depois do `preparar` e antes do `compilar`.
- `listar`: cada sonda, os nomes de exibição, o que ela exige do alvo e os itens do catálogo que cobre.
"""
from __future__ import annotations
import argparse
import json

from . import config, estado, fases, sandbox, sondas
from .cli import registrar


def _cfg(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="acao", required=True)
    sp = sub.add_parser("injetar", help="copia as sondas para a worktree de cada alvo (build Debug)")
    sp.add_argument("--alvo")
    sub.add_parser("listar", help="lista as sondas e os itens do catálogo que cobrem")


def _saida(obj: dict, ok: bool = True) -> int:
    # stdout redirecionado no Windows é cp1252: o que ele não codifica sai como \uXXXX
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(obj, ensure_ascii=True, indent=2))
    return 0 if ok else 1


def _nomes_dos_alvos(e: estado.Estado | None, so: str | None) -> list[str]:
    if e is not None and (e.pasta / "alvos.json").exists():
        return [a["nome"] for a in fases._alvos(e, so) if not a["compartilha_com"]]
    if so:
        return [so]
    raiz = config.ALVOS_DIR
    return sorted(p.name for p in raiz.iterdir() if (p / sondas.PROJETO).is_file()) if raiz.is_dir() else []


def _injetar(args) -> int:
    e = estado.em_andamento(config.RELATORIOS)
    arquivos = sondas.listar()
    por_alvo: dict[str, dict] = {}
    avisos: list[str] = []
    ok = True
    for nome in _nomes_dos_alvos(e, args.alvo):
        wt = config.ALVOS_DIR / nome
        try:
            r = sondas.injetar(wt, arquivos)
        except (OSError, ValueError) as ex:  # FileNotFoundError é OSError
            por_alvo[nome] = {"erro": str(ex)}
            avisos.append(f"sondas não injetadas em {nome}: {ex}")
            ok = False
            continue
        r["nomes"] = [n for a in arquivos if a.name in r["injetadas"] for n in sondas.nomes(a)]
        por_alvo[nome] = r
        for arq, motivo in r["puladas"].items():
            avisos.append(f"sonda {arq} pulada em {nome}: {motivo}")
        if e is not None and r["alterado"] and e.feito(f"compilar:{nome}:Debug"):
            avisos.append(f"{nome}: o build Debug desta sprint já foi feito; as sondas só entram na próxima compilação")
    if e is not None:
        anterior = fases._ler_json(e.pasta / "sondas.json", {})
        sandbox.escrever_json(e.pasta / "sondas.json", {**anterior, **por_alvo})
        fases._acrescentar_limitacoes(e, "sondas", avisos)
    return _saida({"sondas": [a.name for a in arquivos], "alvos": por_alvo, "avisos": avisos}, ok)


def _listar(args) -> int:
    mapa = sondas.carregar_mapa()
    lista = []
    vistos: set[str] = set()
    for a in sondas.listar():
        ns = sondas.nomes(a)
        vistos.update(ns)
        lista.append({"arquivo": a.name, "nomes": ns,
                      "requisitos": [{"arquivo": arq, "identificadores": ids} for arq, ids in sondas.requisitos(a)],
                      "itens": {n: mapa.get(n, []) for n in ns}})
    sem_sonda = sorted(n for n in mapa if n not in vistos)
    return _saida({"sondas": lista, "mapa_sem_sonda": sem_sonda}, not sem_sonda)


@registrar("sondas", "sondas C++ do agente: injetar nas worktrees dos alvos ou listar", _cfg)
def cmd_sondas(args: argparse.Namespace) -> int:
    return {"injetar": _injetar, "listar": _listar}[args.acao](args)
