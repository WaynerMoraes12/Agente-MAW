"""Estado persistente de uma sprint: cada passo concluído fica gravado, e a retomada o pula."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from . import config, sandbox

FASES = ("preflight", "preparar", "compilar", "catalogar", "executar", "verificar",
         "consolidar", "encerrar", "relatorio")
FINAL = "relatorio"  # a sprint só está concluída quando o PDF existe


class Estado:
    def __init__(self, pasta: Path, numero: int, passos: dict | None = None, criado: str | None = None):
        self.pasta = Path(pasta)
        self.numero = numero
        self.nome = f"sprint-{numero:02d}"
        self.passos: dict[str, dict] = passos or {}
        self.criado = criado or time.strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _agora() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    def feito(self, passo: str) -> bool:
        return self.passos.get(passo, {}).get("status") == "concluido"

    def dados(self, passo: str) -> dict | None:
        return self.passos.get(passo, {}).get("detalhe")

    def iniciar(self, passo: str) -> None:
        self.passos[passo] = {"status": "em_andamento", "inicio": self._agora()}
        self.salvar()

    def concluir(self, passo: str, detalhe: dict | None = None) -> None:
        p = self.passos.setdefault(passo, {"inicio": self._agora()})
        p.update({"status": "concluido", "fim": self._agora(), "detalhe": detalhe})
        self.salvar()

    def falhar(self, passo: str, erro: str) -> None:
        p = self.passos.setdefault(passo, {"inicio": self._agora()})
        p.update({"status": "falhou", "fim": self._agora(), "erro": erro})
        self.salvar()

    def salvar(self) -> None:
        sandbox.escrever_json(self.pasta / "estado.json",
                              {"numero": self.numero, "criado": self.criado, "passos": self.passos})


def carregar(pasta: Path) -> Estado:
    d = json.loads((Path(pasta) / "estado.json").read_text(encoding="utf-8"))
    return Estado(pasta, d["numero"], d["passos"], d.get("criado"))


def _existentes(raiz: Path) -> list[Path]:
    return sorted((p for p in Path(raiz).glob("sprint-*") if re.fullmatch(r"sprint-\d+", p.name)),
                  key=lambda p: int(p.name.split("-")[1]))


def nova_sprint(raiz: Path = config.RELATORIOS) -> Estado:
    nums = [int(p.name.split("-")[1]) for p in _existentes(raiz)]
    n = (max(nums) + 1) if nums else 1
    e = Estado(Path(raiz) / f"sprint-{n:02d}", n)
    sandbox.criar_pasta(e.pasta)
    e.salvar()
    return e


def em_andamento(raiz: Path = config.RELATORIOS) -> Estado | None:
    for p in reversed(_existentes(raiz)):
        if (p / "estado.json").exists():
            e = carregar(p)
            if not e.feito(FINAL):
                return e
    return None


def anterior(e: Estado) -> Path | None:
    for p in reversed(_existentes(e.pasta.parent)):
        if int(p.name.split("-")[1]) < e.numero and (p / "estado.json").exists() and carregar(p).feito(FINAL):
            return p
    return None
