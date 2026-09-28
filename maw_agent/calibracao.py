"""Calibração: reinjeta defeitos conhecidos no alvo e mede se a suíte (com as sondas) os pega.

Cada defeito é uma troca de texto que tem de acontecer exatamente uma vez num arquivo. A medição
tem controle: a mesma árvore sem defeito é compilada e rodada primeiro, e só conta como detecção o
que falha a mais do que no controle (um bloco que já falhava não prova nada). Um defeito que não se
aplica, não compila ou não pôde rodar vira `erro` e fica fora da taxa: é problema da calibração,
não da suíte.

A orquestração recebe as operações de fora (`Operacoes`): criar a worktree, compilar, rodar a suíte
e limpar. Assim ela é testável sem MSBuild, e quem roda de verdade decide como proteger o ambiente.
"""
from __future__ import annotations
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import yaml

from . import alvos, sandbox

CONTROLE = "_controle"


class DefeitoInvalido(Exception):
    """O defeito não pode ser aplicado como está escrito (texto ausente, repetido, arquivo sumido)."""


class Abortar(Exception):
    """Uma operação encontrou algo que impede continuar (ex.: o ambiente não foi restaurado)."""


@dataclass
class Defeito:
    id: str
    arquivo: str
    buscar: str
    trocar: str
    origem: str = ""
    descricao: str = ""
    esperado: list[str] = field(default_factory=list)

    def como_dict(self) -> dict:
        return asdict(self)


class Operacoes(Protocol):
    def preparar(self, nome: str) -> Path: ...
    def compilar(self, nome: str, worktree: Path) -> dict: ...
    def rodar(self, nome: str, exe: Path) -> dict: ...
    def limpar(self, nome: str) -> None: ...  # remove a worktree de `nome`, exista ela inteira, pela metade ou não


# ---------- defeitos ----------

def carregar_defeitos(caminho: Path) -> list[Defeito]:
    brutos = yaml.safe_load(Path(caminho).read_text(encoding="utf-8")) or []
    if not isinstance(brutos, list):
        raise DefeitoInvalido(f"{caminho}: esperava uma lista de defeitos")
    out: list[Defeito] = []
    vistos: set[str] = set()
    for i, b in enumerate(brutos):
        rotulo = f"defeito {i} ({(b or {}).get('id')})"
        for campo in ("id", "arquivo", "buscar", "trocar"):
            if not isinstance((b or {}).get(campo), str) or not b[campo]:
                raise DefeitoInvalido(f"{rotulo}: falta '{campo}' (texto não vazio)")
        if b["id"] in vistos:
            raise DefeitoInvalido(f"id duplicado: {b['id']}")
        vistos.add(b["id"])
        if b["buscar"] == b["trocar"]:
            raise DefeitoInvalido(f"{rotulo}: 'trocar' igual a 'buscar' não é defeito")
        if Path(b["arquivo"]).is_absolute() or ".." in Path(b["arquivo"]).parts:
            raise DefeitoInvalido(f"{rotulo}: 'arquivo' tem de ser relativo à raiz do alvo")
        esperado = b.get("esperado")
        if not isinstance(esperado, list) or not esperado or not all(isinstance(x, str) and x for x in esperado):
            raise DefeitoInvalido(f"{rotulo}: 'esperado' tem de ser uma lista não vazia de blocos da suíte "
                                  "(sem ela a detecção não pode ser contada)")
        out.append(Defeito(b["id"], b["arquivo"], b["buscar"], b["trocar"], str(b.get("origem") or ""),
                           str(b.get("descricao") or ""), list(esperado)))
    return out


def aplicar_defeito(worktree: Path, d: Defeito) -> Path:
    """Troca `buscar` por `trocar` no arquivo; recusa se o texto não aparece exatamente uma vez.
    Num arquivo com CRLF, as quebras de linha do defeito viram CRLF."""
    p = Path(worktree) / d.arquivo
    sandbox.garantir_escrita(p)
    if not p.is_file():
        raise DefeitoInvalido(f"{d.id}: arquivo não existe no alvo: {d.arquivo}")
    texto = p.read_bytes().decode("utf-8", errors="surrogateescape")
    buscar, trocar = d.buscar, d.trocar
    if "\r\n" in texto:
        buscar = buscar.replace("\r\n", "\n").replace("\n", "\r\n")
        trocar = trocar.replace("\r\n", "\n").replace("\n", "\r\n")
    n = texto.count(buscar)
    if n == 0:
        raise DefeitoInvalido(f"{d.id}: texto não encontrado em {d.arquivo}")
    if n > 1:
        raise DefeitoInvalido(f"{d.id}: o texto aparece {n} vezes em {d.arquivo}; precisa ser único")
    sandbox.escrever_bytes(p, texto.replace(buscar, trocar).encode("utf-8", errors="surrogateescape"))
    return p


