"""Fase `e2e`: descobre e roda os cenários de `privado/cenarios/**/*.py`, em série por alvo (main
primeiro), com timeout por cenário, filtro por cenário/arquivo, prazo e retomada por
(alvo, cenário). Também roda `--avulso`, fora de qualquer sprint. Não edita `fases.py` (Task 1 é
dona do arquivo): reusa `fases._sprint_atual`/`fases._alvos` e o resto vem de `catalogo`, `config`,
`e2e`, `sandbox`.
"""
from __future__ import annotations
import argparse
import fnmatch
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import bancada, build, cache_local, catalogo, config, e2e, fases, sandbox
from .cli import registrar


def _saida(obj: dict, ok: bool = True) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0 if ok else 1


_RX_HORA = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _hora_valida(texto: str) -> str:
    """'HH:MM' (0–23, 0–59) normalizado com dois dígitos; tipo do argparse para `--limite`."""
    m = _RX_HORA.fullmatch(str(texto).strip())
    if not m:
        raise argparse.ArgumentTypeError(f"hora inválida: {texto!r} (use HH:MM, ex.: 06:00)")
    return f"{int(m[1]):02d}:{m[2]}"


def _cfg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--alvo", help="roda só este alvo (padrão: todos os alvos da sprint que não "
                                  "compartilham árvore com outro; em --avulso, 'main')")
    p.add_argument("--cenario", action="append", default=[], metavar="ID_OU_GLOB",
                  help="roda só cenários cujo id casa com este padrão fnmatch (repetível)")
    p.add_argument("--arquivo", action="append", default=[], metavar="NOME.py",
                  help="roda só cenários deste arquivo, pelo nome (repetível)")
    p.add_argument("--avulso", action="store_true",
                  help="roda fora de qualquer sprint: não grava resultados.jsonl, achados, "
                       "bancada.json nem passo nenhum; evidências em work/e2e-avulso/<carimbo>/")
    p.add_argument("--limite", type=_hora_valida, metavar="HH:MM",
                  help="para de começar cenários novos depois deste horário (hoje, ou amanhã se já "
                       "passou); os que sobrarem viram uma limitação, não um resultado")
    p.add_argument("--oculto", action="store_true",
                  help="roda no desktop oculto: nada aparece na tela do usuário (a fase se relança lá "
                       "dentro, sem teclado/mouse reais, e repassa a saída e o código de saída)")


_RESULTADO_CATALOGO = {"passou": "passou", "problema": "falhou", "pulei": "nao_testavel"}


def _slug(texto: str) -> str:
    """Um `id` de cenário pode ter '/' (ex.: 'projeto/relink'); nomes de arquivo não podem."""
    return texto.replace("/", "-").replace("\\", "-").replace(" ", "_")


def _cenarios_aplicaveis(cenarios: list[e2e.Cenario], nome_alvo: str) -> list[e2e.Cenario]:
    return [c for c in cenarios if c.alvos == "todos" or nome_alvo in c.alvos]


def _filtrar_cenarios(cenarios: list[e2e.Cenario], padroes_id: list[str],
                      arquivos: list[str]) -> list[e2e.Cenario]:
    """`--cenario` (fnmatch no `id`) e `--arquivo` (nome exato do arquivo) — cada um só filtra se
    foi passado; os dois juntos exigem os dois (E, não OU)."""
    saida = cenarios
    if padroes_id:
        saida = [c for c in saida if any(fnmatch.fnmatch(c.id, p) for p in padroes_id)]
    if arquivos:
        saida = [c for c in saida if c.arquivo is not None and c.arquivo.name in arquivos]
    return saida


def _ordenar_alvos(alvos: list[dict]) -> list[dict]:
    """`main` primeiro — é o alvo da bancada e do achado do usuário; o resto mantém a ordem que já
    tinha (`sorted` é estável)."""
    return sorted(alvos, key=lambda a: a["nome"] != "main")


def _requisitos_extra() -> set[str]:
    """Requisitos que a própria fase decide, por variável de ambiente — não vêm do preflight:
    'som' e 'entrada_real' são ausentes (bloqueiam cenários que os pedem) a menos que ligados
    explicitamente, para nunca tocar som alto ou mexer no teclado/mouse de verdade durante o dia."""
    extra: set[str] = set()
    if os.environ.get("MAW_AGENTE_SOM") != "1":
        extra.add("som")
    if os.environ.get("MAW_AGENTE_ENTRADA_REAL") != "1":
        extra.add("entrada_real")
    if os.environ.get("MAW_AGENTE_MICROFONE") != "1":  # gravar pelo microfone: só à noite
        extra.add("microfone")
    return extra


