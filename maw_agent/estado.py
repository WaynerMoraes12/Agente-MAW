"""Estado persistente de uma sprint: cada passo concluído fica gravado, e a retomada o pula.

Vários processos gravam o mesmo `estado.json` ao mesmo tempo (o script noturno, a compilação e o
`claude -p` marcando passos): toda mudança pega a trava `estado.lock`, relê o arquivo, mexe só na
sua chave e grava de forma atômica — ninguém apaga o passo de outro."""
from __future__ import annotations
import json
import msvcrt
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import config, sandbox

ESPERA_TRAVA = 60.0  # segundos esperando outro processo soltar a trava antes de desistir


@contextmanager
def _trava(pasta: Path):
    """Trava entre processos (e entre threads) sobre o primeiro byte de `estado.lock`."""
    caminho = sandbox.garantir_escrita(Path(pasta) / "estado.lock")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    f = open(caminho, "a+b")
    try:
        limite = time.monotonic() + ESPERA_TRAVA
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= limite:
                    raise TimeoutError(f"estado.lock de {Path(pasta).name} ocupado há mais de {ESPERA_TRAVA:.0f} s")
                time.sleep(0.02)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        f.close()

FASES = ("preflight", "preparar", "compilar", "catalogar", "executar", "verificar",
         "consolidar", "encerrar", "relatorio")
FINAL = "relatorio"  # a sprint só está concluída quando o PDF existe


class Estado:
    def __init__(self, pasta: Path, numero: int, passos: dict | None = None, criado: str | None = None,
                 restauracoes: list[dict] | None = None):
        self.pasta = Path(pasta)
        self.numero = numero
        self.nome = f"sprint-{numero:02d}"
        self.passos: dict[str, dict] = passos or {}
        self.criado = criado or time.strftime("%Y-%m-%dT%H:%M:%S")
        # cada restauração do %APPDATA%\MAW feita nesta sprint, com o resultado da conferência
        self.restauracoes: list[dict] = restauracoes or []

    @staticmethod
    def _agora() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    def feito(self, passo: str) -> bool:
        return self.passos.get(passo, {}).get("status") == "concluido"

    def dados(self, passo: str) -> dict | None:
        return self.passos.get(passo, {}).get("detalhe")

    def iniciar(self, passo: str) -> None:
        self._mudar(lambda: self.passos.__setitem__(passo, {"status": "em_andamento", "inicio": self._agora()}))

    def concluir(self, passo: str, detalhe: dict | None = None) -> None:
        def mudar():
            p = self.passos.setdefault(passo, {"inicio": self._agora()})
            p.update({"status": "concluido", "fim": self._agora(), "detalhe": detalhe})
        self._mudar(mudar)

    def falhar(self, passo: str, erro: str) -> None:
        def mudar():
            p = self.passos.setdefault(passo, {"inicio": self._agora()})
            p.update({"status": "falhou", "fim": self._agora(), "erro": erro})
        self._mudar(mudar)

    def registrar_restauracao(self, registro: dict) -> None:
        self._mudar(lambda: self.restauracoes.append({"quando": self._agora(), **registro}))

    def _mudar(self, mudanca: Callable[[], None]) -> None:
        """Trava → relê o disco (o que outro processo gravou) → aplica só esta mudança → grava."""
        with _trava(self.pasta):
            arq = self.pasta / "estado.json"
            if arq.exists():
                d = json.loads(arq.read_text(encoding="utf-8"))
                self.passos = d.get("passos", {})
                self.restauracoes = d.get("restauracoes", [])
            mudanca()
            self._gravar()

    def salvar(self) -> None:
        with _trava(self.pasta):
            self._gravar()

    def _gravar(self) -> None:
        sandbox.escrever_json(self.pasta / "estado.json",
                              {"numero": self.numero, "criado": self.criado, "passos": self.passos,
                               "restauracoes": self.restauracoes})


def carregar(pasta: Path) -> Estado:
    d = json.loads((Path(pasta) / "estado.json").read_text(encoding="utf-8"))
    return Estado(pasta, d["numero"], d["passos"], d.get("criado"), d.get("restauracoes"))


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
    """Só a sprint mais nova pode estar em andamento: se ela está concluída, nenhuma está.
    Uma sprint antiga abandonada nunca é retomada."""
    existentes = [p for p in _existentes(raiz) if (p / "estado.json").exists()]
    if not existentes:
        return None
    e = carregar(existentes[-1])
    return None if e.feito(FINAL) else e


def anterior(e: Estado) -> Path | None:
    for p in reversed(_existentes(e.pasta.parent)):
        if int(p.name.split("-")[1]) < e.numero and (p / "estado.json").exists() and carregar(p).feito(FINAL):
            return p
    return None
