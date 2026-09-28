"""Fases da sprint expostas como `python -m maw_agent sprint <acao>` e `achado <acao>`."""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

from . import (achados, alvos, build, catalogo, config, estado, preflight, sandbox, saude, suite)
from .cli import registrar


def _saida(obj: dict, ok: bool = True) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def _sprint_atual() -> estado.Estado:
    e = estado.em_andamento()
    if e is None:
        raise SystemExit("nenhuma sprint em andamento: rode `sprint iniciar`")
    return e


def _alvos(e: estado.Estado, so: str | None = None) -> list[dict]:
    lista = json.loads((e.pasta / "alvos.json").read_text(encoding="utf-8"))["alvos"]
    return [a for a in lista if so is None or a["nome"] == so]


# ---------- funções puras ----------

def achado_de_build(alvo: dict, r: dict) -> dict:
    cfg = r["config"]
    obtido = ("; ".join(r["erros"][:5]) if r["erros"] else
              f"o build falhou sem mensagem de erro reconhecível; ver o log {Path(r['log']).name}")
    return {"titulo": f"Build {cfg} de {alvo['nome']} não compila", "tipo": "erro", "severidade": "critica",
            "prioridade": None, "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
            "item_catalogo": f"saude/build-{cfg.lower()}", "principio": "P14",
            "passos": [f"msbuild Builds\\VisualStudio2022\\MAW_APP_App.vcxproj /p:Configuration={cfg} /p:Platform=x64"],
            "esperado": "compilação sem erros", "obtido": obtido,
            "evidencias": [{"arquivo": r["log"], "legenda": "log do MSBuild", "embutir": False}],
            "causa_provavel": None, "sugestao": "corrigir os erros de compilação listados",
            "criterio_aceite": f"o build {cfg} compila sem erros", "confianca": "confirmado",
            "assinatura": f"build:{cfg}:" + (r["erros"][0] if r["erros"] else "sem-erro"), "fonte": "build"}


_RX_DETALHE_CABECALHO = re.compile(r"^-\s*(.+?)\s+/\s+(.+?)\s*$")


def _detalhes_por_bloco(detalhes: list[str]) -> dict[tuple[str, str], list[str]]:
    """Agrupa as linhas de 'Detalhe das falhas' pelo cabeçalho ('- <nome> / <sub>') que as antecede,
    para que o achado de um bloco não carregue as mensagens de outro bloco."""
    out: dict[tuple[str, str], list[str]] = {}
    atual: tuple[str, str] | None = None
    for linha in detalhes:
        m = _RX_DETALHE_CABECALHO.match(linha)
        if m:
            atual = (m.group(1), m.group(2))
            out.setdefault(atual, [])
        elif atual is not None:
            out[atual].append(linha)
    return out