def _requisito_loopback_avulso() -> set[str]:
    """Fora de uma sprint não há preflight.json: consulta a mesma checagem direto. Nunca levanta —
    se falhar por qualquer motivo, 'loopback' vira ausente (mais seguro que travar o --avulso)."""
    try:
        from . import preflight
        tem = preflight._loopback_padrao()
    except Exception:
        tem = None
    return set() if tem else {"loopback"}


def _agora_dt() -> datetime:
    return datetime.now()


def _prazo(agora: datetime, limite: str | None) -> datetime | None:
    """A próxima ocorrência de `limite` (HH:MM) depois de `agora` — hoje, ou amanhã se já passou."""
    if not limite:
        return None
    h, m = (int(x) for x in limite.split(":"))
    prazo = agora.replace(hour=h, minute=m, second=0, microsecond=0)
    return prazo if prazo > agora else prazo + timedelta(days=1)


def _alvo_para_bancada(c: e2e.Cenario, nome_alvo: str) -> str | None:
    """A bancada do usuário é sobre o alvo `main`: um cenário que mira `main` (`alvos="todos"` ou
    `main` na lista) só alimenta a bancada quando é a própria execução em `main`; as execuções
    dele nas outras branches não contam (main já fala por elas). Um cenário que mira só branches
    (os itens `onde: proxima`, ex.: as branches de uma feature nova) alimenta a bancada com o alvo que de fato
    rodou, porque não há execução em `main` para esse cenário."""
    if e2e.cenario_mira_main(c):
        return nome_alvo if nome_alvo == "main" else None
    return nome_alvo


def _acrescentar_limitacoes(e, linhas: list[str]) -> None:
    """`limitacoes-e2e.json`: erro do agente e thread presa vão aqui, nunca para as fichas
    (regra 5 do CLAUDE.md) — mesma forma de `fases._acrescentar_limitacoes`, que não reuso por não
    editar `fases.py`."""
    if not linhas:
        return
    p = e.pasta / "limitacoes-e2e.json"
    atuais = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    novas = atuais + [l for l in linhas if l not in atuais]
    if novas != atuais:
        sandbox.escrever_json(p, novas)


def _registrar_resultado(pasta_sprint: Path, c: e2e.Cenario, alvo: dict, resultado: e2e.Resultado,
                         evidencias: list[dict]) -> None:
    motivo = (resultado.nota or None) if resultado.estado != "passou" else None
    for item in c.itens:
        catalogo.registrar_resultado(pasta_sprint, item, alvo["nome"], _RESULTADO_CATALOGO[resultado.estado],
                                     motivo, fonte="e2e")
    if resultado.estado == "problema":
        achado = e2e.achado_de_problema(c, alvo, resultado, evidencias)
        sandbox.escrever_json(pasta_sprint / "achados-brutos" / f"e2e-{alvo['nome']}-{_slug(c.id)}.json", achado)


_EXTENSOES_AUDIO = {".wav", ".flac", ".ogg", ".mp3", ".aif", ".aiff"}


def _varrer_gravacoes(raiz: Path) -> int:
    """Apaga (pelo sandbox) todo áudio dentro de pastas `*_Audio` sob `raiz` — onde a MAW põe as tomadas
    do projeto salvo nas evidências. O som da sala gravado pelo microfone nunca fica guardado, nem quando o
    cenário caiu antes do próprio `finally`. Devolve quantos arquivos apagou; nunca levanta."""
    apagados = 0
    try:
        arquivos = [a for a in Path(raiz).rglob("*") if a.is_file() and a.suffix.lower() in _EXTENSOES_AUDIO
                    and any(p.name.endswith("_Audio") for p in a.parents)]
    except OSError:
        return 0
    for a in arquivos:
        try:
            sandbox.remover(a)
            apagados += 1
        except Exception:
            pass
    return apagados


def _binario_da_sprint(e, nome: str) -> tuple[bool, str]:
    """O Release que a E2E vai abrir é o desta sprint? (mesma regra da suíte: `fases.binario_valido`).
    Sem isso, um build que falhou ou foi cortado faria a E2E testar o exe de um commit anterior e
    atribuir o resultado ao commit novo."""
    exe = build.caminho_exe(config.ALVOS_DIR / nome, "Release")
    return fases.binario_valido(fases._ler_json(e.pasta / "builds" / f"{nome}-Release.json"), exe,
                                (e.passos.get(f"compilar:{nome}:Release") or {}).get("inicio"))


