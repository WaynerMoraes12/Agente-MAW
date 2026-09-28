"""CLI: python -m maw_agent <subcomando> [...]. Toda saída de dados é JSON em stdout."""
from __future__ import annotations
import argparse
import json
import sys
from typing import Callable

_SUBCOMANDOS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None], Callable[[argparse.Namespace], int]]] = {}


def registrar(nome: str, ajuda: str, configurar: Callable[[argparse.ArgumentParser], None]):
    """Decorator: registra `executar(args) -> int` como subcomando `nome`."""
    def deco(executar: Callable[[argparse.Namespace], int]):
        _SUBCOMANDOS[nome] = (ajuda, configurar, executar)
        return executar
    return deco


def _carregar_modulos() -> None:
    # importar registra os subcomandos; toda fase_*.py é descoberta automaticamente
    import importlib
    import pkgutil

    import maw_agent

    nomes = ["hook", "fases"]
    for info in pkgutil.iter_modules(maw_agent.__path__):
        if info.name.startswith("fase_") and info.name not in nomes:
            nomes.append(info.name)

    for mod in nomes:
        try:
            importlib.import_module(f"maw_agent.{mod}")
        except ModuleNotFoundError as e:
            # só engole a ausência do próprio módulo opcional; qualquer outro
            # ModuleNotFoundError (ex.: uma dependência interna faltando) sobe.
            if e.name != f"maw_agent.{mod}":
                raise


def main(argv: list[str] | None = None) -> int:
    _carregar_modulos()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--help-json"]:
        print(json.dumps({"subcomandos": {k: v[0] for k, v in sorted(_SUBCOMANDOS.items())}},
                         ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(prog="maw_agent")
    sub = parser.add_subparsers(dest="comando", required=True)
    for nome, (ajuda, configurar, _) in sorted(_SUBCOMANDOS.items()):
        configurar(sub.add_parser(nome, help=ajuda))
    args = parser.parse_args(argv)
    return _SUBCOMANDOS[args.comando][2](args)
