"""Fases da sprint expostas como `python -m maw_agent sprint <acao>` e `achado <acao>`."""
from __future__ import annotations
import argparse
import json
import os
import re
import time
from pathlib import Path

from . import (achados, alvos, build, catalogo, config, estado, preflight, redacao, sandbox, saude, suite)
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


def _ler_json(p: Path, padrao=None):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else padrao


def _itens_catalogo() -> list[dict]:
    return catalogo.carregar(config.CATALOGO) if config.CATALOGO.exists() else []


def _agora() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------- funções puras ----------

_RX_ERRO_BUILD = re.compile(r"^(?P<arq>.*?)(?:\((?P<pos>[\d,\s-]+)\))?\s*:\s*(?:fatal\s+)?(?:error|erro)\s+"
                            r"(?P<cod>[A-Z]{1,4}\d{3,5})\s*:\s*(?P<msg>.*)$", re.IGNORECASE)
# unidade + pastas de um caminho absoluto (deixa só o nome do arquivo)
_RX_CAMINHO_ABS = re.compile(r"[A-Za-z]:[\\/](?:[^\\/'\"<>|\r\n]*[\\/])*")


def assinatura_erro_build(linha: str) -> str:
    """'<código>:<arquivo base>:<mensagem>' — sem o caminho do worktree e sem (linha,coluna), para o
    mesmo erro ter a mesma impressão digital em todos os alvos e sprints."""
    m = _RX_ERRO_BUILD.match(linha.strip())
    if not m:
        return _RX_CAMINHO_ABS.sub("", linha).strip()
    base = re.split(r"[\\/]", m["arq"].strip())[-1].strip()
    return f"{m['cod'].upper()}:{base}:{_RX_CAMINHO_ABS.sub('', m['msg']).strip()}"


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
            "assinatura": f"build:{cfg}:" + (assinatura_erro_build(r["erros"][0]) if r["erros"] else "sem-erro"),
            "fonte": "build"}


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


# verificações que a suíte e o benchmark cobrem sozinhos; 'revisao' não é cobertura dinâmica
COBRIVEIS_NO_M1 = ("suite", "benchmark")


def resultados_suite_por_item(itens: list[dict], blocos: list[dict],
                              cobertas: tuple[str, ...] | set[str] = ("suite",)) -> dict[str, tuple[str, str | None]]:
    """Resultado de cada item do catálogo que cita blocos da suíte (`suite:<nome exato do bloco>`).

    - algum bloco citado falhou → falhou;
    - algum bloco citado não existe → nao_testavel listando os ausentes (mesmo que outros passem);
    - todos passaram → passou só se toda a `verificacao` do item foi coberta nesta sprint (`cobertas`);
      senão nao_testavel "parcial", dizendo o que falta e para qual marco."""
    cobertas = set(cobertas)
    out: dict[str, tuple[str, str | None]] = {}
    for it in itens:
        nomes = list(dict.fromkeys(c[len("suite:"):] for c in it.get("cenarios", []) if c.startswith("suite:")))
        if not nomes:
            continue
        casados = [b for b in blocos if b["nome"] in nomes]
        presentes = {b["nome"] for b in casados}
        ausentes = [n for n in nomes if n not in presentes]
        restantes = [v for v in it.get("verificacao", []) if v not in cobertas]
        if any(b["falhas"] for b in casados):
            out[it["id"]] = ("falhou", None)
        elif ausentes:
            out[it["id"]] = ("nao_testavel", f"bloco da suíte não encontrado: {', '.join(ausentes)}")
        elif restantes:
            out[it["id"]] = ("nao_testavel", f"parcial: blocos da suíte passaram ({', '.join(nomes)}); "
                                             f"{', '.join(restantes)} previstas para o marco {it.get('marco', '?')}")
        else:
            out[it["id"]] = ("passou", None)
    return out


