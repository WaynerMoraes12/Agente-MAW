"""Troca segredos por [REDACTED] antes de qualquer texto ir para disco."""
from __future__ import annotations
import re

PADROES: dict[str, re.Pattern[str]] = {
    "chave-google": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "token-github": re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{22,})"),
    "chave-openai-anthropic": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}"),
}


def segredos_em(texto: str) -> list[str]:
    return [nome for nome, rx in PADROES.items() if rx.search(texto)]


def redigir(texto: str) -> str:
    for rx in PADROES.values():
        texto = rx.sub("[REDACTED]", texto)
    return texto