# ---------- avaliação ----------

def _falhas(suite: dict | None) -> list[str]:
    return [f"{b['nome']} → {b['sub']}" for b in (suite or {}).get("blocos", []) if b.get("falhas")]


def _passaram(suite: dict | None) -> set[str]:
    return {f"{b['nome']} → {b['sub']}" for b in (suite or {}).get("blocos", []) if not b.get("falhas")}


def _casa(falha: str, esperado: list[str]) -> bool:
    nome = falha.split(" → ", 1)[0]
    return any(e == falha or e == nome for e in esperado)


def avaliar(suite: dict, controle: dict | None, esperado: list[str]) -> dict:
    """Detectado (o que entra na taxa) = algum bloco ESPERADO falhou e esse mesmo bloco passou no
    controle. Falha nova fora dos esperados, bloco esperado que nem existia no controle e incoerência
    nova (a suíte morreu, não fechou os totais) não contam: vão para `detectado_so_por_outro_bloco`,
    que o relatório mostra à parte."""
    ja_falhavam = set(_falhas(controle))
    passaram_no_controle = _passaram(controle)
    novas = [f for f in _falhas(suite) if f not in ja_falhavam]
    esperadas = [f for f in novas if _casa(f, esperado) and f in passaram_no_controle]
    outras = [f for f in novas if f not in esperadas]
    incoerencias_antes = set((controle or {}).get("incoerencias", []))
    incoerencias = [i for i in suite.get("incoerencias", []) if i not in incoerencias_antes]
    detectado = bool(esperadas)
    return {"detectado": detectado, "detectado_so_por_outro_bloco": not detectado and bool(outras or incoerencias),
            "blocos_esperados_que_falharam": esperadas, "outros_blocos_que_falharam": outras,
            "blocos_que_falharam": novas, "incoerencias": incoerencias}


def taxa(resultados: dict[str, dict]) -> dict:
    """Taxa = detectados pelo bloco esperado / defeitos medidos (sem erro). Os que só derrubaram
    outro bloco ficam fora da taxa e são contados à parte."""
    validos = [r for r in resultados.values() if not r.get("erro")]
    detectados = sum(1 for r in validos if r.get("detectado"))
    return {"total": len(resultados), "validos": len(validos), "detectados": detectados,
            "detectados_so_por_outro_bloco": sum(1 for r in validos if r.get("detectado_so_por_outro_bloco")),
            "erros": len(resultados) - len(validos),
            "taxa": round(detectados / len(validos), 4) if validos else None}


# ---------- worktrees ----------

def criar_worktree(espelho: Path, commit: str, destino: Path) -> Path:
    """Worktree destacada do espelho do agente em `destino`, sempre limpa (uma sobra de execução
    anterior é removida antes)."""
    destino = Path(destino)
    sandbox.garantir_escrita(destino)
    if destino.exists():
        remover_worktree(espelho, destino)
    sandbox.criar_pasta(destino.parent)
    alvos.git(["worktree", "prune"], Path(espelho))
    alvos.git(["worktree", "add", "-q", "--force", "--detach", str(destino), commit], Path(espelho))
    return destino


def remover_worktree(espelho: Path, destino: Path, tentativas: int = 5, espera: float = 1.0) -> None:
    """`git worktree remove --force`; se o Windows ainda segura algum arquivo (pdb, exe recém-
    fechado), tenta de novo e, no fim, apaga a pasta pela sandbox e poda o registro."""
    destino = Path(destino)
    sandbox.garantir_escrita(destino)
    ultimo: Exception | None = None
    for i in range(tentativas):
        try:
            if destino.exists():
                try:
                    alvos.git(["worktree", "remove", "--force", str(destino)], Path(espelho))
                except RuntimeError:
                    pass  # não registrada ou arquivo preso: a sandbox tenta abaixo
            if destino.exists():
                sandbox.remover(destino)
            ultimo = None
            break
        except OSError as ex:
            ultimo = ex
            time.sleep(espera * (i + 1))
    alvos.git(["worktree", "prune"], Path(espelho))
    if ultimo is not None or destino.exists():
        raise OSError(f"não consegui remover a worktree de calibração {destino}: {ultimo}")


