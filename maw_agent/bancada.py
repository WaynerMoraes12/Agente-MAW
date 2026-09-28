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


def registrar(pasta_sprint: Path, codigo: str, estado: str, nota: str, alvo: str) -> None:
    if estado not in _ORDEM_ESTADO:
        raise ValueError(f"estado inválido: {estado!r} (use {', '.join(ESTADOS)})")
    dados = carregar(pasta_sprint)
    atual = dados.get(codigo)
    if atual is None:
        dados[codigo] = {"estado": estado, "nota": nota, "alvo": alvo}
    else:
        pior_vence = _ORDEM_ESTADO[estado] > _ORDEM_ESTADO[atual["estado"]]
        notas = [n for n in (atual.get("nota"), nota) if n]
        dados[codigo] = {"estado": estado if pior_vence else atual["estado"],
                         "nota": "; ".join(dict.fromkeys(notas)),
                         "alvo": alvo if pior_vence else atual["alvo"]}
    sandbox.escrever_json(Path(pasta_sprint) / "bancada.json", dados)