def binario_valido(build_json: dict | None, exe: Path, inicio_iso: str | None) -> tuple[bool, str]:
    """O exe só vale se o build desta sprint deu certo e o exe não é mais velho que o início da
    compilação (senão é o binário de uma sprint anterior). Devolve (vale?, motivo se não vale)."""
    if not build_json:
        return False, "build não registrado nesta sprint"
    if not build_json.get("ok"):
        return False, "build falhou nesta sprint"
    exe = Path(exe)
    if not exe.exists():
        return False, f"executável ausente: {exe}"
    if not inicio_iso:
        return False, "início da compilação desta sprint não registrado"
    try:
        inicio = time.mktime(time.strptime(inicio_iso, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False, f"início da compilação ilegível: {inicio_iso}"
    if exe.stat().st_mtime < inicio:
        return False, "executável é anterior à compilação desta sprint"
    return True, ""


_ORDEM_REVERIFICACAO = {"corrigido": 0, "nao_verificavel": 1, "persiste": 2}


def juntar_reverificacoes(pasta: Path, incluir_base: bool = True,
                          extras: list[tuple[str, dict]] | tuple = ()) -> tuple[dict[str, str], list[str]]:
    """Junta as reverificações de todas as fontes: `extras` (ex.: as derivadas automaticamente),
    `reverificacoes.json` (se `incluir_base` e se existir) e cada `reverificacoes-<agente>.json` da
    pasta da sprint. Em conflito para o mesmo id, vence o valor mais conservador
    (persiste > nao_verificavel > corrigido — um achado só é 'corrigido' se ninguém disser que persiste);
    cada conflito, valor fora dos três permitidos e arquivo ilegível vira uma mensagem no segundo retorno."""
    pasta = Path(pasta)
    mensagens: list[str] = []
    fontes: list[tuple[str, object]] = list(extras)
    arquivos = []
    base = pasta / "reverificacoes.json"
    if incluir_base and base.exists():
        arquivos.append(("reverificacoes.json", base))
    arquivos += [(arq.stem[len("reverificacoes-"):], arq) for arq in sorted(pasta.glob("reverificacoes-*.json"))]
    for nome, arq in arquivos:
        try:
            fontes.append((nome, json.loads(arq.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as ex:
            mensagens.append(f"reverificações ilegíveis em {arq.name}: {ex} — ignoradas")
    resultado: dict[str, str] = {}
    de_onde: dict[str, str] = {}
    for nome, dados in fontes:
        if not isinstance(dados, dict):
            mensagens.append(f"reverificações ilegíveis em {nome}: esperado um objeto id → valor — ignoradas")
            continue
        for id_, valor in dados.items():
            if valor not in _ORDEM_REVERIFICACAO:
                mensagens.append(f"{id_}: valor inválido em {nome}: '{valor}' — ignorado")
                continue
            if id_ not in resultado:
                resultado[id_] = valor
                de_onde[id_] = nome
            elif valor != resultado[id_]:
                if _ORDEM_REVERIFICACAO[valor] > _ORDEM_REVERIFICACAO[resultado[id_]]:
                    mensagens.append(f"{id_}: {de_onde[id_]} diz {resultado[id_]}, {nome} diz {valor} — ficou {valor}")
                    resultado[id_] = valor
                    de_onde[id_] = nome
                else:
                    mensagens.append(f"{id_}: {de_onde[id_]} diz {resultado[id_]}, {nome} diz {valor} — ficou {resultado[id_]}")
    return resultado, mensagens


_ESTADOS_ABERTOS = ("novo", "aberto", "regressao", "nao_verificavel")


def _veredito_automatico(ultimo: dict, origem: str, pasta: Path) -> str:
    """Reverifica um achado de build/suíte num alvo pelo seu critério de aceite: 'corrigido' só quando
    o critério passa nesta sprint; 'persiste' quando o mesmo erro reaparece; senão 'nao_verificavel'."""
    assin = ultimo.get("assinatura", "")
    if ultimo.get("fonte") == "build":
        partes = assin.split(":")
        cfg = partes[1] if assin.startswith("build:") and len(partes) > 1 else \
            ("Debug" if ultimo.get("item_catalogo", "").endswith("debug") else "Release")
        b = _ler_json(pasta / "builds" / f"{origem}-{cfg}.json")
        # critério: "o build compila sem erros". Build que falhou por outro motivo pode estar escondendo o erro.
        return "corrigido" if b and b.get("ok") else "nao_verificavel"
    s = _ler_json(pasta / "suites" / f"{origem}.json")
    if s is None:
        return "nao_verificavel"
    if assin.startswith("suite:"):
        nome, _, sub = assin[len("suite:"):].partition("|")
        for b in s.get("blocos", []):
            if b["nome"] == nome and b["sub"] == sub:
                return "persiste" if b["falhas"] else "corrigido"
        return "nao_verificavel"
    if assin.startswith("suite-incoerente:"):
        inc = s.get("incoerencias", [])
        if not inc:
            return "corrigido"
        return "persiste" if assin[len("suite-incoerente:"):] in inc else "nao_verificavel"
    if assin.startswith("jassert:"):
        if s.get("captura_erro"):
            return "nao_verificavel"
        assercoes = s.get("assercoes", [])
        if not assercoes:
            return "corrigido"
        procurada = achados.normalizar_assinatura(assin)
        mesma = any(achados.normalizar_assinatura(f"jassert:{x}") == procurada for x in assercoes)
        return "persiste" if mesma else "nao_verificavel"
    return "nao_verificavel"


def reverificacoes_automaticas(historico: dict, alvos_sprint: list[dict], pasta: Path) -> dict[str, str]:
    """Reverificação dos achados abertos de fonte build/suíte: para cada alvo do achado que existe
    nesta sprint (quem compartilha a árvore usa a execução da origem), olha o build/suíte desta sprint.
    Vários alvos: vale o mais conservador."""
    por_nome = {a["nome"]: a for a in alvos_sprint}
    out: dict[str, str] = {}
    for reg in historico.get("itens", {}).values():
        ultimo = reg.get("ultimo") or {}
        if reg.get("estado") not in _ESTADOS_ABERTOS or ultimo.get("fonte") not in ("build", "suite"):
            continue
        vereditos = []
        for x in ultimo.get("alvos", []):
            a = por_nome.get(x["alvo"])
            if a is not None:
                vereditos.append(_veredito_automatico(ultimo, a.get("compartilha_com") or a["nome"], Path(pasta)))
        if vereditos:
            out[reg["id"]] = max(vereditos, key=_ORDEM_REVERIFICACAO.__getitem__)
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


_RESULTADOS_VEREDITO = ("confirmado", "provavel", "derrubado")


def ler_brutos(pasta: Path) -> tuple[list[tuple[str, dict]], set[int], list[str]]:
    """Lê `achados-brutos/` e casa cada achado com seu veredito em `vereditos/<mesmo nome>`.

    Achado sem veredito legível (ausente, JSON inválido, lista mais curta, objeto sem `resultado`
    válido) entra com `confianca: provavel` e `veredito: null`, e seu id() vai para o segundo retorno
    (o contador "sem verificação adversarial"). Veredito ilegível e achado bruto ilegível viram
    mensagens para `erros_agente.json`. Nunca levanta exceção por conteúdo ruim."""
    pasta = Path(pasta)
    entradas: list[tuple[str, dict]] = []
    sem_verificacao: set[int] = set()
    mensagens: list[str] = []
    for p in sorted((pasta / "achados-brutos").glob("*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            mensagens.append(f"achado bruto ilegível em {p.name}: {ex}")
            continue
        lista = obj if isinstance(obj, list) else [obj]
        vered = pasta / "vereditos" / p.name
        vlista: list | None = None
        if vered.exists():
            try:
                v = json.loads(vered.read_text(encoding="utf-8"))
                if isinstance(v, list):
                    vlista = v
                elif len(lista) == 1:
                    vlista = [v]
                else:
                    mensagens.append(f"veredito ilegível em vereditos/{p.name}: um objeto só para "
                                     f"{len(lista)} achados (esperada uma lista na mesma ordem)")
            except (OSError, ValueError) as ex:
                mensagens.append(f"veredito ilegível em vereditos/{p.name}: JSON inválido ({ex})")
        for i, a in enumerate(lista):
            origem = f"{p.name}[{i}]"
            if not isinstance(a, dict):
                mensagens.append(f"achado inválido em {origem}: não é um objeto JSON")
                continue
            veredito = None
            if vlista is not None:
                if i >= len(vlista):
                    mensagens.append(f"veredito ilegível para {origem}: a lista de vereditos tem {len(vlista)} item(ns)")
                elif not isinstance(vlista[i], dict) or vlista[i].get("resultado") not in _RESULTADOS_VEREDITO:
                    mensagens.append(f"veredito ilegível para {origem}: sem 'resultado' válido "
                                     f"({'/'.join(_RESULTADOS_VEREDITO)})")
                else:
                    veredito = vlista[i]
            a["veredito"] = veredito
            if veredito is None:
                a["confianca"] = "provavel"
                sem_verificacao.add(id(a))
            entradas.append((origem, a))
    return entradas, sem_verificacao, mensagens


def conferir_referencias(lista: list[dict], ids_catalogo: set[str] | None,
                         alvos_sprint: dict[str, str] | None) -> list[str]:
    """Referências de um achado a registrar: item do catálogo existente (quando há catálogo),
    alvo = um `nome` de alvos.json da sprint e commit = o commit desse alvo."""
    erros: list[str] = []
    if alvos_sprint is None:
        return ["alvos.json da sprint ausente: rode `sprint preparar` antes de registrar achados"]
    for i, a in enumerate(lista):
        item = a.get("item_catalogo")
        if ids_catalogo is not None and item not in ids_catalogo:
            erros.append(f"achado {i}: item_catalogo '{item}' não existe no catálogo "
                         "(use um id de privado/catalogo/funcionalidades.yaml ou '<área>/geral')")
        for x in a.get("alvos", []):
            nome, commit = x.get("alvo"), x.get("commit")
            if nome not in alvos_sprint:
                erros.append(f"achado {i}: alvo '{nome}' não está em alvos.json da sprint — use o campo "
                             f"`nome` do alvo, nunca o nome da branch (alvos: {', '.join(sorted(alvos_sprint))})")
            elif commit != alvos_sprint[nome]:
                erros.append(f"achado {i}: commit '{commit}' não é o do alvo {nome} ({alvos_sprint[nome]})")
    return erros


# ---------- ambiente: %APPDATA%\MAW ----------

def _restaurar_ambiente(e: estado.Estado, backup: Path, quem: str) -> dict:
    """Restaura o %APPDATA%\\MAW do backup, confere o manifesto, apaga a bandeira appdata-sujo.json
    e depois o backup (que contém a chave do Gemini). Registra tudo no estado da sprint."""
    reg: dict = {"quem": quem, "backup": str(backup), "verificado": False, "erro": None, "backup_apagado": False}
    try:
        sandbox.restaurar_pasta(Path(backup))
        reg["verificado"] = True
    except Exception as ex:  # a falha vai para o PDF, não some
        reg["erro"] = redacao.redigir(str(ex))
    if reg["verificado"]:
        try:
            sandbox.remover(e.pasta / "appdata-sujo.json")
            sandbox.descartar_backup(Path(backup))
            reg["backup_apagado"] = True
        except OSError as ex:
            reg["erro_ao_apagar"] = redacao.redigir(str(ex))
    e.registrar_restauracao(reg)
    return reg


def _restaurar_pendente(e: estado.Estado) -> dict | None:
    """Se uma execução anterior caiu com o %APPDATA%\\MAW sujo (appdata-sujo.json), restaura agora."""
    sujo = e.pasta / "appdata-sujo.json"
    if not sujo.exists():
        return None
    try:
        backup = Path(json.loads(sujo.read_text(encoding="utf-8"))["backup"])
    except (OSError, ValueError, KeyError, TypeError) as ex:
        reg = {"quem": "retomada", "backup": None, "verificado": False, "backup_apagado": False,
               "erro": f"appdata-sujo.json ilegível: {ex}"}
        e.registrar_restauracao(reg)
        return reg
    return _restaurar_ambiente(e, backup, "retomada")


def _restaurar_pendentes() -> list[dict]:
    out = []
    for pasta in estado._existentes(config.RELATORIOS):
        if (pasta / "appdata-sujo.json").exists() and (pasta / "estado.json").exists():
            reg = _restaurar_pendente(estado.carregar(pasta))
            if reg:
                out.append({"sprint": pasta.name, **reg})
    return out


def _situacao_do_ambiente(e: estado.Estado) -> tuple[bool, str | None]:
    """Restaurado = nenhuma bandeira pendente e a última restauração de cada backup verificada."""
    erros = []
    if (e.pasta / "appdata-sujo.json").exists():
        erros.append("restauração do %APPDATA%\\MAW pendente (appdata-sujo.json)")
    ultima: dict[str, dict] = {}
    for r in e.restauracoes:
        ultima[str(r.get("backup"))] = r
    erros += [f"restauração de {b} falhou: {r.get('erro') or 'sem mensagem de erro'}"
              for b, r in ultima.items() if not r.get("verificado")]
    return (not erros, "; ".join(erros) or None)


def _arquivos_musica() -> dict[str, list[str]]:
    """Arquivos das pastas MAW* dentro de Music (a suíte pode gravar áudio lá)."""
    out: dict[str, list[str]] = {}
    if config.MUSICA.is_dir():
        for pasta in sorted(config.MUSICA.glob("MAW*")):
            if pasta.is_dir():
                out[pasta.name] = sorted(p.relative_to(pasta).as_posix() for p in pasta.rglob("*") if p.is_file())
    return out


def _acrescentar_limitacoes(e: estado.Estado, nome: str, linhas: list[str]) -> None:
    p = e.pasta / f"limitacoes-{nome}.json"
    atuais = _ler_json(p, [])
    novas = atuais + [l for l in linhas if l not in atuais]
    if novas != atuais:
        sandbox.escrever_json(p, novas)


# ---------- ações ----------

_ACOES_SPRINT = {
    "iniciar": "inicia uma sprint nova ou retoma a em andamento (restaura o ambiente pendente antes)",
    "preflight": "confere os pré-requisitos e registra o ambiente",
    "preparar": "atualiza o espelho, descobre os alvos, cria os worktrees e registra a prova 'antes'",
    "compilar": "compila Release e Debug de cada alvo",
    "suite": "roda a suíte (Debug) e o benchmark (Release) de cada alvo",
    "consolidar": "junta achados, vereditos e reverificações e atualiza o histórico",
    "encerrar": "restaura pendências do ambiente e prova que a MAW ficou intocada",
    "relatorio": "gera o PDF da sprint",
    "status": "mostra os passos da sprint em andamento",
    "marcar": "conclui um passo de julgamento (ex.: revisao:main:testador-motor, verificar, textos)",
}


def _cfg_sprint(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="acao", required=True)
    for acao, ajuda in _ACOES_SPRINT.items():
        sp = sub.add_parser(acao, help=ajuda)
        if acao == "iniciar":
            g = sp.add_mutually_exclusive_group()
            g.add_argument("--nova", action="store_true", help="começa uma sprint nova")
            g.add_argument("--retomar", action="store_true", help="retoma a em andamento (o padrão)")
        elif acao in ("compilar", "suite"):
            sp.add_argument("--alvo")
        elif acao == "marcar":
            sp.add_argument("passo")
            sp.add_argument("--detalhe", help="JSON guardado com o passo")


@registrar("sprint", "executa uma fase da sprint", _cfg_sprint)
def cmd_sprint(args: argparse.Namespace) -> int:
    return {"iniciar": _iniciar, "preflight": _preflight, "preparar": _preparar, "compilar": _compilar,
            "suite": _suite, "consolidar": _consolidar, "encerrar": _encerrar, "relatorio": _relatorio,
            "status": _status, "marcar": _marcar}[args.acao](args)


def _iniciar(args) -> int:
    restauracoes = _restaurar_pendentes()  # antes de qualquer coisa: o ambiente volta ao original
    e = None if args.nova else estado.em_andamento()
    retomada = e is not None
    e = e or estado.nova_sprint()
    return _saida({"sprint": e.nome, "pasta": str(e.pasta), "retomada": retomada,
                   "passos_concluidos": sorted(k for k in e.passos if e.feito(k)),
                   "restauracoes": restauracoes},
                  all(r["verificado"] for r in restauracoes))


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
    # sem backup aqui: o %APPDATA%\MAW só é salvo e restaurado em volta de quem roda a MAW (a suíte)
    detalhe = {"alvos": [a.nome for a in lista], "avisos": avisos}
    e.concluir("preparar", detalhe)
    return _saida({"ok": True, **detalhe})


_CONFIGS = ("Release", "Debug")


def _compilar(args) -> int:
    e = _sprint_atual()
    ok_geral = True
    resumo = []
    for a in _alvos(e, args.alvo):
        if a["compartilha_com"]:
            continue
        wt = config.ALVOS_DIR / a["nome"]
        for cfg in _CONFIGS:
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
    if not e.feito("compilar") and all(e.feito(f"compilar:{a['nome']}:{cfg}")
                                       for a in _alvos(e) if not a["compartilha_com"] for cfg in _CONFIGS):
        e.concluir("compilar", {"builds": len(resumo)})
    return _saida({"builds": resumo}, ok_geral)


_MOTIVO_BUILD = "build {cfg} falhou ou não rodou nesta sprint"
_MOTIVO_MAW_ABERTA = "MAW aberta: feche-a para a suíte rodar"


def _itens_com_suite(itens: list[dict]) -> list[str]:
    return [it["id"] for it in itens if any(c.startswith("suite:") for c in it.get("cenarios", []))]


def _registrar_suite_nao_rodou(e: estado.Estado, nome: str, itens: list[dict], motivo: str) -> None:
    catalogo.registrar_resultado(e.pasta, "saude/suite-existente", nome, "nao_testavel", motivo, fonte="suite")
    for item in _itens_com_suite(itens):
        catalogo.registrar_resultado(e.pasta, item, nome, "nao_testavel", motivo, fonte="suite")


def _suite_de_um_alvo(e: estado.Estado, a: dict, itens: list[dict], env: dict[str, str]) -> dict:
    nome = a["nome"]
    passo = f"suite:{nome}"
    e.iniciar(passo)
    wt = config.ALVOS_DIR / nome
    cwd = sandbox.criar_pasta(config.WORK / "execucao" / nome)
    valido = {cfg: binario_valido(_ler_json(e.pasta / "builds" / f"{nome}-{cfg}.json"), build.caminho_exe(wt, cfg),
                                  (e.passos.get(f"compilar:{nome}:{cfg}") or {}).get("inicio"))
              for cfg in _CONFIGS}
    benchmark_ok = False
    if valido["Release"][0]:
        b = suite.rodar_benchmark(build.caminho_exe(wt, "Release"), cwd, env=env)
        sandbox.escrever_json(e.pasta / "benchmarks" / f"{nome}.json", b)
        benchmark_ok = bool(b["linhas"]) and b["exit_code"] == 0
        catalogo.registrar_resultado(e.pasta, "saude/benchmark", nome, "passou" if benchmark_ok else "falhou",
                                     None if b["linhas"] else "tabela ausente", fonte="benchmark")
    else:
        catalogo.registrar_resultado(e.pasta, "saude/benchmark", nome, "nao_testavel",
                                     _MOTIVO_BUILD.format(cfg="Release"), fonte="benchmark")
    if valido["Debug"][0]:
        with saude.CapturaDepuracao() as cap:
            r = suite.rodar_suite(build.caminho_exe(wt, "Debug"), cwd, env=env)
        d = r.como_dict()
        d["assercoes"] = [redacao.redigir(x) for x in saude.assercoes(saude.filtrar(cap.mensagens))]
        d["captura_erro"] = cap.erro
        sandbox.escrever_json(e.pasta / "suites" / f"{nome}.json", d)
        for i, ach in enumerate(achados_da_suite(a, d)):
            sandbox.escrever_json(e.pasta / "achados-brutos" / f"suite-{nome}-{i:03d}.json", ach)
        catalogo.registrar_resultado(e.pasta, "saude/suite-existente", nome,
                                     "passou" if r.passou and not d["assercoes"] else "falhou", fonte="suite")
        cobertas = COBRIVEIS_NO_M1 if benchmark_ok else ("suite",)
        for item, (res, motivo) in resultados_suite_por_item(itens, d["blocos"], cobertas).items():
            catalogo.registrar_resultado(e.pasta, item, nome, res, motivo, fonte="suite")
    else:
        _registrar_suite_nao_rodou(e, nome, itens, _MOTIVO_BUILD.format(cfg="Debug"))
    detalhe = {cfg.lower(): (valido[cfg][1] or "binário desta sprint") for cfg in _CONFIGS}
    e.concluir(passo, detalhe)
    return {"alvo": nome, **detalhe}


def _rodar_suites(e: estado.Estado, pendentes: list[dict], itens: list[dict]) -> tuple[list[dict], dict]:
    """backup do %APPDATA%\\MAW → bandeira appdata-sujo.json → roda → restaura e confere → apaga a
    bandeira → apaga o backup. A pasta de música é listada antes e depois."""
    env = suite.ambiente_com_ffmpeg(dict(os.environ), config.FERRAMENTAS / "ffmpeg" / "bin")
    musica_antes = _arquivos_musica()
    backup = sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
    sandbox.escrever_json(e.pasta / "appdata-sujo.json", {"backup": str(backup), "desde": _agora()})
    resumo: list[dict] = []
    try:
        for a in pendentes:
            resumo.append(_suite_de_um_alvo(e, a, itens, env))
    finally:
        restauracao = _restaurar_ambiente(e, backup, "suite")
        musica_depois = _arquivos_musica()
        novos = {pasta: sorted(set(lista) - set(musica_antes.get(pasta, [])))
                 for pasta, lista in musica_depois.items()}
        novos = {k: v for k, v in novos.items() if v}
        sandbox.escrever_json(e.pasta / "musica-suite.json",
                              {"antes": musica_antes, "depois": musica_depois, "novos": novos})
        _acrescentar_limitacoes(e, "suite", [f"a suíte deixou {len(v)} arquivo(s) em Music\\{k}"
                                             for k, v in novos.items()])
    return resumo, restauracao


def _suite(args) -> int:
    e = _sprint_atual()
    itens = _itens_catalogo()
    todos = _alvos(e)
    pendentes = [a for a in _alvos(e, args.alvo)
                 if not a["compartilha_com"] and not e.feito(f"suite:{a['nome']}")]
    saida: dict = {"suites": []}
    ok = True
    if pendentes:
        recusa = None
        if suite.maw_aberta():
            recusa = _MOTIVO_MAW_ABERTA  # e o %APPDATA%\MAW nem é tocado
        else:
            pendente = _restaurar_pendente(e)
            if pendente and not pendente["verificado"]:
                recusa = "restauração pendente do %APPDATA%\\MAW falhou: a suíte não roda sobre um ambiente sujo"
        if recusa is None:
            try:
                saida["suites"], restauracao = _rodar_suites(e, pendentes, itens)
                saida["ambiente_restaurado"] = restauracao["verificado"]
                ok = restauracao["verificado"]
            except OSError as ex:
                if (e.pasta / "appdata-sujo.json").exists():
                    raise  # já rodou algo: o erro não pode ser escondido
                recusa = f"não foi possível fazer o backup do %APPDATA%\\MAW: {ex}"
        if recusa is not None:
            for a in pendentes:
                _registrar_suite_nao_rodou(e, a["nome"], itens, recusa)
                catalogo.registrar_resultado(e.pasta, "saude/benchmark", a["nome"], "nao_testavel", recusa,
                                             fonte="benchmark")
            saida["recusado"] = recusa
            ok = False
    # alvos que compartilham a árvore herdam as células da origem
    res = catalogo.carregar_resultados(e.pasta)
    for a in todos:
        if a["compartilha_com"]:
            for (item, alvo), r in list(res.items()):
                if alvo == a["compartilha_com"]:
                    catalogo.registrar_resultado(e.pasta, item, a["nome"], r["resultado"],
                                                 f"mesma árvore de código de {alvo}" + (f": {r['motivo']}" if r.get("motivo") else ""),
                                                 r.get("achados"), r.get("fonte", ""))
    return _saida(saida, ok)


def _cfg_achado(p: argparse.ArgumentParser) -> None:
    p.add_argument("acao", choices=["validar", "registrar"])
    p.add_argument("arquivo")


@registrar("achado", "valida ou registra um achado (JSON) na sprint em andamento", _cfg_achado)
def cmd_achado(args: argparse.Namespace) -> int:
    try:
        obj = json.loads(Path(args.arquivo).read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        return _saida({"ok": False, "erros": [f"não foi possível ler {args.arquivo}: {ex}"]}, False)
    lista = obj if isinstance(obj, list) else [obj]
    erros = {i: achados.validar(a) if isinstance(a, dict) else ["não é um objeto JSON"] for i, a in enumerate(lista)}
    erros = {i: e for i, e in erros.items() if e}
    if erros or args.acao == "validar":
        return _saida({"ok": not erros, "erros": erros}, not erros)
    e = _sprint_atual()
    ids = {it["id"] for it in _itens_catalogo()} if config.CATALOGO.exists() else None
    alvos_json = _ler_json(e.pasta / "alvos.json")
    alvos_sprint = {a["nome"]: a["commit"] for a in alvos_json["alvos"]} if alvos_json else None
    ref = conferir_referencias(lista, ids, alvos_sprint)
    if ref:
        return _saida({"ok": False, "erros": ref}, False)
    destino = e.pasta / "achados-brutos" / Path(args.arquivo).name
    if destino.exists():
        return _saida({"ok": False, "erros": [f"já existe um achado bruto chamado {destino.name}: "
                                              "use o próximo número (NNN) em vez de sobrescrever"]}, False)
    sandbox.escrever_texto(destino, redacao.redigir(json.dumps(obj, ensure_ascii=False, indent=2) + "\n"))
    return _saida({"ok": True, "registrado": str(destino)})


def _cfg_catalogo(p: argparse.ArgumentParser) -> None:
    p.add_argument("acao", choices=["validar"])


@registrar("catalogo", "valida o catálogo de funcionalidades", _cfg_catalogo)
def cmd_catalogo(args: argparse.Namespace) -> int:
    itens = catalogo.carregar(config.CATALOGO)
    princ = catalogo.carregar(config.PRINCIPIOS) if config.PRINCIPIOS.exists() else []
    erros = catalogo.validar(itens)
    ids = {i["id"] for i in itens}
    for p in princ:
        if f"principio/{p['id']}" not in ids:
            erros.append(f"princípio {p['id']} sem item principio/{p['id']} no catálogo")
    for obrig in ("saude/build-release", "saude/build-debug", "saude/suite-existente", "saude/benchmark"):
        if obrig not in ids:
            erros.append(f"item obrigatório ausente: {obrig}")
    return _saida({"itens": len(itens), "principios": len(princ), "erros": erros}, not erros)


def _consolidar(args) -> int:
    """Idempotente: consolida sempre a partir do histórico como estava antes desta sprint
    (historico-antes.json, gravado na primeira execução) e reconstrói erros_agente.json."""
    e = _sprint_atual()
    e.iniciar("consolidar")
    antes = e.pasta / "historico-antes.json"
    if not antes.exists():
        manual = e.pasta / "reverificacoes.json"
        if manual.exists():  # escrito à mão antes da primeira consolidação: vira uma fonte como as outras
            sandbox.copiar(manual, e.pasta / "reverificacoes-manual.json")
        sandbox.escrever_json(antes, achados.carregar_historico(config.HISTORICO))
    hist_antes = achados.carregar_historico(antes)
    catalogo.descartar_fonte(e.pasta, "consolidacao")
    entradas, sem_verificacao, erros_leitura = ler_brutos(e.pasta)
    brutos, invalidos = separar_validos(entradas)
    sem_verif = sum(1 for a in brutos if id(a) in sem_verificacao)
    derivadas = reverificacoes_automaticas(hist_antes, _alvos(e) if (e.pasta / "alvos.json").exists() else [],
                                           e.pasta)
    rever, conflitos_rever = juntar_reverificacoes(e.pasta, incluir_base=False,
                                                   extras=[("automatico", derivadas)])
    sandbox.escrever_json(e.pasta / "reverificacoes.json", rever)
    sandbox.escrever_json(e.pasta / "erros_agente.json", erros_leitura + invalidos + conflitos_rever)
    for a in brutos:
        if (a.get("veredito") or {}).get("resultado") == "provavel":
            a["confianca"] = "provavel"
    derrubados = [{"titulo": a["titulo"], "justificativa": a["veredito"].get("justificativa", "")}
                  for a in brutos if (a.get("veredito") or {}).get("resultado") == "derrubado"]
    lista, hist = achados.consolidar(brutos, hist_antes, f"{e.numero:02d}", rever)
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
    detalhe = {"achados": len(lista), "derrubados": len(derrubados), "achados_invalidos": len(invalidos),
               "sem_verificacao_adversarial": sem_verif, "reverificacoes_automaticas": len(derivadas)}
    e.concluir("consolidar", detalhe)
    return _saida(detalhe)


def _encerrar(args) -> int:
    e = _sprint_atual()
    if e.feito("encerrar"):  # já encerrada: devolve o que foi registrado, sem restaurar nada de novo
        info = e.dados("encerrar") or {}
        return _saida({**info, "retomado": True},
                      bool(info.get("verificado")) and bool(info.get("ambiente_restaurado")))
    e.iniciar("encerrar")
    _restaurar_pendente(e)  # caso uma suíte tenha caído sem restaurar
    antes = _ler_json(e.pasta / "prova-antes.json")
    depois = alvos.prova_intocada(config.pastas_protegidas())
    sandbox.escrever_json(e.pasta / "prova-depois.json", depois)
    difs = (["prova 'antes' não registrada (a fase preparar não rodou)"] if antes is None
            else alvos.comparar_provas(antes, depois))
    restaurado, erro = _situacao_do_ambiente(e)
    info = {"verificado": not difs, "diferencas": difs, "ambiente_restaurado": restaurado, "erro_restauracao": erro}
    sandbox.escrever_json(e.pasta / "intocada.json", info)
    e.concluir("encerrar", info)
    return _saida(info, not difs and restaurado)


def _relatorio(args) -> int:
    from .relatorio import contexto, pdf
    e = _sprint_atual()
    e.iniciar("relatorio")
    itens = _itens_catalogo()
    principios = catalogo.carregar(config.PRINCIPIOS) if config.PRINCIPIOS.exists() else []
    logo = config.ALVOS_DIR / "main" / "Source" / "logo_MAW.png"
    ctx = contexto.montar(e.pasta, itens, principios, estado.anterior(e), logo if logo.exists() else None)
    destino = pdf.renderizar(ctx, e.pasta / f"MAW-Sprint-{e.numero:02d}.pdf")
    if ctx["achados_anexados"]:
        pdf.anexar(destino, e.pasta / "achados.json", "achados.json")
    e.concluir("relatorio", {"pdf": str(destino), "achados_anexados": ctx["achados_anexados"]})
    return _saida({"pdf": str(destino), "achados_anexados": ctx["achados_anexados"]})


def _status(args) -> int:
    e = estado.em_andamento()
    if e is None:
        return _saida({"em_andamento": None})
    return _saida({"sprint": e.nome, "passos": e.passos, "restauracoes": e.restauracoes})


# passos que só a própria fase da CLI conclui (nunca `sprint marcar`)
_PASSOS_DA_CLI = ("preflight", "preparar", "compilar", "suite", "consolidar", "encerrar", "relatorio")


def _marcar(args) -> int:
    passo = args.passo.strip()
    if not passo:
        return _saida({"ok": False, "erro": "passo vazio"}, False)
    if passo.split(":")[0] in _PASSOS_DA_CLI:
        return _saida({"ok": False, "erro": f"o passo '{passo}' é concluído pela própria fase "
                                            f"(`sprint {passo.split(':')[0]}`), não por `sprint marcar`"}, False)
    detalhe = None
    if args.detalhe:
        try:
            detalhe = json.loads(args.detalhe)
        except ValueError as ex:
            return _saida({"ok": False, "erro": f"--detalhe não é JSON válido: {ex}"}, False)
    e = _sprint_atual()
    e.concluir(passo, detalhe)
    return _saida({"ok": True, "sprint": e.nome, "passo": passo})