def _detalhe_do_passo(c: e2e.Cenario, resultado: e2e.Resultado, alvo_bancada: str | None) -> dict:
    """O que fica no passo `e2e:<alvo>:<id>`: o resultado e, para a consolidação refazer a matriz e a bancada
    quando o achado do cenário é derrubado, os itens do catálogo e os códigos da bancada que ele alimentou
    (`bancada_alvo`: o alvo com que entrou na bancada, ou None quando não entrou)."""
    return {"estado": resultado.estado, "nota": resultado.nota, "itens": list(c.itens), "bancada": list(c.bancada),
            "bancada_alvo": alvo_bancada}


def _pular_sem_binario(e, c: e2e.Cenario, alvo: dict, motivo: str) -> dict:
    """Cenário de um alvo sem o Release desta sprint: `pulei` com o motivo, célula não testável, nenhum
    achado. Na bancada entra como `pulei` com o mesmo motivo (o usuário vê por que não rodou)."""
    nome = alvo["nome"]
    passo = f"e2e:{nome}:{c.id}"
    if e.feito(passo):
        return {"alvo": nome, "cenario": c.id, "retomado": True, **(e.dados(passo) or {})}
    resultado = e2e.Resultado("pulei", nota=f"o build Release desta sprint não ficou pronto para {nome}: {motivo}")
    _registrar_resultado(e.pasta, c, alvo, resultado, [])
    alvo_bancada = _alvo_para_bancada(c, nome)
    if alvo_bancada is not None:
        for codigo in c.bancada:
            bancada.registrar(e.pasta, codigo, resultado.estado, resultado.nota, alvo_bancada)
    detalhe = _detalhe_do_passo(c, resultado, alvo_bancada)
    e.concluir(passo, detalhe)
    return {"alvo": nome, "cenario": c.id, "estado": detalhe["estado"], "nota": detalhe["nota"]}


def _rodar_um(e, c: e2e.Cenario, alvo: dict, requisitos_ausentes: set[str]) -> dict:
    nome = alvo["nome"]
    passo = f"e2e:{nome}:{c.id}"
    if e.feito(passo):
        return {"alvo": nome, "cenario": c.id, "retomado": True, **(e.dados(passo) or {})}
    e.iniciar(passo)
    evidencias = sandbox.criar_pasta(e.pasta / "evidencias" / nome / _slug(c.id))
    ctx = e2e.Contexto(alvo=nome, exe_release=build.caminho_exe(config.ALVOS_DIR / nome, "Release"),
                       pasta_alvo=config.ALVOS_DIR / nome, fixtures=config.WORK / "fixtures",
                       evidencias=evidencias, estado=e)
    resultado = e2e.rodar_cenario(c, ctx, requisitos_ausentes)
    if "microfone" in c.requisitos:
        _varrer_gravacoes(evidencias)
    _registrar_resultado(e.pasta, c, alvo, resultado, ctx.evidencias_registradas)
    _acrescentar_limitacoes(e, resultado.limitacoes)
    alvo_bancada = _alvo_para_bancada(c, nome)
    if alvo_bancada is not None:
        for codigo in c.bancada:
            bancada.registrar(e.pasta, codigo, resultado.estado, resultado.nota, alvo_bancada)
    detalhe = _detalhe_do_passo(c, resultado, alvo_bancada)
    e.concluir(passo, detalhe)
    return {"alvo": nome, "cenario": c.id, "estado": detalhe["estado"], "nota": detalhe["nota"]}


def _propagar_heranca(e) -> None:
    """Alvo que compartilha árvore de código com outro (ex.: uma branch de docs) nunca roda e2e de
    novo — é o mesmo binário. Herda as células de fonte 'e2e' do alvo de origem, como
    `fases._suite` já faz para a suíte (mas só as de 'e2e': não é papel desta fase copiar as de
    outras fontes). Roda sempre, mesmo com `--alvo`: o que já existe em `resultados.jsonl` nesta
    sprint é reaplicado a cada chamada, então a herança se completa assim que a origem tiver
    rodado, mesmo que isso tenha sido numa chamada anterior filtrada.

    Idempotente: lê `resultados.jsonl` uma vez (`res`, no começo) e não regrava uma célula herdada
    que já está lá com o mesmo `fonte`/`resultado`/`motivo` — senão uma segunda chamada na mesma
    sprint (ex.: retomada) acrescentaria uma linha idêntica a cada vez."""
    res = catalogo.carregar_resultados(e.pasta)
    for a in fases._alvos(e):
        origem = a.get("compartilha_com")
        if not origem:
            continue
        for (item, alvo_nome), r in list(res.items()):
            if alvo_nome != origem or r.get("fonte") != "e2e":
                continue
            motivo = f"mesma árvore de código de {origem}" + (f": {r['motivo']}" if r.get("motivo") else "")
            atual = res.get((item, a["nome"]))
            if (atual and atual.get("fonte") == "e2e" and atual.get("resultado") == r["resultado"]
                    and atual.get("motivo") == motivo):
                continue  # já herdado com o mesmo valor: nada a fazer
            catalogo.registrar_resultado(e.pasta, item, a["nome"], r["resultado"], motivo,
                                         r.get("achados"), fonte="e2e")