def achados_da_suite(alvo: dict, r: dict) -> list[dict]:
    evidencia_suite = {"arquivo": f"suites/{alvo['nome']}.json", "legenda": "relatório completo da suíte",
                       "embutir": False}
    base = {"prioridade": None, "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
            "evidencias": [evidencia_suite], "causa_provavel": None, "confianca": "confirmado", "fonte": "suite",
            "passos": ["MAW_APP.exe --run-tests (build Debug)"]}
    out = []
    detalhes_por_bloco = _detalhes_por_bloco(r.get("detalhes", []))
    for b in r["blocos"]:
        if b["falhas"]:
            det = detalhes_por_bloco.get((b["nome"], b["sub"]), [])[:6]
            out.append({**base, "titulo": f"Teste da suíte falha: {b['nome']} → {b['sub']}", "tipo": "erro",
                        "severidade": "alta", "item_catalogo": "saude/suite-existente", "principio": "P6",
                        "esperado": "o bloco passa", "obtido": f"{b['falhas']} verificação(ões) falharam. " + " ".join(det),
                        "sugestao": "corrigir o código (não o teste) até o bloco passar",
                        "criterio_aceite": f"o bloco '{b['nome']} → {b['sub']}' passa na suíte",
                        "assinatura": f"suite:{b['nome']}|{b['sub']}"})
    for inc in r.get("incoerencias", []):
        out.append({**base, "titulo": f"Relatório da suíte incoerente: {inc[:60]}", "tipo": "violacao",
                    "severidade": "alta", "item_catalogo": "saude/suite-existente", "principio": "P4",
                    "esperado": "código de saída, linha RESULTADO e totais concordam", "obtido": inc,
                    "sugestao": "fazer o relatório e o código de saída dizerem a mesma coisa",
                    "criterio_aceite": "a suíte roda e o relatório é coerente com o código de saída",
                    "assinatura": f"suite-incoerente:{inc}"})
    for a in sorted(set(r.get("assercoes", []))):
        out.append({**base, "titulo": f"jassert disparado durante a suíte: {a[:70]}", "tipo": "erro",
                    "severidade": "media", "item_catalogo": "saude/suite-existente", "principio": "P6",
                    "esperado": "nenhuma asserção do JUCE dispara", "obtido": a,
                    "sugestao": "investigar a condição da asserção; ela indica uso fora do contrato",
                    "criterio_aceite": "a suíte no Debug roda sem 'JUCE Assertion failure'",
                    "assinatura": f"jassert:{a}"})
    return out


def resultados_suite_por_item(itens: list[dict], blocos: list[dict]) -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    for it in itens:
        prefixos = [c[len("suite:"):] for c in it.get("cenarios", []) if c.startswith("suite:")]
        if not prefixos:
            continue
        casados = [b for b in blocos for p in prefixos if b["nome"].startswith(p)]
        if not casados:
            out[it["id"]] = ("nao_testavel", f"bloco da suíte não encontrado: {', '.join(prefixos)}")
        elif any(b["falhas"] for b in casados):
            out[it["id"]] = ("falhou", None)
        else:
            out[it["id"]] = ("passou", None)
    return out


def separar_validos(brutos: list[tuple[str, dict]]) -> tuple[list[dict], list[str]]:
    """Portão de validação da consolidação: separa achados brutos (identificados por
    'arquivo[i]', vindos do automático ou de subagentes) válidos dos inválidos, e formata
    uma mensagem por inválido para `erros_agente.json`."""
    validos: list[dict] = []
    mensagens: list[str] = []
    for origem, a in brutos:
        erros = achados.validar(a)
        if erros:
            mensagens.append(f"achado inválido em {origem}: {erros}")
        else:
            validos.append(a)
    return validos, mensagens


# ---------- ações ----------

def _cfg_sprint(p: argparse.ArgumentParser) -> None:
    p.add_argument("acao", choices=["iniciar", "preflight", "preparar", "compilar", "suite",
                                    "consolidar", "encerrar", "relatorio", "status"])
    p.add_argument("--alvo")
    p.add_argument("--nova", action="store_true")


@registrar("sprint", "executa uma fase da sprint", _cfg_sprint)
def cmd_sprint(args: argparse.Namespace) -> int:
    return {"iniciar": _iniciar, "preflight": _preflight, "preparar": _preparar, "compilar": _compilar,
            "suite": _suite, "consolidar": _consolidar, "encerrar": _encerrar, "relatorio": _relatorio,
            "status": _status}[args.acao](args)


def _iniciar(args) -> int:
    e = None if args.nova else estado.em_andamento()
    retomada = e is not None
    e = e or estado.nova_sprint()
    return _saida({"sprint": e.nome, "pasta": str(e.pasta), "retomada": retomada,
                   "passos_concluidos": sorted(k for k in e.passos if e.feito(k))})


