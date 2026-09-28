"""Catálogo de funcionalidades, resultados por célula e a matriz de cobertura."""
from __future__ import annotations
import json
import time
from pathlib import Path

import yaml

from . import sandbox

VERIFICACOES = ("e2e", "sonda", "servico", "revisao", "suite", "benchmark")
RESULTADOS = ("passou", "falhou", "nao_testavel", "na")
_OBRIGATORIOS = ("id", "area", "titulo", "descricao", "origem", "verificacao", "cenarios", "requisitos", "marco")
_TENTATIVAS_YAML = 5
_ESPERA_YAML = 0.3


def carregar(caminho: Path) -> list[dict]:
    """Lê e faz parse do YAML; em `yaml.YAMLError` tenta de novo (até 5 vezes, 0,3s entre
    elas) antes de levantar — cobre o instante em que outro processo está regravando o
    arquivo (ex.: `catalogo publicar` de outra sprint)."""
    caminho = Path(caminho)
    ultimo_erro: yaml.YAMLError | None = None
    for tentativa in range(_TENTATIVAS_YAML):
        try:
            return yaml.safe_load(caminho.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as ex:
            ultimo_erro = ex
            if tentativa < _TENTATIVAS_YAML - 1:
                time.sleep(_ESPERA_YAML)
    raise ultimo_erro


def validar(itens: list[dict]) -> list[str]:
    erros, vistos = [], set()
    for i, it in enumerate(itens):
        for campo in _OBRIGATORIOS:
            if campo not in it:
                erros.append(f"item {i} ({it.get('id')}): falta '{campo}'")
        if it.get("id") in vistos:
            erros.append(f"id duplicado: {it.get('id')}")
        vistos.add(it.get("id"))
        for v in it.get("verificacao", []):
            if v not in VERIFICACOES:
                erros.append(f"{it.get('id')}: verificação desconhecida '{v}'")
    return erros


def registrar_resultado(pasta_sprint: Path, item: str, alvo: str, resultado: str,
                        motivo: str | None = None, achados: list[str] | None = None, fonte: str = "") -> None:
    if resultado not in RESULTADOS:
        raise ValueError(f"resultado inválido: {resultado}")
    p = Path(pasta_sprint) / "resultados.jsonl"
    linha = json.dumps({"item": item, "alvo": alvo, "resultado": resultado, "motivo": motivo,
                        "achados": achados or [], "fonte": fonte}, ensure_ascii=False)
    anterior = p.read_text(encoding="utf-8") if p.exists() else ""
    sandbox.escrever_texto(p, anterior + linha + "\n")


def descartar_fonte(pasta_sprint: Path, fonte: str) -> None:
    """Tira de resultados.jsonl as linhas gravadas por `fonte` (a consolidação refaz as suas)."""
    p = Path(pasta_sprint) / "resultados.jsonl"
    if not p.exists():
        return
    linhas = [l for l in p.read_text(encoding="utf-8").splitlines()
              if l.strip() and json.loads(l).get("fonte") != fonte]
    sandbox.escrever_texto(p, "".join(l + "\n" for l in linhas))


def carregar_resultados(pasta_sprint: Path) -> dict[tuple[str, str], dict]:
    p = Path(pasta_sprint) / "resultados.jsonl"
    out: dict[tuple[str, str], dict] = {}
    if p.exists():
        for linha in p.read_text(encoding="utf-8").splitlines():
            if linha.strip():
                r = json.loads(linha)
                out[(r["item"], r["alvo"])] = r
    return out


def montar_matriz(itens: list[dict], alvos: list[str], resultados: dict,
                  ausentes: dict[tuple[str, str], bool] | None = None) -> dict[str, dict[str, dict]]:
    ausentes = ausentes or {}
    m: dict[str, dict[str, dict]] = {}
    for it in itens:
        linha = {}
        for alvo in alvos:
            fora = it.get("somente_em") is not None and alvo not in it["somente_em"]
            if fora or ausentes.get((it["id"], alvo)):
                linha[alvo] = {"resultado": "na", "motivo": "a funcionalidade não existe neste alvo", "achados": []}
            elif (it["id"], alvo) in resultados:
                r = resultados[(it["id"], alvo)]
                linha[alvo] = {"resultado": r["resultado"], "motivo": r.get("motivo"), "achados": r.get("achados", [])}
            else:
                motivo = "sem resultado registrado nesta sprint"
                if not it.get("cenarios"):
                    motivo += f" (cenário previsto para o marco {it.get('marco', '?')})"
                linha[alvo] = {"resultado": "nao_testavel", "motivo": motivo, "achados": []}
        m[it["id"]] = linha
    return m


def resumo(matriz: dict[str, dict[str, dict]]) -> dict[str, int]:
    cont = {r: 0 for r in RESULTADOS}
    for linha in matriz.values():
        for cel in linha.values():
            cont[cel["resultado"]] += 1
    return cont
