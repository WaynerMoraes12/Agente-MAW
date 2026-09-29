"""Lê a pasta da sprint e monta o dicionário que o modelo HTML usa. Nada é inventado:
arquivo ausente vira limitação declarada."""
from __future__ import annotations
import base64
import json
import time
from collections import Counter
from pathlib import Path

from .. import calibracao, catalogo, config
from ..achados import ESTADOS_TERMINAIS
from ..estado import FASES, FINAL

# alvo_removido (a branch do achado foi apagada) não está aberto: fica fora das fichas e do semáforo
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
    if abertos:
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


def _concluido(passos: dict, nome: str) -> bool:
    return passos.get(nome, {}).get("status") == "concluido"


def _limitacoes_das_fases(passos: dict) -> list[str]:
    """Fases do fluxo nunca concluídas e passos que ficaram em andamento (interrompidos). O passo
    final (o relatório) está em andamento por definição enquanto o PDF é gerado."""
    out = [f"fase {f} não concluída" for f in FASES if f != FINAL and not _concluido(passos, f)]
    out += [f"passo {p} não concluído (interrompido)" for p, info in passos.items()
            if p != FINAL and info.get("status") == "em_andamento"]
    return out


def _limitacoes_dos_agentes(pasta: Path, erros_agente: list[str]) -> list[str]:
    """Cada `limitacoes-<agente>.json` é uma lista de strings: o que o agente não conseguiu verificar."""
    out = []
    for arq in sorted(pasta.glob("limitacoes-*.json")):
        agente = arq.stem[len("limitacoes-"):]
        try:
            lista = json.loads(arq.read_text(encoding="utf-8"))
            if not isinstance(lista, list):
                raise ValueError("não é uma lista de textos")
        except (OSError, ValueError) as ex:
            erros_agente.append(f"limitações ilegíveis em {arq.name}: {ex}")
            continue
        # as da própria CLI (suíte, ambiente) já dizem de onde vêm; as dos subagentes levam o nome
        out += [str(l) if agente in ("suite", "ambiente") else f"{agente}: {l}" for l in lista]
    return out


def _prova(pasta: Path) -> list[dict]:
    """Tabela da prova de intocada: pasta, HEAD antes/depois (ou a contagem de arquivos) e se ficou igual."""
    antes = _ler(pasta / "prova-antes.json", {})
    depois = _ler(pasta / "prova-depois.json", {})

    def resumo(p: dict | None) -> str:
        if p is None:
            return "ausente"
        if p.get("tipo") == "arquivos":
            return f"sem git: {len(p.get('arquivos', []))} arquivo(s)"
        return (p.get("head") or "?")[:10]

    return [{"pasta": nome, "antes": resumo(antes.get(nome)), "depois": resumo(depois.get(nome)),
             "igual": antes.get(nome) == depois.get(nome)} for nome in sorted(set(antes) | set(depois))]


def _metodo(estado: dict) -> list[str]:
    """Um texto por backup do %APPDATA%\\MAW, pelo desfecho final (a última restauração dele)."""
    por_backup: dict[str, list[dict]] = {}
    for r in estado.get("restauracoes", []):
        por_backup.setdefault(str(r.get("backup")), []).append(r)
    out = []
    for backup, regs in por_backup.items():
        r = regs[-1]
        falhas = sum(1 for x in regs[:-1] if not x.get("verificado"))
        antes = f", depois de {falhas} tentativa(s) de restauração com falha" if falhas else ""
        quando = r.get("quando", "?")
        if r.get("descartado"):
            out.append(f"Um backup temporário das configurações da MAW ({backup}) foi apagado sem ser restaurado "
                       f"({quando}): {r['descartado']}.")
        elif r.get("adiada"):
            out.append(f"O backup temporário das configurações da MAW foi mantido em {backup}: a restauração foi "
                       f"adiada porque a MAW estava aberta ({quando}); feche a MAW e rode `sprint iniciar` de novo.")
        elif r.get("backup_apagado"):
            out.append(f"Um backup temporário das configurações da MAW (%APPDATA%\\MAW) existiu durante a execução "
                       f"({r.get('quem', '?')}) e foi apagado depois da restauração verificada ({quando}{antes}).")
        else:
            motivo = r.get("erro") or r.get("erro_ao_apagar") or "motivo não informado"
            out.append(f"O backup temporário das configurações da MAW foi mantido em {backup} "
                       f"porque a restauração ou a remoção falhou ({quando}): {motivo}.")
    return out


ESTADOS_BANCADA = ("passou", "problema", "pulei", "sem resultado")