def _preflight(args) -> int:
    e = _sprint_atual()
    e.iniciar("preflight")
    vs = preflight.verificar_tudo()
    sandbox.escrever_json(e.pasta / "preflight.json", [v.como_dict() for v in vs])
    sandbox.escrever_json(e.pasta / "ambiente.json", preflight.ambiente())
    bloqueios = [v.detalhe for v in vs if v.bloqueia and not v.ok]
    if bloqueios:
        e.falhar("preflight", "; ".join(bloqueios))
        return _saida({"ok": False, "bloqueios": bloqueios}, False)
    e.concluir("preflight", {"requisitos_ausentes": sorted(preflight.requisitos_ausentes(vs))})
    return _saida({"ok": True, "requisitos_ausentes": sorted(preflight.requisitos_ausentes(vs))})


def _preparar(args) -> int:
    e = _sprint_atual()
    if e.feito("preparar"):
        return _saida({"ok": True, "retomado": True, **(e.dados("preparar") or {})})
    e.iniciar("preparar")
    protegidas = config.pastas_protegidas()
    sandbox.escrever_json(e.pasta / "prova-antes.json", alvos.prova_intocada(protegidas))
    esp = alvos.garantir_espelho()
    locais = alvos.importar_clones_locais(esp, protegidas)
    lista, avisos = alvos.descobrir_alvos(esp, locais)
    sandbox.escrever_json(e.pasta / "alvos.json", {"alvos": [a.como_dict() for a in lista], "avisos": avisos})
    for a in lista:
        if a.compartilha_com is None:
            alvos.criar_worktree(esp, a)
    bk = sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
    detalhe = {"alvos": [a.nome for a in lista], "avisos": avisos, "backup": str(bk)}
    e.concluir("preparar", detalhe)
    return _saida({"ok": True, **detalhe})


def _compilar(args) -> int:
    e = _sprint_atual()
    ok_geral = True
    resumo = []
    for a in _alvos(e, args.alvo):
        if a["compartilha_com"]:
            continue
        wt = config.ALVOS_DIR / a["nome"]
        for cfg in ("Release", "Debug"):
            passo = f"compilar:{a['nome']}:{cfg}"
            if e.feito(passo):
                resumo.append({"alvo": a["nome"], "config": cfg, "retomado": True})
                continue
            e.iniciar(passo)
            r = build.compilar(a["nome"], wt, cfg, e.pasta / "evidencias" / "builds")
            sandbox.escrever_json(e.pasta / "builds" / f"{a['nome']}-{cfg}.json", r.como_dict())
            catalogo.registrar_resultado(e.pasta, f"saude/build-{cfg.lower()}", a["nome"],
                                         "passou" if r.ok else "falhou", fonte="build")
            if not r.ok:
                ok_geral = False
                sandbox.escrever_json(e.pasta / "achados-brutos" / f"build-{a['nome']}-{cfg}.json",
                                      achado_de_build(a, r.como_dict()))
            e.concluir(passo, {"ok": r.ok, "segundos": r.segundos})
            resumo.append({"alvo": a["nome"], "config": cfg, "ok": r.ok, "segundos": r.segundos,
                           "avisos": len(r.avisos), "erros": len(r.erros)})
    return _saida({"builds": resumo}, ok_geral)


