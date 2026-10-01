"""Bancada do usuário: resultados do agente por código, em `bancada.json` na pasta da sprint.

Cada código (item de `work/bancada/testes/<código>.json`) recebe um estado agregado —
`passou`/`pulei`/`problema` — mesmo quando mais de um cenário ou mais de um alvo contribui
para ele: registrar duas vezes o mesmo código nunca perde um `problema` já visto.
"""
from __future__ import annotations
import json
from pathlib import Path

from . import sandbox

# pior primeiro: um "problema" nunca é apagado por um "passou" que venha depois (ou antes)
_ORDEM_ESTADO = {"problema": 2, "pulei": 1, "passou": 0}
ESTADOS = tuple(sorted(_ORDEM_ESTADO, key=_ORDEM_ESTADO.get, reverse=True))


def carregar(pasta_sprint: Path) -> dict:
    p = Path(pasta_sprint) / "bancada.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _juntar_um(atual: dict | None, estado: str, nota: str, alvo: str) -> dict:
    """Uma contribuição a mais num código: o pior estado vence (e leva o alvo), as notas se juntam."""
    if estado not in _ORDEM_ESTADO:
        raise ValueError(f"estado inválido: {estado!r} (use {', '.join(ESTADOS)})")
    if atual is None:
        return {"estado": estado, "nota": nota, "alvo": alvo}
    pior_vence = _ORDEM_ESTADO[estado] > _ORDEM_ESTADO[atual["estado"]]
    notas = [n for n in (atual.get("nota"), nota) if n]
    return {"estado": estado if pior_vence else atual["estado"],
            "nota": "; ".join(dict.fromkeys(notas)),
            "alvo": alvo if pior_vence else atual["alvo"]}


def registrar(pasta_sprint: Path, codigo: str, estado: str, nota: str, alvo: str) -> None:
    dados = carregar(pasta_sprint)
    dados[codigo] = _juntar_um(dados.get(codigo), estado, nota, alvo)
    sandbox.escrever_json(Path(pasta_sprint) / "bancada.json", dados)


def juntar(contribuicoes: list[tuple[str, str, str]]) -> dict | None:
    """O que `registrar` deixaria num código depois destas contribuições (estado, nota, alvo), em ordem."""
    atual = None
    for estado, nota, alvo in contribuicoes:
        atual = _juntar_um(atual, estado, nota, alvo)
    return atual


def reconstruir(pasta_sprint: Path, codigo: str, contribuicoes: list[tuple[str, str, str]]) -> dict | None:
    """Refaz um código do zero a partir das contribuições (a consolidação, quando uma delas mudou — ex.: o
    achado de um cenário derrubado na verificação adversarial). Sem contribuição, não mexe e devolve None."""
    novo = juntar(contribuicoes)
    if novo is None:
        return None
    dados = carregar(pasta_sprint)
    dados[codigo] = novo
    sandbox.escrever_json(Path(pasta_sprint) / "bancada.json", dados)
    return novo