def _rodar_sprint(args: argparse.Namespace, cenarios_todos: list[e2e.Cenario],
                  cenarios_filtrados: list[e2e.Cenario]) -> int:
    e = fases._sprint_atual()
    # `--alvo`/`--cenario`/`--arquivo` só filtram QUEM RODA agora; o passo agregado 'e2e' precisa
    # de TODOS os alvos e cenários da sprint (como `fases._compilar` faz para 'compilar') — senão
    # uma chamada filtrada fecharia o passo agregado antes do resto ter rodado.
    alvos_todos = [a for a in fases._alvos(e) if not a.get("compartilha_com")]
    alvos_rodar = _ordenar_alvos([a for a in fases._alvos(e, args.alvo) if not a.get("compartilha_com")])
    requisitos_ausentes = set((e.dados("preflight") or {}).get("requisitos_ausentes", [])) | _requisitos_extra()
    prazo = _prazo(_agora_dt(), args.limite)
    resumo: list[dict] = []
    ok = True
    pulados_por_tempo: dict[str, int] = {}
    for a in alvos_rodar:
        vale, motivo_binario = _binario_da_sprint(e, a["nome"])
        if not vale:
            _acrescentar_limitacoes(e, [f"e2e não rodou em {a['nome']}: o build Release desta sprint não ficou "
                                        f"pronto ({motivo_binario})"])
        for c in _cenarios_aplicaveis(cenarios_filtrados, a["nome"]):
            passo = f"e2e:{a['nome']}:{c.id}"
            if not vale:
                resumo.append(_pular_sem_binario(e, c, a, motivo_binario))
                continue
            if not e.feito(passo) and prazo is not None and _agora_dt() >= prazo:
                pulados_por_tempo[a["nome"]] = pulados_por_tempo.get(a["nome"], 0) + 1
                continue
            r = _rodar_um(e, c, a, requisitos_ausentes)
            if r.get("estado") == "problema":
                ok = False
            resumo.append(r)
    _propagar_heranca(e)
    varridos = _varrer_gravacoes(e.pasta / "evidencias")
    if varridos:
        _acrescentar_limitacoes(e, [f"{varridos} arquivo(s) de áudio de gravação apagado(s) das evidências no fim "
                                    f"da E2E (o som da sala nunca fica guardado)"])
    if pulados_por_tempo:
        _acrescentar_limitacoes(e, [f"cenários não rodados por falta de tempo: {alvo}: {n}"
                                    for alvo, n in sorted(pulados_por_tempo.items())])
    todos_passos = [f"e2e:{a['nome']}:{c.id}" for a in alvos_todos
                    for c in _cenarios_aplicaveis(cenarios_todos, a["nome"])]
    if not e.feito("e2e") and all(e.feito(p) for p in todos_passos):
        e.concluir("e2e", {"execucoes": len(todos_passos)})
    return _saida({"execucoes": resumo}, ok)


def _contexto_avulso(pasta_execucao: Path, nome_alvo: str, c: e2e.Cenario) -> e2e.Contexto:
    evidencias = sandbox.criar_pasta(pasta_execucao / nome_alvo / _slug(c.id))
    return e2e.Contexto(alvo=nome_alvo, exe_release=build.caminho_exe(config.ALVOS_DIR / nome_alvo, "Release"),
                        pasta_alvo=config.ALVOS_DIR / nome_alvo, fixtures=config.WORK / "fixtures",
                        evidencias=evidencias)  # sem `estado`: SessaoApp decide sozinha, como de costume