def _suite(args) -> int:
    e = _sprint_atual()
    itens = catalogo.carregar(config.CATALOGO) if config.CATALOGO.exists() else []
    todos = _alvos(e)
    resumo = []
    backup = sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
    try:
        for a in _alvos(e, args.alvo):
            if a["compartilha_com"]:
                continue
            passo = f"suite:{a['nome']}"
            if e.feito(passo):
                continue
            e.iniciar(passo)
            dbg = build.caminho_exe(config.ALVOS_DIR / a["nome"], "Debug")
            rel = build.caminho_exe(config.ALVOS_DIR / a["nome"], "Release")
            cwd = sandbox.criar_pasta(config.WORK / "execucao" / a["nome"])
            if dbg.exists():
                with saude.CapturaDepuracao() as cap:
                    r = suite.rodar_suite(dbg, cwd)
                d = r.como_dict()
                d["assercoes"] = saude.assercoes(saude.filtrar(cap.mensagens))
                d["captura_erro"] = cap.erro
                sandbox.escrever_json(e.pasta / "suites" / f"{a['nome']}.json", d)
                for i, ach in enumerate(achados_da_suite(a, d)):
                    sandbox.escrever_json(e.pasta / "achados-brutos" / f"suite-{a['nome']}-{i:03d}.json", ach)
                catalogo.registrar_resultado(e.pasta, "saude/suite-existente", a["nome"],
                                             "passou" if r.passou and not d["assercoes"] else "falhou", fonte="suite")
                for item, (res, motivo) in resultados_suite_por_item(itens, d["blocos"]).items():
                    catalogo.registrar_resultado(e.pasta, item, a["nome"], res, motivo, fonte="suite")
            else:
                catalogo.registrar_resultado(e.pasta, "saude/suite-existente", a["nome"], "nao_testavel",
                                             "build Debug ausente", fonte="suite")
            if rel.exists():
                b = suite.rodar_benchmark(rel, cwd)
                sandbox.escrever_json(e.pasta / "benchmarks" / f"{a['nome']}.json", b)
                catalogo.registrar_resultado(e.pasta, "saude/benchmark", a["nome"],
                                             "passou" if b["linhas"] and b["exit_code"] == 0 else "falhou",
                                             None if b["linhas"] else "tabela ausente", fonte="benchmark")
            else:
                catalogo.registrar_resultado(e.pasta, "saude/benchmark", a["nome"], "nao_testavel",
                                             "build Release ausente", fonte="benchmark")
            e.concluir(passo)
            resumo.append(a["nome"])
    finally:
        sandbox.restaurar_pasta(backup)
    # alvos que compartilham a árvore herdam as células da origem
    res = catalogo.carregar_resultados(e.pasta)
    for a in todos:
        if a["compartilha_com"]:
            for (item, alvo), r in list(res.items()):
                if alvo == a["compartilha_com"]:
                    catalogo.registrar_resultado(e.pasta, item, a["nome"], r["resultado"],
                                                 f"mesma árvore de código de {alvo}", r.get("achados"), r.get("fonte", ""))
    return _saida({"suites": resumo})


def _cfg_achado(p: argparse.ArgumentParser) -> None:
    p.add_argument("acao", choices=["validar", "registrar"])
    p.add_argument("arquivo")


@registrar("achado", "valida ou registra um achado (JSON) na sprint em andamento", _cfg_achado)
def cmd_achado(args: argparse.Namespace) -> int:
    obj = json.loads(Path(args.arquivo).read_text(encoding="utf-8"))
    lista = obj if isinstance(obj, list) else [obj]
    erros = {i: achados.validar(a) for i, a in enumerate(lista)}
    erros = {i: e for i, e in erros.items() if e}
    if erros or args.acao == "validar":
        return _saida({"ok": not erros, "erros": erros}, not erros)
    e = _sprint_atual()
    destino = e.pasta / "achados-brutos" / Path(args.arquivo).name
    sandbox.escrever_json(destino, obj)
    return _saida({"ok": True, "registrado": str(destino)})


