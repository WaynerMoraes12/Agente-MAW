"""Achados: validação por esquema, impressão digital estável, deduplicação e histórico."""
from __future__ import annotations
import copy
import hashlib
import json
import re
from collections.abc import Collection
from pathlib import Path

import jsonschema

from . import sandbox

TIPOS = ("erro", "bug", "violacao", "afirmacao_falsa", "lacuna", "melhoria")
SEVERIDADES = ("critica", "alta", "media", "baixa")
PRIORIDADES = ("alta", "media", "baixa")
CONFIANCAS = ("confirmado", "provavel")
ESTADOS = ("novo", "aberto", "corrigido", "regressao", "nao_verificavel", "alvo_removido")
# notícia só na sprint em que acontecem; nas seguintes, o achado não visto fica fora da lista
ESTADOS_TERMINAIS = ("corrigido", "alvo_removido")
_DO_VEREDITO = {"corrigido": "corrigido", "persiste": "aberto", "nao_verificavel": "nao_verificavel"}
ORDEM_SEVERIDADE = {s: i for i, s in enumerate(SEVERIDADES)}
_ESQUEMA = json.loads((Path(__file__).parent / "esquemas" / "achado.schema.json").read_text(encoding="utf-8"))


def validar(obj: dict) -> list[str]:
    erros = [f"{'/'.join(map(str, e.path)) or 'raiz'}: {e.message}"
             for e in jsonschema.Draft202012Validator(_ESQUEMA).iter_errors(obj)]
    if obj.get("tipo") == "melhoria":
        if not obj.get("prioridade"):
            erros.append("melhoria precisa de prioridade")
    elif obj.get("tipo") in TIPOS and not obj.get("severidade"):
        erros.append("achado que não é melhoria precisa de severidade")
    return erros


def normalizar_assinatura(s: str) -> str:
    s = s.casefold()
    s = re.sub(r":\d+", "", s)
    s = re.sub(r"0x[0-9a-f]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def impressao_digital(obj: dict) -> str:
    chave = f"{obj['item_catalogo']}|{obj['tipo']}|{normalizar_assinatura(obj['assinatura'])}"
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()[:16]


def _rank(a: dict) -> int:
    if a.get("tipo") == "melhoria":
        return PRIORIDADES.index(a.get("prioridade") or "baixa")
    return ORDEM_SEVERIDADE.get(a.get("severidade") or "baixa", 3)


def deduplicar(lista: list[dict]) -> list[dict]:
    por_imp: dict[str, dict] = {}
    for a in lista:
        imp = impressao_digital(a)
        if imp not in por_imp:
            por_imp[imp] = copy.deepcopy(a)
            continue
        u = por_imp[imp]
        for alvo in a["alvos"]:
            if alvo not in u["alvos"]:
                u["alvos"].append(alvo)
        for ev in a.get("evidencias", []):
            if ev not in u["evidencias"]:
                u["evidencias"].append(ev)
        if _rank(a) < _rank(u):
            u["severidade"], u["prioridade"] = a.get("severidade"), a.get("prioridade")
    return list(por_imp.values())


def marcar_introducao(lista: list[dict], alvo_principal: str = "main") -> None:
    for a in lista:
        nomes = {x["alvo"] for x in a["alvos"]}
        a["introduzido_por"] = next(iter(nomes)) if len(nomes) == 1 and alvo_principal not in nomes else None


def carregar_historico(p: Path) -> dict:
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"proximo": 1, "itens": {}}


def salvar_historico(p: Path, h: dict) -> None:
    sandbox.escrever_json(Path(p), h)


def consolidar(achados: list[dict], historico: dict, sprint: str, reverificacoes: dict[str, str],
               alvos_removidos: Collection[str] = (), alvo_principal: dict | None = None) -> tuple[list[dict], dict]:
    """Junta os achados desta sprint ao histórico. Um achado do histórico não visto nesta sprint fica com
    o que a reverificação disser (ela vence sempre); sem reverificação, vira `alvo_removido` quando todos
    os seus alvos estão em `alvos_removidos` (a branch foi apagada do origin), senão `nao_verificavel`.
    `persiste` num achado de alvos todos removidos só pode ter sido visto no `alvo_principal` (o main,
    para onde o código da branch foi antes de ela ser apagada): ele entra nos alvos do achado.
    `alvo_removido` é terminal como `corrigido`; se a mesma impressão digital voltar a aparecer, o achado
    reabre como `aberto` — não `regressao`, porque ele nunca foi corrigido, só a branch sumiu."""
    h = copy.deepcopy(historico)
    removidos = set(alvos_removidos)
    vivos = [a for a in achados if (a.get("veredito") or {}).get("resultado") != "derrubado"]
    saida: list[dict] = []
    vistos: set[str] = set()
    for a in deduplicar(vivos):
        imp = impressao_digital(a)
        vistos.add(imp)
        reg = h["itens"].get(imp)
        if reg is None:
            reg = {"id": f"MAW-{h['proximo']:04d}", "historico": []}
            h["proximo"] += 1
            estado = "novo"
        else:  # reaparecer depois de alvo_removido também é "aberto"
            estado = "regressao" if reg.get("estado") == "corrigido" else "aberto"
        reg["estado_anterior"] = reg.get("estado")
        reg["historico"] = reg["historico"] + [sprint]
        reg.update({"estado": estado, "ultimo": a, "sprint_do_estado": sprint})
        h["itens"][imp] = reg
        saida.append({**a, "id": reg["id"], "estado": estado, "historico": reg["historico"],
                      "estado_anterior": reg["estado_anterior"]})
    for imp, reg in h["itens"].items():
        if imp in vistos:
            continue
        anterior = reg.get("estado")
        if anterior in ESTADOS_TERMINAIS:
            if reg.get("sprint_do_estado") != sprint:
                continue  # corrigido (ou alvo removido) numa sprint anterior: não é notícia
        rv = reverificacoes.get(reg["id"])
        alvos_do_achado = reg["ultimo"].get("alvos", [])
        todos_removidos = bool(alvos_do_achado) and {x["alvo"] for x in alvos_do_achado} <= removidos
        if rv in _DO_VEREDITO:
            novo = _DO_VEREDITO[rv]
            if novo == "aberto" and todos_removidos and alvo_principal and alvo_principal not in alvos_do_achado:
                reg["ultimo"] = {**reg["ultimo"], "alvos": alvos_do_achado + [dict(alvo_principal)]}
        else:
            novo = "alvo_removido" if todos_removidos else "nao_verificavel"
        reg["historico"] = reg["historico"] + [sprint]
        reg.update({"estado": novo, "estado_anterior": anterior, "sprint_do_estado": sprint})
        saida.append({**reg["ultimo"], "id": reg["id"], "estado": novo, "historico": reg["historico"],
                      "estado_anterior": anterior})
    saida.sort(key=lambda a: (_rank(a), a["id"]))
    return saida, h