def _relatar_avulso(c: e2e.Cenario, nome_alvo: str, resultado: e2e.Resultado, evidencias: list[dict]) -> dict:
    problema = resultado.estado == "problema"
    return {"id": c.id, "alvo": nome_alvo, "estado": resultado.estado, "nota": resultado.nota,
            "esperado": resultado.esperado if problema else None, "obtido": resultado.obtido if problema else None,
            "evidencias": evidencias, "limitacoes": resultado.limitacoes}


def _rodar_avulso(args: argparse.Namespace, cenarios_filtrados: list[e2e.Cenario]) -> int:
    """Fora de qualquer sprint: sem `Estado`, sem gravar `resultados.jsonl`/achados/`bancada.json`/
    passo nenhum. Evidências em `work/e2e-avulso/<carimbo>/`. Código de saída 0 a menos que o
    executor em si quebre (uma exceção não tratada aqui, não um cenário em `problema`/`pulei`)."""
    nome_alvo = args.alvo or "main"
    requisitos_ausentes = _requisitos_extra() | _requisito_loopback_avulso()
    aplicaveis = _cenarios_aplicaveis(cenarios_filtrados, nome_alvo)
    pasta_execucao = sandbox.criar_pasta(config.WORK / "e2e-avulso" / time.strftime("%Y%m%d-%H%M%S"))
    prazo = _prazo(_agora_dt(), args.limite)
    execucoes: list[dict] = []
    pulados_por_tempo = 0
    for c in aplicaveis:
        if prazo is not None and _agora_dt() >= prazo:
            pulados_por_tempo += 1
            continue
        ctx = _contexto_avulso(pasta_execucao, nome_alvo, c)
        resultado = e2e.rodar_cenario(c, ctx, requisitos_ausentes)
        execucoes.append(_relatar_avulso(c, nome_alvo, resultado, ctx.evidencias_registradas))
    saida = {"execucoes": execucoes, "pasta_evidencias": str(pasta_execucao)}
    if pulados_por_tempo:
        saida["pulados_por_tempo"] = {nome_alvo: pulados_por_tempo}
    return _saida(saida, True)


# ---------- --oculto: a fase se relança no desktop oculto ----------

TEMPO_OCULTO_MAX = 24 * 3600.0  # teto da execução inteira lá dentro
MARGEM_OCULTO = 600.0  # além da soma dos timeouts dos cenários (abrir o Python, descobrir, sobras)
FOLGA_POR_CENARIO = 30.0  # o fechamento forçado e a cortesia depois do tempo esgotado de cada cenário


def _precisa_relancar(args: argparse.Namespace) -> bool:
    """De dia a fase nunca roda na tela do usuário: fora do desktop oculto, ela se relança lá dentro com
    `--oculto` ou sempre que MAW_AGENTE_ENTRADA_REAL não é 1 (só a execução noturna, com teclado e mouse
    reais e a sessão desbloqueada, roda no desktop normal)."""
    from . import desktop_oculto

    if desktop_oculto.no_desktop_oculto():
        return False
    return bool(getattr(args, "oculto", False)) or os.environ.get("MAW_AGENTE_ENTRADA_REAL") != "1"


def _tempo_oculto(args: argparse.Namespace) -> float:
    """Teto da execução lá dentro: a soma dos timeouts dos cenários que vão rodar (vezes os alvos), mais
    folga por cenário e uma margem; no máximo TEMPO_OCULTO_MAX."""
    try:
        cenarios = _filtrar_cenarios(e2e.descobrir(config.PRIVADO / "cenarios"), args.cenario, args.arquivo)
        if args.avulso:
            alvos = [args.alvo or "main"]
        else:
            alvos = [a["nome"] for a in fases._alvos(fases._sprint_atual(), args.alvo) if not a.get("compartilha_com")]
        total = sum(c.timeout + FOLGA_POR_CENARIO for a in alvos for c in _cenarios_aplicaveis(cenarios, a))
    except (Exception, SystemExit):  # sem sprint ou cenário que não importa: o filho diz o que houve
        return TEMPO_OCULTO_MAX
    return min(TEMPO_OCULTO_MAX, total + MARGEM_OCULTO)


def _argv_sem_oculto(args: argparse.Namespace) -> list[str]:
    """A linha de comando da fase de novo, a partir do Namespace, sem `--oculto` (as opções vêm do
    próprio parser da fase, então opções novas entram sozinhas)."""
    p = argparse.ArgumentParser(add_help=False)
    _cfg(p)
    out = ["e2e"]
    for acao in p._actions:
        if not acao.option_strings or acao.dest == "oculto" or not hasattr(args, acao.dest):
            continue
        valor = getattr(args, acao.dest)
        opcao = next((o for o in acao.option_strings if o.startswith("--")), acao.option_strings[0])
        if isinstance(acao, argparse._StoreTrueAction):
            if valor:
                out.append(opcao)
        elif isinstance(acao, argparse._AppendAction):
            for v in valor or []:
                out += [opcao, str(v)]
        elif valor is not None and valor != acao.default:
            out += [opcao, str(valor)]
    return out