# ---------- orquestração ----------

def _erro(msg: str) -> dict:
    return {"detectado": False, "detectado_so_por_outro_bloco": False, "blocos_esperados_que_falharam": [],
            "outros_blocos_que_falharam": [], "blocos_que_falharam": [], "incoerencias": [], "erro": msg}


def _limpar(ops: Operacoes, nome: str) -> str | None:
    """Limpa pelo id em toda saída (também quando `preparar` caiu e deixou uma worktree pela
    metade); devolve o erro da limpeza, que fica registrado no resultado."""
    try:
        ops.limpar(nome)
        return None
    except Exception as ex:
        return f"{type(ex).__name__}: {ex}"


def _medir_controle(ops: Operacoes) -> dict:
    inicio = time.monotonic()
    out: dict = {"erro": None, "suite": None, "blocos_que_falharam": [], "abortar": None}
    try:
        wt = ops.preparar(CONTROLE)
        b = ops.compilar(CONTROLE, wt)
        if not b.get("ok"):
            out["erro"] = "o controle (sem defeito) não compilou: " + ("; ".join(b.get("erros", [])[:3]) or "sem mensagem")
        else:
            r = ops.rodar(CONTROLE, Path(b["exe"]))
            out["suite"] = r
            out["blocos_que_falharam"] = _falhas(r)
            if r.get("incoerencias"):
                out["erro"] = "a suíte do controle não é confiável: " + "; ".join(r["incoerencias"])
    except Abortar as ex:
        out["erro"] = out["abortar"] = str(ex)
    except Exception as ex:  # erro do agente: vai para o resultado, não some
        out["erro"] = f"{type(ex).__name__}: {ex}"
    finally:
        limpeza = _limpar(ops, CONTROLE)
        if limpeza:
            out["limpeza"] = limpeza
    out["segundos"] = round(time.monotonic() - inicio, 1)
    return out


def calibrar(defeitos: list[Defeito], ops: Operacoes,
             ao_registrar: Callable[[str, dict], None] | None = None) -> tuple[dict[str, dict], dict]:
    """Mede o controle e depois cada defeito numa worktree própria, limpa pelo id em toda saída.
    Devolve ({id: resultado}, controle). `ao_registrar(id, resultado)` é chamado a cada defeito
    (para gravar o parcial: uma execução que cai no meio não perde o que já mediu)."""
    controle = _medir_controle(ops)
    resultados: dict[str, dict] = {}
    parada = None if controle["erro"] is None else f"sem controle válido: {controle['erro']}"
    for d in defeitos:
        inicio = time.monotonic()
        if parada is not None:
            res = _erro(parada)
        else:
            res = None
            limpeza = None
            try:
                wt = ops.preparar(d.id)
                aplicar_defeito(wt, d)
                b = ops.compilar(d.id, wt)
                if not b.get("ok"):
                    res = _erro("não compilou com o defeito: " + ("; ".join(b.get("erros", [])[:3]) or "sem mensagem"))
                else:
                    r = ops.rodar(d.id, Path(b["exe"]))
                    res = {**avaliar(r, controle["suite"], d.esperado), "erro": None}
            except DefeitoInvalido as ex:
                res = _erro(str(ex))
            except Abortar as ex:
                parada = f"calibração interrompida: {ex}"
                res = _erro(parada)
            except Exception as ex:  # erro do agente: registrado no defeito, a calibração segue
                res = _erro(f"{type(ex).__name__}: {ex}")
            finally:
                limpeza = _limpar(ops, d.id)
            if limpeza:
                res["limpeza"] = limpeza
        res["segundos"] = round(time.monotonic() - inicio, 1)
        res["origem"] = d.origem
        res["esperado"] = list(d.esperado)
        resultados[d.id] = res
        if ao_registrar is not None:
            ao_registrar(d.id, res)
    return resultados, controle

