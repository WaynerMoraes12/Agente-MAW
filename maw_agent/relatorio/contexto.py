"""Lê a pasta da sprint e monta o dicionário que o modelo HTML usa. Nada é inventado:
arquivo ausente vira limitação declarada."""
from __future__ import annotations
import base64
import json
import time
from collections import Counter
from pathlib import Path

from .. import catalogo

ABERTOS = ("novo", "aberto", "regressao", "nao_verificavel")
_ROTULO_SEMAFORO = {"pronto": "Pronto para merge", "com_ressalvas": "Com ressalvas", "bloqueado": "Bloqueado"}


def _ler(p: Path, padrao=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else padrao


def semaforo(achados_do_alvo: list[dict], build_ok: bool) -> str:
    if not build_ok:
        return "bloqueado"
    abertos = [a for a in achados_do_alvo if a.get("estado", "novo") in ABERTOS]
    graves = [a for a in abertos if a.get("tipo") != "melhoria" and a.get("severidade") in ("critica", "alta")
              and a.get("confianca") == "confirmado"]
    if graves:
        return "bloqueado"
    if [a for a in abertos if a.get("tipo") not in ("melhoria", "lacuna")]:
        return "com_ressalvas"
    return "pronto"


def _duracao(estado: dict) -> str:
    passos = [p for p in estado.get("passos", {}).values() if p.get("inicio") and p.get("fim")]
    if not passos:
        return "não medida"
    fmt = "%Y-%m-%dT%H:%M:%S"
    ini = min(time.mktime(time.strptime(p["inicio"], fmt)) for p in passos)
    fim = max(time.mktime(time.strptime(p["fim"], fmt)) for p in passos)
    h, r = divmod(int(fim - ini), 3600)
    return f"{h} h {r // 60:02d} min"


def montar(pasta: Path, itens: list[dict], principios: list[dict], anterior: Path | None,
           logo: Path | None) -> dict:
    pasta = Path(pasta)
    estado = _ler(pasta / "estado.json", {"numero": 0, "passos": {}})
    alvos_info = _ler(pasta / "alvos.json", {"alvos": [], "avisos": []})
    achados_lista = _ler(pasta / "achados.json", [])
    for a in achados_lista:
        for ev in a.get("evidencias", []):
            arq = Path(ev["arquivo"]) if Path(ev["arquivo"]).is_absolute() else pasta / ev["arquivo"]
            if ev.get("embutir") and arq.suffix.lower() in (".png", ".jpg", ".jpeg") and arq.exists():
                tipo = "png" if arq.suffix.lower() == ".png" else "jpeg"
                ev["data_uri"] = f"data:image/{tipo};base64," + base64.b64encode(arq.read_bytes()).decode()
    intocada = _ler(pasta / "intocada.json", {"verificado": False, "diferencas": ["prova não registrada"],
                                              "ambiente_restaurado": False})
    textos = _ler(pasta / "textos.json", {})
    resumo = textos.get("resumo")
    limitacoes: list[str] = list(alvos_info.get("avisos", []))
    if not resumo:
        limitacoes.append("resumo executivo não redigido nesta execução")
        resumo = ""
    for v in _ler(pasta / "preflight.json", []):
        if not v["ok"]:
            limitacoes.append(f"Pré-voo: {v['detalhe']}")
    nomes = [a["nome"] for a in alvos_info["alvos"]]
    alvos = []
    for a in alvos_info["alvos"]:
        origem = a.get("compartilha_com") or a["nome"]
        b_rel = _ler(pasta / "builds" / f"{origem}-Release.json")
        b_dbg = _ler(pasta / "builds" / f"{origem}-Debug.json")
        build_ok = bool(b_rel and b_rel["ok"])
        if b_rel is None:
            limitacoes.append(f"{a['nome']}: build Release não registrado")
        doalvo = [x for x in achados_lista if a["nome"] in {y["alvo"] for y in x["alvos"]}]
        sem = semaforo(doalvo, build_ok)
        texto_alvo = textos.get("por_alvo", {}).get(a["nome"])
        if not texto_alvo:
            limitacoes.append(f"texto do alvo {a['nome']} não redigido nesta execução")
            texto_alvo = ""
        alvos.append({**a, "semaforo": sem,
                      "semaforo_rotulo": _ROTULO_SEMAFORO[sem],
                      "build": b_rel, "build_debug": b_dbg,
                      "suite": _ler(pasta / "suites" / f"{origem}.json"),
                      "benchmark": _ler(pasta / "benchmarks" / f"{origem}.json"),
                      "achados_ids": [x["id"] for x in doalvo],
                      "introduzidos": [x["id"] for x in doalvo if x.get("introduzido_por") == a["nome"]],
                      "texto": texto_alvo})
    resultados = catalogo.carregar_resultados(pasta)
    m = catalogo.montar_matriz(itens, nomes, resultados)
    linhas = [{"item": it["id"], "titulo": it["titulo"], "area": it["area"],
               "celulas": [m[it["id"]][n] for n in nomes]} for it in itens]
    cobertura = catalogo.resumo(m)
    motivos = Counter(c["motivo"] for l in m.values() for c in l.values() if c["resultado"] == "nao_testavel")
    for motivo, n in motivos.most_common():
        limitacoes.append(f"{n} célula(s) não testável(is): {motivo or 'motivo não informado'}")
    problemas = [a for a in achados_lista if a["tipo"] != "melhoria"]
    melhorias = [a for a in achados_lista if a["tipo"] == "melhoria"]
    provaveis = [a for a in achados_lista if a.get("confianca") == "provavel"]
    if provaveis:
        limitacoes.append(f"{len(provaveis)} achado(s) marcado(s) como provável(is): evidentes no código, não reproduzidos dinamicamente")
    derrubados = _ler(pasta / "derrubados.json", [])
    erros_agente = _ler(pasta / "erros_agente.json", [])
    for passo, info in estado.get("passos", {}).items():
        if info.get("status") == "falhou":
            erros_agente.append(f"passo {passo} falhou: {info.get('erro') or 'sem mensagem de erro'}")
    abertos = [a for a in problemas if a["estado"] in ABERTOS]
    principios_ctx = []
    for p in principios:
        cels = [m.get(f"principio/{p['id']}", {}).get(n, {"resultado": "nao_testavel", "motivo": "sem item na matriz", "achados": []})
                for n in nomes]
        principios_ctx.append({**p, "celulas": cels,
                               "violacoes": [a["id"] for a in achados_lista if a.get("principio") == p["id"]]})
    logo_uri = None
    if logo and Path(logo).exists():
        logo_uri = "data:image/png;base64," + base64.b64encode(Path(logo).read_bytes()).decode()
    return {
        "sprint": f"sprint-{estado['numero']:02d}",
        "data": estado.get("criado", "")[:10],
        "duracao": _duracao(estado),
        "alvos": alvos,
        "avisos_alvos": alvos_info.get("avisos", []),
        "veredito": textos.get("veredito", "Veredito não redigido nesta execução."),
        "resumo": resumo,
        "contagens": {
            "por_severidade": dict(Counter(a.get("severidade") for a in abertos)),
            "por_estado": dict(Counter(a["estado"] for a in achados_lista)),
            "por_tipo": dict(Counter(a["tipo"] for a in achados_lista)),
        },
        "top5": abertos[:5],
        "achados": problemas,
        "melhorias": melhorias,
        "corrigidos": [a for a in achados_lista if a["estado"] == "corrigido"],
        "regressoes": [a for a in achados_lista if a["estado"] == "regressao"],
        "principios": principios_ctx,
        "matriz": {"alvos": nomes, "linhas": linhas},
        "cobertura": cobertura,
        "limitacoes": limitacoes,
        "derrubados": derrubados,
        "erros_agente": erros_agente,
        "intocada": intocada,
        "ambiente": _ler(pasta / "ambiente.json", {}),
        "preflight": _ler(pasta / "preflight.json", []),
        "anterior": anterior.name if anterior else None,
        "logo_data_uri": logo_uri,
    }