def _repassar(linha: str) -> None:
    try:
        print(linha, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(linha.encode(enc, errors="replace").decode(enc), flush=True)


def _relancar_no_oculto(args: argparse.Namespace) -> int:
    """Roda `python -m maw_agent e2e ...` (sem `--oculto`) no desktop oculto, com
    MAW_AGENTE_DESKTOP_OCULTO=1 e sem entrada real; repassa cada linha e devolve o código dele."""
    from . import desktop_oculto

    argv = [sys.executable, "-m", "maw_agent", *_argv_sem_oculto(args)]
    env = dict(os.environ)
    env["MAW_AGENTE_ENTRADA_REAL"] = "0"  # a entrada real não chega ao desktop oculto
    log = desktop_oculto.log_padrao("e2e")
    relatorio: dict = {}
    codigo = None
    try:
        codigo = desktop_oculto.rodar_oculto(argv, cwd=config.RAIZ, env=env, log=log, timeout=_tempo_oculto(args),
                                             eco=_repassar, relatorio=relatorio)
    except desktop_oculto.DesktopOcultoIndisponivel as ex:
        return _saida({"erro": f"desktop oculto indisponível: {ex}"}, False)
    except desktop_oculto.JanelaNoDesktopDoUsuario as ex:
        _saida({"erro": str(ex), "log": str(log)}, False)
        codigo = desktop_oculto.CODIGO_VIOLACAO
    finally:
        _depois_do_oculto(args, relatorio, codigo, log)
    print(f"[desktop oculto] e2e terminou com código {codigo}; log: {log}", file=sys.stderr, flush=True)
    return codigo


def _depois_do_oculto(args: argparse.Namespace, relatorio: dict, codigo: int | None, log: Path) -> None:
    """No processo de fora, depois do filho: possíveis fugas viram limitação, e um filho morto à força
    (tempo esgotado, janela no desktop do usuário, queda) pode ter deixado o %APPDATA% da MAW sujo:
    restaura agora, com a trava da máquina."""
    from . import desktop_oculto

    linhas = [desktop_oculto.descrever_fuga(f) for f in relatorio.get("fugas", [])]
    for l in linhas:
        print(f"[desktop oculto] {l}", file=sys.stderr, flush=True)
    e = None
    if not args.avulso:
        try:
            e = fases._sprint_atual()
        except (Exception, SystemExit):
            e = None
    if linhas and e is not None:
        _acrescentar_limitacoes(e, linhas)
    if codigo in (0, 1):
        return
    try:
        r = fases.restaurar_pendencias_ambiente("e2e no desktop oculto encerrada à força", e)
    except Exception as ex:  # o registro da falha fica no stderr; a bandeira continua para a retomada
        print(f"[desktop oculto] restauração depois do filho falhou: {ex}", file=sys.stderr, flush=True)
        return
    feitas = len(r["restauradas"]) + len(r["descartadas"])
    if feitas or r["adiadas"] or r["erros"]:
        print(f"[desktop oculto] %APPDATA% da MAW depois do filho: {feitas} restaurada(s)/descartada(s), "
              f"{len(r['adiadas'])} adiada(s), {len(r['erros'])} erro(s)", file=sys.stderr, flush=True)


@registrar("e2e", "roda os cenários E2E de privado/cenarios, por alvo", _cfg)
def cmd_e2e(args: argparse.Namespace) -> int:
    if _precisa_relancar(args):
        return _relancar_no_oculto(args)
    cenarios_todos = e2e.descobrir(config.PRIVADO / "cenarios")
    cenarios_filtrados = _filtrar_cenarios(cenarios_todos, args.cenario, args.arquivo)
    if args.avulso:
        with cache_local.temporario(lambda t: print(f"[e2e] {t}", file=sys.stderr, flush=True)):
            return _rodar_avulso(args, cenarios_filtrados)
    e = fases._sprint_atual()
    with cache_local.temporario(lambda t: _acrescentar_limitacoes(e, [t])):
        return _rodar_sprint(args, cenarios_todos, cenarios_filtrados)