def _ler_objeto(p: Path, erros_agente: list[str], rotulo: str) -> dict | None:
    """JSON que tem de ser um objeto; ausente → None; ilegível → None e uma linha nos erros do agente."""
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("não é um objeto JSON")
        return d
    except (OSError, ValueError) as ex:
        erros_agente.append(f"{rotulo} ilegível: {ex}")
        return None


def _bancada(pasta: Path, pasta_bancada: Path, erros_agente: list[str]) -> dict:
    """Cada teste da bancada do usuário (`work/bancada/testes/<código>.json`) com o estado do agente
    nesta sprint (`bancada.json`: passou/problema/pulei; ausente = "sem resultado") e a nota, por seção
    (ordem e títulos de `meta/secoes.json`), com contagens. O que faltar vira uma linha, nunca uma
    seção vazia."""
    testes_dir = pasta_bancada / "testes"
    testes: list[dict] = []
    for arq in sorted(testes_dir.glob("*.json")) if testes_dir.is_dir() else []:
        d = _ler_objeto(arq, erros_agente, f"teste da bancada {arq.name}")
        if d is None:
            continue
        ordem = d.get("ordem") if isinstance(d.get("ordem"), int) else 10 ** 6
        testes.append({"codigo": arq.stem, "titulo": str(d.get("titulo") or arq.stem),
                       "secao": str(d.get("secao") or "sem seção"), "ordem": ordem})
    linhas: list[str] = []
    resultados = _ler_objeto(pasta / "bancada.json", erros_agente, "bancada.json")
    if not testes:
        linhas.append("A bancada não pôde ser listada: a lista de testes (work/bancada/testes) não foi encontrada "
                      "ou está vazia.")
    if resultados is None:
        linhas.append("A bancada não rodou nesta sprint: nenhum resultado do agente (bancada.json ausente ou "
                      "ilegível); todos os testes ficam sem resultado.")
    resultados = resultados or {}
    meta = _ler_objeto(pasta_bancada / "meta" / "secoes.json", erros_agente, "meta/secoes.json da bancada") or {}
    titulos: dict[str, str] = {}
    for s in meta.get("lista") or []:
        if isinstance(s, dict) and s.get("id"):
            titulos.setdefault(str(s["id"]), str(s.get("titulo") or s["id"]))
    ordem_secoes = list(titulos) + sorted({t["secao"] for t in testes} - set(titulos))
    vazio = {e: 0 for e in ESTADOS_BANCADA}
    total = dict(vazio)
    secoes = []
    for sid in ordem_secoes:
        doid = sorted((t for t in testes if t["secao"] == sid), key=lambda t: (t["ordem"], t["codigo"]))
        if not doid:
            continue
        cont = dict(vazio)
        linhas_secao = []
        for t in doid:
            r = resultados.get(t["codigo"])
            r = r if isinstance(r, dict) else {}
            estado = r.get("estado") if r.get("estado") in ESTADOS_BANCADA[:3] else "sem resultado"
            if r and estado == "sem resultado":
                erros_agente.append(f"bancada.json: estado inválido para {t['codigo']}: {r.get('estado')!r}")
            cont[estado] += 1
            total[estado] += 1
            linhas_secao.append({"codigo": t["codigo"], "titulo": t["titulo"], "estado": estado,
                                 "classe": estado.replace(" ", "_"), "nota": str(r.get("nota") or ""),
                                 "alvo": str(r.get("alvo") or "")})
        secoes.append({"id": sid, "titulo": titulos.get(sid, sid), "testes": linhas_secao, "contagens": cont})
    fora = sorted(set(resultados) - {t["codigo"] for t in testes})
    if fora and testes:
        linhas.append(f"Resultados do agente para código(s) fora da lista da bancada: {', '.join(fora)}.")
    return {"secoes": secoes, "contagens": total, "total": len(testes), "linhas": linhas}