def _consolidar(args) -> int:
    e = _sprint_atual()
    e.iniciar("consolidar")
    brutos_com_origem: list[tuple[str, dict]] = []
    for p in sorted((e.pasta / "achados-brutos").glob("*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        vered = e.pasta / "vereditos" / p.name
        vlista = json.loads(vered.read_text(encoding="utf-8")) if vered.exists() else None
        for i, a in enumerate(obj if isinstance(obj, list) else [obj]):
            if vlista is not None:
                a["veredito"] = vlista[i] if isinstance(vlista, list) else vlista
            brutos_com_origem.append((f"{p.name}[{i}]", a))
    brutos, invalidos = separar_validos(brutos_com_origem)
    if invalidos:
        erros_agente_p = e.pasta / "erros_agente.json"
        existentes = json.loads(erros_agente_p.read_text(encoding="utf-8")) if erros_agente_p.exists() else []
        sandbox.escrever_json(erros_agente_p, existentes + invalidos)
    rever_p = e.pasta / "reverificacoes.json"
    rever = json.loads(rever_p.read_text(encoding="utf-8")) if rever_p.exists() else {}
    for a in brutos:
        v = (a.get("veredito") or {}).get("resultado")
        if v == "provavel":
            a["confianca"] = "provavel"
    derrubados = [{"titulo": a["titulo"], "justificativa": a["veredito"].get("justificativa", "")}
                  for a in brutos if (a.get("veredito") or {}).get("resultado") == "derrubado"]
    hist = achados.carregar_historico(config.HISTORICO)
    lista, hist = achados.consolidar(brutos, hist, f"{e.numero:02d}", rever)
    achados.marcar_introducao(lista)
    sandbox.escrever_json(e.pasta / "achados.json", lista)
    sandbox.escrever_json(e.pasta / "derrubados.json", derrubados)
    achados.salvar_historico(config.HISTORICO, hist)
    resultados = catalogo.carregar_resultados(e.pasta)
    for a in lista:
        if a["estado"] in ("novo", "aberto", "regressao") and a["confianca"] == "confirmado" \
                and a["tipo"] not in ("melhoria", "lacuna"):
            for x in a["alvos"]:
                atual = resultados.get((a["item_catalogo"], x["alvo"]), {})
                ids = sorted(set(atual.get("achados", [])) | {a["id"]})
                catalogo.registrar_resultado(e.pasta, a["item_catalogo"], x["alvo"], "falhou",
                                             atual.get("motivo"), ids, "consolidacao")
                resultados[(a["item_catalogo"], x["alvo"])] = {"item": a["item_catalogo"], "alvo": x["alvo"],
                                                               "resultado": "falhou", "motivo": atual.get("motivo"),
                                                               "achados": ids, "fonte": "consolidacao"}
    e.concluir("consolidar", {"achados": len(lista), "derrubados": len(derrubados), "achados_invalidos": len(invalidos)})
    return _saida({"achados": len(lista), "derrubados": len(derrubados), "achados_invalidos": len(invalidos)})


def _encerrar(args) -> int:
    e = _sprint_atual()
    e.iniciar("encerrar")
    restaurado = False
    bk = (e.dados("preparar") or {}).get("backup")
    erro = None
    if bk:
        try:
            sandbox.restaurar_pasta(Path(bk))
            restaurado = True
        except Exception as ex:  # a falha vai para o PDF, não some
            erro = str(ex)
    antes = json.loads((e.pasta / "prova-antes.json").read_text(encoding="utf-8"))
    depois = alvos.prova_intocada(config.pastas_protegidas())
    sandbox.escrever_json(e.pasta / "prova-depois.json", depois)
    difs = alvos.comparar_provas(antes, depois)
    info = {"verificado": not difs, "diferencas": difs, "ambiente_restaurado": restaurado, "erro_restauracao": erro}
    sandbox.escrever_json(e.pasta / "intocada.json", info)
    e.concluir("encerrar", info)
    return _saida(info, not difs and restaurado)


def _relatorio(args) -> int:
    from .relatorio import contexto, pdf
    e = _sprint_atual()
    e.iniciar("relatorio")
    itens = catalogo.carregar(config.CATALOGO) if config.CATALOGO.exists() else []
    principios = catalogo.carregar(config.PRINCIPIOS) if config.PRINCIPIOS.exists() else []
    logo = config.ALVOS_DIR / "main" / "Source" / "logo_MAW.png"
    ctx = contexto.montar(e.pasta, itens, principios, estado.anterior(e), logo if logo.exists() else None)
    destino = pdf.renderizar(ctx, e.pasta / f"MAW-Sprint-{e.numero:02d}.pdf")
    if (e.pasta / "achados.json").exists():
        pdf.anexar(destino, e.pasta / "achados.json", "achados.json")
    e.concluir("relatorio", {"pdf": str(destino)})
    return _saida({"pdf": str(destino)})


def _status(args) -> int:
    e = estado.em_andamento()
    if e is None:
        return _saida({"em_andamento": None})
    return _saida({"sprint": e.nome, "passos": e.passos})