def _calibracao(pasta: Path, passos: dict, erros_agente: list[str]) -> dict:
    """Taxa de detecção da calibração desta sprint (`calibracao.json`) e, para cada defeito
    reinjetado, se foi detectado e por quais blocos. Sem calibração: uma linha dizendo que não rodou."""
    passo = passos.get("calibrar") or {}
    vazio = {"rodou": False, "linhas": [], "defeitos": [], "taxa": None, "texto_taxa": ""}
    p = pasta / "calibracao.json"
    if not p.exists():
        linha = "A calibração não rodou nesta sprint (calibracao.json ausente)."
        if passo.get("status") == "falhou":
            linha += f" O passo calibrar falhou: {passo.get('erro') or 'sem mensagem de erro'}."
        return {**vazio, "linhas": [linha]}
    res = _ler_objeto(p, erros_agente, "calibracao.json")
    if res is not None and not all(isinstance(r, dict) for r in res.values()):
        erros_agente.append("calibracao.json ilegível: algum defeito não é um objeto JSON")
        res = None
    if res is None:
        return {**vazio, "linhas": ["A calibração não rodou até o fim: calibracao.json está ilegível (ver os erros "
                                    "do agente)."]}
    t = calibracao.taxa(res)
    if t["validos"]:
        texto = f"{t['detectados']} de {t['validos']} defeito(s) reinjetado(s) detectado(s) ({t['taxa'] * 100:.0f}%)"
    else:
        texto = "nenhum defeito medido com validade: sem taxa"
    if t["erros"]:
        texto += f"; {t['erros']} defeito(s) fora da taxa por erro da calibração"
    defeitos = []
    for id_, r in res.items():
        situacao = "erro" if r.get("erro") else ("detectado" if r.get("detectado") else "não detectado")
        defeitos.append({"id": id_, "origem": str(r.get("origem") or ""), "situacao": situacao,
                         "classe": {"não detectado": "nao_detectado"}.get(situacao, situacao),
                         "blocos": [str(b) for b in r.get("blocos_que_falharam") or []]
                                   + [f"incoerência: {i}" for i in r.get("incoerencias") or []],
                         "erro": r.get("erro"), "como_esperado": r.get("como_esperado")})
    linhas = []
    if passo.get("status") != "concluido":
        linhas.append("Resultado parcial: o passo calibrar não terminou nesta sprint.")
    if not res:
        linhas.append("calibracao.json não tem nenhum defeito medido.")
    controle = (passo.get("detalhe") or {}).get("controle") or {}
    if controle.get("erro"):
        linhas.append(f"Controle (a mesma árvore sem defeito) inválido: {controle['erro']}.")
    if controle.get("blocos_que_falharam"):
        linhas.append(f"Controle (a mesma árvore sem defeito): {len(controle['blocos_que_falharam'])} bloco(s) já "
                      f"falhavam e foram descontados: {'; '.join(controle['blocos_que_falharam'])}.")
    return {"rodou": True, "linhas": linhas, "defeitos": defeitos, "taxa": t, "texto_taxa": texto}


def montar(pasta: Path, itens: list[dict], principios: list[dict], anterior: Path | None,
           logo: Path | None, pasta_bancada: Path | None = None) -> dict:
    """`pasta_bancada`: a pasta da bancada do usuário (padrão `work/bancada`)."""
    pasta = Path(pasta)
    pasta_bancada = Path(pasta_bancada) if pasta_bancada is not None else config.WORK / "bancada"
    estado = _ler(pasta / "estado.json", {"numero": 0, "passos": {}})
    passos = estado.get("passos", {})
    alvos_info = _ler(pasta / "alvos.json", {"alvos": [], "avisos": []})
    achados_lista = _ler(pasta / "achados.json", [])
    for a in achados_lista:
        for ev in a.get("evidencias", []):
            arq = Path(ev["arquivo"]) if Path(ev["arquivo"]).is_absolute() else pasta / ev["arquivo"]
            if ev.get("embutir") and arq.suffix.lower() in (".png", ".jpg", ".jpeg") and arq.exists():
                tipo = "png" if arq.suffix.lower() == ".png" else "jpeg"
                ev["data_uri"] = f"data:image/{tipo};base64," + base64.b64encode(arq.read_bytes()).decode()
        # "Nota da verificação" na ficha: a justificativa de quem julgou (confirmado ou provável),
        # até 700 caracteres. Sem veredito legível (mecânico ou não revisado), não há nota.
        justificativa = (a.get("veredito") or {}).get("justificativa")
        a["nota_verificacao"] = justificativa[:700] if justificativa else None
    intocada = _ler(pasta / "intocada.json", {"verificado": False, "diferencas": ["prova não registrada"],
                                              "ambiente_restaurado": False})
    textos = _ler(pasta / "textos.json", {})
    resumo = textos.get("resumo")
    erros_agente = _ler(pasta / "erros_agente.json", [])
    limitacoes: list[str] = list(alvos_info.get("avisos", []))
    limitacoes += _limitacoes_das_fases(passos)
    achados_anexados = _concluido(passos, "consolidar") and (pasta / "achados.json").exists()
    if not _concluido(passos, "consolidar"):
        limitacoes.append("achados não consolidados nesta execução")
    sem_verificacao = (passos.get("consolidar", {}).get("detalhe") or {}).get("sem_verificacao_adversarial", 0)
    if sem_verificacao:
        limitacoes.append(f"{sem_verificacao} achado(s) sem verificação adversarial (veredito ausente ou "
                          "ilegível): entraram como prováveis")
    if not resumo:
        limitacoes.append("resumo executivo não redigido nesta execução")
        resumo = ""
    for v in _ler(pasta / "preflight.json", []):
        if not v["ok"]:
            limitacoes.append(f"Pré-voo: {v['detalhe']}")
    limitacoes += _limitacoes_dos_agentes(pasta, erros_agente)
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
        herdados = []
        if a.get("compartilha_com"):
            # mesma árvore de código: os achados de código da origem valem aqui também; os de
            # documentação não, porque a documentação é revisada por alvo
            herdados = [x for x in achados_lista if x not in doalvo
                        and origem in {y["alvo"] for y in x["alvos"]}
                        and not x.get("item_catalogo", "").startswith("documentacao/")]
        combinados = doalvo + herdados
        # a situação (semáforo) de uma branch só olha o que ela traz além do main: achados que
        # também afetam o main não são "culpa" da branch. O próprio main continua vendo tudo.
        achados_para_semaforo = combinados if a["nome"] == "main" \
            else [x for x in combinados if "main" not in {y["alvo"] for y in x["alvos"]}]
        sem = semaforo(achados_para_semaforo, build_ok)
        suite_alvo = _ler(pasta / "suites" / f"{origem}.json")
        if suite_alvo and not a.get("compartilha_com"):
            if suite_alvo.get("captura_erro"):
                limitacoes.append(f"jassert não capturado no alvo {a['nome']}: {suite_alvo['captura_erro']}")
            limitacoes += [f"{a['nome']}: aviso antes da suíte: {l}" for l in suite_alvo.get("preambulo", [])]
        texto_alvo = textos.get("por_alvo", {}).get(a["nome"])
        if not texto_alvo:
            limitacoes.append(f"texto do alvo {a['nome']} não redigido nesta execução")
            texto_alvo = ""
        alvos.append({**a, "semaforo": sem,
                      "semaforo_rotulo": _ROTULO_SEMAFORO[sem],
                      "build": b_rel, "build_debug": b_dbg,
                      "suite": suite_alvo,
                      "benchmark": _ler(pasta / "benchmarks" / f"{origem}.json"),
                      "achados_ids": [x["id"] for x in doalvo + herdados],
                      "herdados": [x["id"] for x in herdados],
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
    # fichas para o agente de correção: só o que está aberto; corrigidos e de alvo removido ficam só nas
    # suas listas curtas
    problemas = [a for a in achados_lista if a["tipo"] != "melhoria" and a["estado"] not in ESTADOS_TERMINAIS]
    melhorias = [a for a in achados_lista if a["tipo"] == "melhoria" and a["estado"] not in ESTADOS_TERMINAIS]
    provaveis = [a for a in achados_lista if a.get("confianca") == "provavel"]
    if provaveis:
        limitacoes.append(f"{len(provaveis)} achado(s) marcado(s) como provável(is): evidentes no código, não reproduzidos dinamicamente")
    derrubados = _ler(pasta / "derrubados.json", [])
    for passo, info in passos.items():
        if info.get("status") == "falhou":
            erros_agente.append(f"passo {passo} falhou: {info.get('erro') or 'sem mensagem de erro'}")
    abertos = [a for a in problemas if a["estado"] in ABERTOS]
    principios_ctx = []
    for p in principios:
        cels = [m.get(f"principio/{p['id']}", {}).get(n, {"resultado": "nao_testavel", "motivo": "sem item na matriz", "achados": []})
                for n in nomes]
        principios_ctx.append({**p, "celulas": cels,
                               "violacoes": [a["id"] for a in achados_lista if a.get("principio") == p["id"]
                                             and a["estado"] != "alvo_removido"]})
    logo_uri = None
    if logo and Path(logo).exists():
        logo_uri = "data:image/png;base64," + base64.b64encode(Path(logo).read_bytes()).decode()
    return {
        "sprint": f"sprint-{estado['numero']:02d}",
        # o agente se chama Roadie; o arquivo do PDF e os nomes do pacote/CLI continuam os mesmos
        "titulo": f"Roadie — relatório da Sprint {estado['numero']:02d}",
        "subtitulo": "agente de testes da MAW",
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
        "alvo_removido": [a for a in achados_lista if a["estado"] == "alvo_removido"],
        "principios": principios_ctx,
        "matriz": {"alvos": nomes, "linhas": linhas},
        "cobertura": cobertura,
        "limitacoes": limitacoes,
        "derrubados": derrubados,
        "erros_agente": erros_agente,
        "intocada": intocada,
        "prova": _prova(pasta),
        "metodo": _metodo(estado),
        "achados_anexados": achados_anexados,
        "ambiente": _ler(pasta / "ambiente.json", {}),
        "preflight": _ler(pasta / "preflight.json", []),
        "anterior": anterior.name if anterior else None,
        "logo_data_uri": logo_uri,
        "bancada": _bancada(pasta, pasta_bancada, erros_agente),
        "calibracao": _calibracao(pasta, passos, erros_agente),
    }
