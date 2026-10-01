"""Fases da sprint expostas como `python -m maw_agent sprint <acao>` e `achado <acao>`."""
from __future__ import annotations
import argparse
import json
import os
import re
import time
from pathlib import Path

import yaml

from . import (achados, alvos, bancada, build, catalogo, config, e2e, estado, preflight, redacao, sandbox, saude,
               sondas, suite, trava_appdata)
from .cli import registrar


def _saida(obj: dict, ok: bool = True) -> int:
    """JSON no stdout. Redirecionado para arquivo (script noturno), o stdout do Windows é cp1252 e não
    tem "→" nem "✓": nesse caso sai o mesmo JSON só com ASCII (escapes \\uXXXX, que qualquer leitor de
    JSON devolve iguais) em vez de uma exceção depois de os dados já estarem gravados."""
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:  # o TextIOWrapper codifica antes de escrever: nada saiu pela metade
        print(json.dumps(obj, ensure_ascii=True, indent=2))
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


def _codigo_de_saida(codigo: int | None) -> str:
    """O código como o Windows o mostra quando é um NTSTATUS (ex.: -1073741571 (0xC00000FD), estouro da pilha)."""
    if codigo is None:
        return "nenhum (tempo esgotado)"
    return f"{codigo} (0x{codigo & 0xFFFFFFFF:08X})" if codigo < 0 or codigo > 0x7FFFFFFF else str(codigo)


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
    ultimo = r.get("ultimo_bloco")
    for inc in r.get("incoerencias", []):
        titulo, obtido = f"Relatório da suíte incoerente: {inc[:60]}", inc
        if ultimo and inc.startswith("totais ausentes"):
            # a MAW escreve "rodando: <área> / <bloco>" no stderr: o processo caiu (ou travou) nesse bloco.
            # A assinatura não leva o bloco: a mesma queda num bloco vizinho continua sendo o mesmo achado.
            titulo += f" (último bloco: {ultimo[:50]})"
            obtido = (f"{inc}. Último bloco que começou a rodar (stderr da suíte): '{ultimo}'. "
                      f"Código de saída: {_codigo_de_saida(r.get('exit_code'))}.")
        out.append({**base, "titulo": titulo, "tipo": "violacao",
                    "severidade": "alta", "item_catalogo": "saude/suite-existente", "principio": "P4",
                    "esperado": "código de saída, linha RESULTADO e totais concordam", "obtido": obtido,
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


def achados_das_sondas(alvo: dict, blocos_sondas: list[dict], detalhes: list[str],
                       mapa: dict[str, list[str]], arquivos: dict[str, str] | None = None) -> list[dict]:
    """Um achado por bloco de sonda do agente que falhou, no item do catálogo que a sonda cobre
    (`privado/sondas/itens.yaml`: o primeiro item que não é princípio; o princípio vai para
    `principio`). A sonda é código C++ do próprio agente e pode estar errada: `fonte: sonda` NÃO é
    mecânica — sem veredito o achado vira provável, e só o advogado-do-diabo o confirma; a causa
    provável diz isso e aponta o arquivo da sonda. A assinatura `suite:<nome>|<sub>` deixa a
    consolidação reverificá-lo pela suíte. `arquivos`: nome da sonda → arquivo .cpp."""
    arquivos = arquivos or {}
    det = _detalhes_por_bloco(detalhes)
    out = []
    for b in blocos_sondas:
        if not b["falhas"]:
            continue
        itens = list(mapa.get(b["nome"], []))
        principio = next((i.split("/", 1)[1] for i in itens if i.startswith("principio/")), None)
        item = next((i for i in itens if not i.startswith("principio/")), None) or \
            (itens[0] if itens else "saude/geral")
        linhas = det.get((b["nome"], b["sub"]), [])[:6]
        arq = arquivos.get(b["nome"])
        out.append({
            "titulo": f"Sonda do agente falha: {b['nome']} → {b['sub']}", "tipo": "bug",
            "severidade": "alta" if principio else "media", "prioridade": None,
            "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
            "item_catalogo": item, "principio": principio,
            "passos": [f"injetar a sonda do agente no build Debug ({arq or b['nome']}; `sondas injetar`) e compilar",
                       "MAW_APP.exe --run-tests (build Debug)",
                       f"ver o bloco '{b['nome']} → {b['sub']}' no relatório da suíte"],
            "esperado": f"o bloco da sonda '{b['nome']} → {b['sub']}' passa",
            "obtido": f"{b['falhas']} verificação(ões) falharam. " + " ".join(linhas),
            "evidencias": [{"arquivo": f"suites/{alvo['nome']}.json", "embutir": False,
                            "legenda": "relatório completo da suíte (os blocos das sondas ficam em 'sondas')"}],
            "causa_provavel": {
                "arquivo_linha": f"privado/sondas/{arq or '<arquivo da sonda>'}",
                "texto": "hipótese: o bloco é uma sonda — teste do próprio agente, não da suíte da MAW — e a "
                         "falha pode ser um erro da sonda; antes de mudar a MAW, confira a sonda contra o código "
                         "da MAW"},
            "sugestao": "corrigir o código da MAW até o bloco da sonda passar (a sonda é do agente e não vai "
                        "para o repositório da MAW; se a sonda estiver errada, quem corrige é o agente)",
            "criterio_aceite": f"o bloco '{b['nome']} → {b['sub']}' passa na suíte do build Debug com as sondas "
                               "do agente injetadas",
            "confianca": "confirmado", "assinatura": f"suite:{b['nome']}|{b['sub']}", "fonte": "sonda"})
    return out


def itens_com_sondas(itens: list[dict], mapa: dict[str, list[str]]) -> list[dict]:
    """Cópia dos itens em que cada item coberto por uma sonda (mapa de `itens.yaml`) cita o cenário
    `suite:<nome da sonda>` — mesmo que o catálogo ainda não o tenha."""
    por_item: dict[str, list[str]] = {}
    for nome, ids in mapa.items():
        for i in ids:
            por_item.setdefault(i, []).append(f"suite:{nome}")
    out = []
    for it in itens:
        extras = [c for c in por_item.get(it.get("id"), []) if c not in it.get("cenarios", [])]
        out.append({**it, "cenarios": list(it.get("cenarios", [])) + extras} if extras else it)
    return out


_RX_PASTA_SONDAS = re.compile(r"(?:^|[\\/])Tests[\\/]Sondas[\\/]", re.IGNORECASE)


def sondas_nos_erros(erros: list[str], injetadas: list[str]) -> set[str]:
    """Quais das sondas injetadas (nomes de arquivo .cpp) aparecem nos erros do MSBuild: o arquivo do
    erro de compilação ou o .obj do erro de link. Um erro na pasta das sondas com um nome que não é de
    nenhuma delas deixa todas sob suspeita."""
    por_base = {Path(x).stem.lower(): x for x in injetadas}
    culpadas: set[str] = set()
    for linha in erros:
        m = _RX_ERRO_BUILD.match(linha.strip())
        arq = m["arq"].strip() if m else linha
        base = re.split(r"[\\/]", arq)[-1].strip()
        stem = base.rsplit(".", 1)[0].lower()
        if stem in por_base:
            culpadas.add(por_base[stem])
        elif _RX_PASTA_SONDAS.search(arq):
            culpadas.update(injetadas)
    return culpadas


# verificações que a suíte e o benchmark cobrem sozinhos; 'revisao' não é cobertura dinâmica
COBRIVEIS_NO_M1 = ("suite", "benchmark")


def resultados_suite_por_item(itens: list[dict], blocos: list[dict],
                              cobertas: tuple[str, ...] | set[str] = ("suite",),
                              motivos_ausentes: dict[str, str] | None = None) -> dict[str, tuple[str, str | None]]:
    """Resultado de cada item do catálogo que cita blocos da suíte (`suite:<nome exato do bloco>`).

    - algum bloco citado falhou → falhou;
    - algum bloco citado não existe → nao_testavel listando os ausentes (mesmo que outros passem), com
      o motivo conhecido de cada um em `motivos_ausentes` (ex.: a sonda não compilou neste alvo);
    - todos passaram → passou só se toda a `verificacao` do item foi coberta nesta sprint (`cobertas`,
      mais `sonda` quando o item cita um bloco de sonda do agente); senão nao_testavel "parcial",
      dizendo o que falta e para qual marco."""
    motivos_ausentes = motivos_ausentes or {}
    out: dict[str, tuple[str, str | None]] = {}
    for it in itens:
        nomes = list(dict.fromkeys(c[len("suite:"):] for c in it.get("cenarios", []) if c.startswith("suite:")))
        if not nomes:
            continue
        cobertas_item = set(cobertas) | ({"sonda"} if any(sondas.e_sonda(n) for n in nomes) else set())
        casados = [b for b in blocos if b["nome"] in nomes]
        presentes = {b["nome"] for b in casados}
        ausentes = [n for n in nomes if n not in presentes]
        restantes = [v for v in it.get("verificacao", []) if v not in cobertas_item]
        if any(b["falhas"] for b in casados):
            out[it["id"]] = ("falhou", None)
        elif ausentes:
            conhecidos = [f"{n}: {motivos_ausentes[n]}" for n in ausentes if n in motivos_ausentes]
            sem_motivo = [n for n in ausentes if n not in motivos_ausentes]
            partes = conhecidos + ([f"bloco da suíte não encontrado: {', '.join(sem_motivo)}"] if sem_motivo else [])
            out[it["id"]] = ("nao_testavel", "; ".join(partes))
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


def _veredito_da_fase(ultimo: dict, origem: str, pasta: Path) -> str:
    """Achado da E2E ou do serviço: o critério de aceite é o cenário (`e2e::<id>[::...]`) ou a checagem
    (`servico:<id>`) passar no alvo nesta sprint, pelo que a fase gravou no estado."""
    assin = ultimo.get("assinatura", "")
    passos = (_ler_json(pasta / "estado.json") or {}).get("passos") or {}
    if ultimo.get("fonte") == "e2e":
        cenario = assin[len("e2e::"):].split("::")[0] if assin.startswith("e2e::") else ""
        estado_ = ((passos.get(f"e2e:{origem}:{cenario}") or {}).get("detalhe") or {}).get("estado")
        return {"passou": "corrigido", "problema": "persiste"}.get(estado_, "nao_verificavel")
    checagem = assin[len("servico:"):] if assin.startswith("servico:") else ""
    for c in ((passos.get(f"servico:{origem}") or {}).get("detalhe") or {}).get("checagens") or []:
        if c.get("id") == checagem:
            return {True: "corrigido", False: "persiste"}.get(c.get("ok"), "nao_verificavel")
    return "nao_verificavel"


def _veredito_automatico(ultimo: dict, origem: str, pasta: Path) -> str:
    """Reverifica um achado de build/suíte num alvo pelo seu critério de aceite: 'corrigido' só quando
    o critério passa nesta sprint; 'persiste' quando o mesmo erro reaparece; senão 'nao_verificavel'."""
    assin = ultimo.get("assinatura", "")
    if ultimo.get("fonte") in ("e2e", "servico"):
        return _veredito_da_fase(ultimo, origem, pasta)
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
        # os blocos das sondas do agente ficam à parte, em "sondas"
        for b in s.get("blocos", []) + (s.get("sondas") or {}).get("blocos", []):
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
    """Reverificação dos achados abertos de fonte build/suíte/sonda/e2e/serviço: para cada alvo do achado que
    existe nesta sprint (quem compartilha a árvore usa a execução da origem), olha o build/suíte desta sprint,
    ou o cenário/checagem que a E2E/o serviço rodou nela.
    Vários alvos: vale o mais conservador."""
    por_nome = {a["nome"]: a for a in alvos_sprint}
    out: dict[str, str] = {}
    for reg in historico.get("itens", {}).values():
        ultimo = reg.get("ultimo") or {}
        if reg.get("estado") not in _ESTADOS_ABERTOS or ultimo.get("fonte") not in FONTES_REVERIFICADAS:
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
# achados mecânicos: a evidência é a saída da ferramenta, não um julgamento
FONTES_MECANICAS = ("build", "suite", "e2e", "servico")
# só as fases da própria CLI produzem estas fontes (a de sonda não é mecânica: passa pelo advogado)
FONTES_DA_CLI = FONTES_MECANICAS + ("sonda",)
# fontes que a consolidação reverifica sozinha pelo build/suíte/E2E/serviço desta sprint
FONTES_REVERIFICADAS = ("build", "suite", "sonda", "e2e", "servico")


def ler_brutos(pasta: Path) -> tuple[list[tuple[str, dict]], set[int], list[str]]:
    """Lê `achados-brutos/` e casa cada achado com seu veredito em `vereditos/<mesmo nome>`.

    Achado de julgamento (de subagente) sem veredito legível (ausente, JSON inválido, lista mais
    curta, objeto sem `resultado` válido) entra com `confianca: provavel` e `veredito: null`, e seu id()
    vai para o segundo retorno (o contador "sem verificação adversarial"). Achado mecânico (`fonte`
    build/suite) sem veredito mantém a confiança e não entra no contador; um veredito presente vale
    para ele como para qualquer outro (ex.: `derrubado`). Veredito legível promove/rebaixa a
    `confianca` do achado (spec §11.3): `confirmado` vira `confirmado`, `provavel` vira `provavel`;
    `derrubado` não mexe na confiança (o achado sai do PDF em `achados.consolidar`). Veredito
    ilegível e achado bruto ilegível viram mensagens para `erros_agente.json`. Nunca levanta
    exceção por conteúdo ruim."""
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
                if a.get("fonte") not in FONTES_MECANICAS:
                    a["confianca"] = "provavel"
                    sem_verificacao.add(id(a))
            elif veredito["resultado"] in ("confirmado", "provavel"):
                # a verificação adversarial vale mais que a confiança de quem achou (spec §11.3):
                # confirmado promove, provável rebaixa. Derrubado sai do PDF em achados.consolidar,
                # então sua confiança não importa mais.
                a["confianca"] = veredito["resultado"]
            entradas.append((origem, a))
    return entradas, sem_verificacao, mensagens


def conferir_referencias(lista: list[dict], ids_catalogo: set[str] | None,
                         alvos_sprint: dict[str, str] | None) -> list[str]:
    """Referências de um achado a registrar: item do catálogo existente (quando há catálogo),
    alvo = um `nome` de alvos.json da sprint e commit = o commit desse alvo. As fontes `build` e
    `suite` são reservadas: só as fases mecânicas da própria CLI produzem esses achados."""
    erros: list[str] = []
    for i, a in enumerate(lista):
        if a.get("fonte") in FONTES_DA_CLI:
            erros.append(f"achado {i}: fonte '{a['fonte']}' é reservada às fases da CLI "
                         "(`sprint compilar`, `sprint suite`...); use o nome do agente que encontrou o achado")
    if alvos_sprint is None:
        return erros + ["alvos.json da sprint ausente: rode `sprint preparar` antes de registrar achados"]
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

def _restaurar_ambiente(e: estado.Estado, backup: Path, quem: str, extra: dict | None = None) -> dict:
    """Restaura o %APPDATA%\\MAW do backup, confere o manifesto, apaga a bandeira appdata-sujo.json
    e depois o backup (que contém a chave do Gemini). Registra tudo no estado da sprint."""
    reg: dict = {"quem": quem, **(extra or {}), "backup": str(backup), "verificado": False, "erro": None,
                 "backup_apagado": False}
    if suite.maw_aberta():
        # uma MAW aberta (a do usuário, aberta no meio da fase) gravaria por cima, ao sair, as configurações
        # de teste que tem na memória — e o único backup já teria sido apagado. Nada é tocado: a bandeira e
        # o backup ficam, e a próxima retomada restaura (fases.restaurar_pendencias_ambiente).
        reg.update(adiada=True, erro=_MOTIVO_ADIADA)
        _acrescentar_limitacoes(e, "ambiente", [_MOTIVO_ADIADA])
        e.registrar_restauracao(reg)
        return reg
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
        _retirar_limitacao(e, "ambiente", _MOTIVO_ADIADA)  # se tinha sido adiada, não está mais
        _retirar_limitacao(e, "ambiente", _MOTIVO_OCUPADA)
    e.registrar_restauracao(reg)
    return reg


_MOTIVO_ADIADA = ("restauração do %APPDATA%\\MAW adiada: MAW aberta — feche a MAW e rode "
                  "`sprint iniciar` de novo")
# Toda mudança no %APPDATA%\\MAW (sessão de GUI, suíte, calibração, restauração) acontece com a trava da
# máquina na mão (`trava_appdata`); quanto esperar que outra sessão do agente a solte.
ESPERA_TRAVA_APPDATA = 1800.0
_MOTIVO_OCUPADA = ("restauração do %APPDATA%\\MAW adiada: outra sessão do agente estava usando a MAW (trava "
                   "ocupada) — rode `sprint iniciar` de novo quando ela terminar")


BANDEIRA = "appdata-sujo.json"


def _pastas_com_pendencia() -> list[Path]:
    """Pastas de sprint com appdata-sujo.json, da mais antiga para a mais nova."""
    return [p for p in estado._existentes(config.RELATORIOS)
            if (p / BANDEIRA).exists() and (p / "estado.json").exists()]


def _bandeira_avulsa() -> Path:
    """A bandeira do driver de GUI numa sessão fora de sprint: ao lado da pasta dos backups."""
    return Path(config.BACKUPS).parent / BANDEIRA


def _bandeira_calibracao() -> Path:
    """A bandeira da calibração rodada fora de sprint."""
    return Path(config.WORK) / "calibracao" / BANDEIRA


def pendencias_ambiente() -> list[dict]:
    """Toda bandeira appdata-sujo.json ainda presente, de todos os lugares que sujam o %APPDATA%\\MAW:
    as pastas das sprints, a sessão avulsa do driver de GUI e a calibração fora de sprint.

    Cada uma: {"flag": Path, "origem": "sprint"|"avulsa"|"calibracao", "desde": str | None,
    "backup": Path | None} (+ "erro" quando a bandeira é ilegível), da mais antiga (`desde`) para a
    mais nova. Uma bandeira ilegível ou sem `desde` legível fica na frente: sem saber a idade dela,
    nenhuma outra pode ser tomada pelo estado original."""
    candidatas = [(p / BANDEIRA, "sprint") for p in _pastas_com_pendencia()]
    candidatas += [(b, o) for b, o in ((_bandeira_avulsa(), "avulsa"), (_bandeira_calibracao(), "calibracao"))
                   if b.is_file()]
    out = []
    for flag, origem in candidatas:
        ent: dict = {"flag": flag, "origem": origem, "desde": None, "backup": None}
        try:
            d = json.loads(flag.read_text(encoding="utf-8"))
            ent["backup"] = Path(d["backup"])
            ent["desde"] = str(d["desde"]) if d.get("desde") else None
        except (OSError, ValueError, KeyError, TypeError) as ex:
            ent["erro"] = f"{BANDEIRA} ilegível: {ex}"
        out.append(ent)
    def idade(ent: dict) -> str:
        try:  # `desde` que não é um instante legível conta como de idade desconhecida: vai para a frente
            time.strptime(ent["desde"] or "", "%Y-%m-%dT%H:%M:%S")
            return ent["desde"]
        except ValueError:
            return ""

    # sorted é estável: no empate fica a ordem sprints (por número) → avulsa → calibração
    return sorted(out, key=idade)


def _backup_da_bandeira(pasta: Path) -> Path:
    return Path(json.loads((Path(pasta) / BANDEIRA).read_text(encoding="utf-8"))["backup"])


def _restaurar_pendente(e) -> dict:
    """Restaura o %APPDATA%\\MAW do backup citado na bandeira appdata-sujo.json da pasta de `e`
    (sem olhar as outras bandeiras; quem quer a regra da mais antiga usa
    `restaurar_pendencias_ambiente`)."""
    try:
        backup = _backup_da_bandeira(e.pasta)
    except (OSError, ValueError, KeyError, TypeError) as ex:
        reg = {"quem": "retomada", "backup": None, "verificado": False, "backup_apagado": False,
               "erro": f"{BANDEIRA} ilegível: {ex}"}
        e.registrar_restauracao(reg)
        return reg
    return _restaurar_ambiente(e, backup, "retomada")


def _descrever_bandeira(ent: dict) -> str:
    if ent["origem"] == "sprint":
        return f"{ent['flag'].parent.name}/{BANDEIRA}"
    return f"{ent['origem']}: {ent['flag']}"


def _de_onde(ent: dict) -> str:
    return {"sprint": ent["flag"].parent.name, "avulsa": "a sessão avulsa do driver de GUI",
            "calibracao": "a calibração fora de sprint"}[ent["origem"]]


class _RegistroForaDeSprint:
    """Registro de quem sujou o ambiente fora de uma sprint (sessão avulsa, calibração): a pasta é a
    da bandeira; as restaurações ficam na memória (e na sprint atual, quando houver)."""

    def __init__(self, pasta: Path):
        self.pasta = Path(pasta)
        self.restauracoes: list[dict] = []

    def registrar_restauracao(self, registro: dict) -> None:
        self.restauracoes.append({"quando": _agora(), **registro})


def _processar_pendencias(motivo: str, atual: estado.Estado | None) -> list[tuple[str, dict]]:
    """Núcleo de `restaurar_pendencias_ambiente`: [(categoria, registro)] na ordem em que aconteceram.
    Com pendência, tudo acontece com a trava do %APPDATA%\\MAW na mão (reentrante: quem já a tem, como a
    sessão de GUI, entra na hora); sem a trava no prazo, tudo é adiado sem tocar em nada."""
    if not pendencias_ambiente():
        return []
    posse = trava_appdata.adquirir(ESPERA_TRAVA_APPDATA)
    try:
        return _processar_pendencias_na_trava(motivo, atual, ocupada=posse is None)
    finally:
        if posse is not None:
            posse.liberar()


def _processar_pendencias_na_trava(motivo: str, atual: estado.Estado | None,
                                   ocupada: bool) -> list[tuple[str, dict]]:
    def registro(ent: dict):
        pasta = ent["flag"].parent
        if atual is not None and Path(atual.pasta).resolve() == pasta.resolve():
            return atual
        if ent["origem"] == "sprint":
            return estado.carregar(pasta)
        return _RegistroForaDeSprint(pasta)

    def anotar(ent: dict, reg_obj, reg: dict) -> dict:
        """O registro vai para o dono da bandeira; o de fora de sprint também vai para a sprint atual
        (é ela que declara no PDF o que foi feito com o %APPDATA%\\MAW)."""
        if ent["origem"] != "sprint" and atual is not None and reg_obj is not atual:
            atual.registrar_restauracao({**reg, "origem": ent["origem"], "bandeira": str(ent["flag"])})
        sprint = ent["flag"].parent.name if ent["origem"] == "sprint" else None
        return {"sprint": sprint, "origem": ent["origem"], "flag": str(ent["flag"]), "desde": ent["desde"], **reg}

    pend = pendencias_ambiente()  # de novo, com a trava na mão: quem a segurava pode ter limpado a dele
    if not pend:
        return []
    if atual is None:  # quem chamou não tem a sprint em mãos: a em andamento (se houver) declara no PDF
        try:
            atual = estado.em_andamento(config.RELATORIOS)
        except (OSError, ValueError, KeyError):
            atual = None
    out: list[tuple[str, dict]] = []
    base = {"quem": "retomada", "motivo": motivo}
    if ocupada or suite.maw_aberta():
        # a MAW aberta gravaria por cima, ao salvar, as configurações que tem na memória; outra sessão do
        # agente com a trava está no meio do próprio ambiente de teste: nada é tocado
        adiada = _MOTIVO_OCUPADA if ocupada else _MOTIVO_ADIADA
        for ent in pend:
            reg_obj = registro(ent)
            reg = {**base, "backup": str(ent["backup"]) if ent["backup"] else None, "verificado": False,
                   "backup_apagado": False, "adiada": True, "erro": adiada}
            reg_obj.registrar_restauracao(reg)
            if isinstance(reg_obj, estado.Estado):
                _acrescentar_limitacoes(reg_obj, "ambiente", [adiada])
            out.append(("adiadas", anotar(ent, reg_obj, reg)))
        if atual is not None:
            _acrescentar_limitacoes(atual, "ambiente", [adiada])
        return out
    # só o backup MAIS ANTIGO foi tirado antes de o agente sujar o ambiente; um mais novo pode ter
    # copiado um estado já sujo. Os mais novos só são descartados depois da restauração verificada.
    mais_antiga, *mais_novas = pend
    reg_obj = registro(mais_antiga)
    if mais_antiga["backup"] is None:
        reg = {**base, "backup": None, "verificado": False, "backup_apagado": False, "erro": mais_antiga["erro"]}
        reg_obj.registrar_restauracao(reg)
    else:
        reg = _restaurar_ambiente(reg_obj, mais_antiga["backup"], "retomada", {"motivo": motivo})
    out.append(("restauradas" if reg["verificado"] else "erros", anotar(mais_antiga, reg_obj, reg)))
    if not reg["verificado"]:
        return out
    for ent in mais_novas:
        reg_obj = registro(ent)
        descarte = {**base, "backup": str(ent["backup"]) if ent["backup"] else None, "verificado": True,
                    "backup_apagado": False,
                    "descartado": f"o backup mais antigo, de {_de_onde(mais_antiga)}, é o estado original e já "
                                  "foi restaurado; este backup, mais novo, foi apagado sem ser restaurado"}
        if ent["backup"] is not None:
            try:
                sandbox.descartar_backup(ent["backup"])
                descarte["backup_apagado"] = True
            except OSError as ex:
                descarte["erro_ao_apagar"] = redacao.redigir(str(ex))
        sandbox.remover(ent["flag"])
        if isinstance(reg_obj, estado.Estado):
            _retirar_limitacao(reg_obj, "ambiente", _MOTIVO_ADIADA)
        reg_obj.registrar_restauracao(descarte)
        out.append(("descartadas", anotar(ent, reg_obj, descarte)))
    if atual is not None and not pendencias_ambiente():
        _retirar_limitacao(atual, "ambiente", _MOTIVO_ADIADA)
    return out


def restaurar_pendencias_ambiente(motivo: str, atual: estado.Estado | None = None) -> dict:
    """Restaura o %APPDATA%\\MAW deixado sujo por execuções que caíram, olhando as bandeiras de todos
    os lugares (`pendencias_ambiente`). `motivo` diz quem pediu (vai para cada registro).

    - MAW aberta (`suite.maw_aberta()`): nada é restaurado nem apagado; todas vão para "adiadas",
      com o adiamento registrado (e nas limitações da sprint).
    - Senão, só o backup da bandeira mais antiga (`desde`) é restaurado, com o manifesto conferido;
      bandeira e backup só são apagados depois da restauração verificada. As mais novas são então
      descartadas (backup apagado sem restaurar, bandeira removida), e isso fica registrado. Se a mais
      antiga falhar (ou for ilegível), vai para "erros" e as outras ficam intocadas.

    Os registros das sprints vão para o `estado.json` de cada uma (`atual`, se for a mesma pasta, é a
    instância que o chamador tem em mãos); os de fora de sprint vão para `atual` ou, sem ele, para a
    sprint em andamento (se houver), que os declara no PDF.
    Devolve {"restauradas", "descartadas", "adiadas", "erros"}: listas de registros."""
    out: dict[str, list[dict]] = {"restauradas": [], "descartadas": [], "adiadas": [], "erros": []}
    for categoria, reg in _processar_pendencias(motivo, atual):
        out[categoria].append(reg)
    return out


def _restaurar_pendentes(atual: estado.Estado | None = None) -> list[dict]:
    """Compatibilidade: o mesmo que `restaurar_pendencias_ambiente("retomada", atual)`, como uma
    lista de registros na ordem em que aconteceram (cada um com "sprint": nome ou None)."""
    return [reg for _, reg in _processar_pendencias("retomada", atual)]


def _situacao_do_ambiente(e: estado.Estado) -> tuple[bool, str | None]:
    """Restaurado = nenhuma bandeira pendente (em nenhum lugar) e a última restauração de cada
    backup desta sprint verificada."""
    erros = [f"restauração do %APPDATA%\\MAW pendente ({_descrever_bandeira(p)})" for p in pendencias_ambiente()]
    ultima: dict[str, dict] = {}
    for r in e.restauracoes:
        ultima[str(r.get("backup"))] = r
    for b, r in ultima.items():
        if not r.get("verificado"):
            erros.append(r["erro"] if r.get("adiada") else
                         f"restauração de {b} falhou: {r.get('erro') or 'sem mensagem de erro'}")
    return (not erros, "; ".join(dict.fromkeys(erros)) or None)


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


def _retirar_limitacao(e: estado.Estado, nome: str, linha: str) -> None:
    p = e.pasta / f"limitacoes-{nome}.json"
    atuais = _ler_json(p, [])
    if linha in atuais:
        sandbox.escrever_json(p, [l for l in atuais if l != linha])


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
    e = None if args.nova else estado.em_andamento()
    retomada = e is not None
    e = e or estado.nova_sprint()
    # antes de qualquer fase: o ambiente volta ao original (bandeiras de todos os lugares); o que foi
    # feito com bandeiras de fora de sprint fica registrado nesta sprint
    restauracoes = [reg for _, reg in _processar_pendencias("sprint iniciar", e)]
    e = estado.carregar(e.pasta)
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


# ---------- sondas do agente no build Debug ----------

# o que `sondas.injetar` pode levantar por conteúdo ou disco (ParseError do XML é SyntaxError); a
# sandbox recusando a escrita não entra: isso é erro do agente e tem de aparecer
_ERROS_DE_SONDA = (OSError, ValueError, SyntaxError)


def _nomes_das_sondas(arquivo: Path) -> list[str]:
    try:
        return sondas.nomes(arquivo)
    except (OSError, UnicodeDecodeError):
        return []


def _mapa_sondas(e: estado.Estado) -> dict[str, list[str]]:
    """O mapa sonda → itens (`privado/sondas/itens.yaml`); ilegível vira limitação e mapa vazio."""
    try:
        return sondas.carregar_mapa()
    except (OSError, ValueError, AttributeError, yaml.YAMLError) as ex:
        _acrescentar_limitacoes(e, "sondas", [f"mapa de itens das sondas ilegível: {ex}"])
        return {}


def _gravar_sondas(e: estado.Estado, alvo: str, info: dict) -> None:
    p = e.pasta / "sondas.json"
    sandbox.escrever_json(p, {**(_ler_json(p, {}) or {}), alvo: info})


def _marcar_itens_sem_sonda(e: estado.Estado, alvo: str, ausentes: dict[str, str]) -> None:
    """Itens do catálogo cobertos por uma sonda que não vai rodar neste alvo: nao_testavel com o motivo
    (a fase `suite` refaz a célula depois, com o mesmo motivo para a sonda ausente)."""
    mapa = _mapa_sondas(e)
    for nome, motivo in ausentes.items():
        for item in mapa.get(nome, []):
            catalogo.registrar_resultado(e.pasta, item, alvo, "nao_testavel", f"{nome}: {motivo}", fonte="sonda")


def _injetar_sondas(e: estado.Estado, alvo: str, wt: Path) -> tuple[list[Path], dict | None]:
    """Antes do build Debug, as sondas do privado entram na worktree do alvo (`sondas.injetar` é uma
    sincronização idempotente; o grupo do projeto só vale no Debug). Devolve (arquivos injetados,
    registro para sondas.json), ou ([], None) quando o privado não tem sondas. Injeção que falha vira
    limitação, o projeto volta ao original e o build segue sem sondas."""
    arquivos = sondas.listar()
    if not arquivos:
        if (Path(wt) / sondas.PASTA_NA_WORKTREE).exists():  # sondas que saíram do privado saem da worktree
            try:
                sondas.injetar(wt, [])
            except _ERROS_DE_SONDA as ex:
                _acrescentar_limitacoes(e, "sondas", [f"sondas antigas não retiradas de {alvo}: "
                                                      f"{redacao.redigir(str(ex))}"])
        return [], None
    try:
        r = sondas.injetar(wt, arquivos)
    except _ERROS_DE_SONDA as ex:  # projeto ausente, XML inválido, disco
        lim = f"sondas não injetadas em {alvo}: {redacao.redigir(str(ex))}"
        try:
            sondas.injetar(wt, [])  # o projeto volta a ser o original
        except _ERROS_DE_SONDA:
            pass
        _acrescentar_limitacoes(e, "sondas", [lim])
        return [], {"injetadas": [], "puladas": {}, "excluidas": {}, "erro": lim,
                    "ausentes": {n: lim for a in arquivos for n in _nomes_das_sondas(a)}}
    por_nome = {a.name: a for a in arquivos}
    ausentes: dict[str, str] = {}
    for arq, motivo in r["puladas"].items():
        for n in _nomes_das_sondas(por_nome[arq]):
            ausentes[n] = f"sonda pulada em {alvo}: {motivo}"
    _acrescentar_limitacoes(e, "sondas", [f"sonda {arq} pulada em {alvo}: {m}" for arq, m in r["puladas"].items()])
    return ([por_nome[x] for x in r["injetadas"]],
            {"injetadas": list(r["injetadas"]), "puladas": dict(r["puladas"]), "excluidas": {}, "erro": None,
             "ausentes": ausentes})


def _guardar_log_da_tentativa(r: build.ResultadoBuild, tentativa: int) -> str | None:
    """O log do build que falhou com as sondas é sobrescrito pela nova tentativa: fica uma cópia."""
    origem = Path(r.log)
    if not origem.is_file():
        return None
    destino = origem.with_name(f"{origem.stem}-com-sondas-{tentativa}{origem.suffix}")
    sandbox.copiar(origem, destino)
    return destino.name


def _compilar_debug_com_sondas(e: estado.Estado, alvo: str, wt: Path, logs: Path,
                               injetadas: list[Path], info: dict) -> build.ResultadoBuild:
    """Build Debug com as sondas. Se ele falha com sonda injetada, a falha só é da MAW quando continua
    sem as sondas:
    - as sondas citadas nos erros (arquivo ou .obj) saem e o Debug é compilado de novo; cada uma vira
      a limitação "sonda não compila em <alvo>: ..." e os itens dela ficam nao_testavel;
    - erro sem sonda citada, com sondas ainda injetadas: todas saem para isolar a falha. Se sem elas
      compila, as sondas eram a causa (erro num cabeçalho da MAW, por exemplo); se não, a falha é da
      MAW e vira achado de build com os erros do build SEM sondas.
    Cada tentativa tira ao menos uma sonda, então são no máximo len(injetadas) + 1 builds."""
    r = build.compilar(alvo, wt, "Debug", logs)
    por_nome = {p.name: p for p in injetadas}
    atuais = sorted(por_nome)
    tentativa = 0
    while not r.ok and atuais:
        tentativa += 1
        culpadas = sondas_nos_erros(r.erros, atuais)
        suspeitas = sorted(culpadas or atuais)
        log = _guardar_log_da_tentativa(r, tentativa)
        onde = f" (log {log})" if log else ""
        detalhe = {}
        for arq in suspeitas:
            linhas = [l for l in r.erros if Path(arq).stem.lower() in l.lower()] if culpadas else []
            detalhe[arq] = ("; ".join((linhas or r.erros)[:3]) or "sem mensagem de erro") + onde
        atuais = [x for x in atuais if x not in suspeitas]
        try:
            sondas.injetar(wt, [por_nome[x] for x in atuais])
        except _ERROS_DE_SONDA as ex:
            _acrescentar_limitacoes(e, "sondas", [f"sondas não retiradas do build Debug de {alvo} para isolar a "
                                                  f"falha: {redacao.redigir(str(ex))}"])
            break
        r = build.compilar(alvo, wt, "Debug", logs)
        if culpadas or r.ok:  # a sonda não compila (citada nos erros, ou sem ela o Debug compila)
            for arq in suspeitas:
                motivo = f"sonda não compila em {alvo}: {arq}: {detalhe[arq]}"
                info["excluidas"][arq] = motivo
                for n in _nomes_das_sondas(por_nome[arq]):
                    info["ausentes"][n] = motivo
                _acrescentar_limitacoes(e, "sondas", [motivo])
        else:  # sem as sondas o Debug também falha: a falha é da MAW
            for arq in suspeitas:
                motivo = f"sonda retirada do build Debug de {alvo} para isolar uma falha da MAW"
                info["excluidas"][arq] = motivo
                for n in _nomes_das_sondas(por_nome[arq]):
                    info["ausentes"][n] = motivo
            _acrescentar_limitacoes(e, "sondas", [
                f"sondas retiradas do build Debug de {alvo} para isolar a falha, que continuou sem elas: a falha é "
                f"da MAW ({', '.join(suspeitas)})"])
    info["injetadas"] = atuais
    return r


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
            logs = e.pasta / "evidencias" / "builds"
            info_sondas = None
            if cfg == "Debug":  # o Release (E2E, benchmark) nunca leva sonda
                injetadas, info_sondas = _injetar_sondas(e, a["nome"], wt)
            if info_sondas is not None:
                r = _compilar_debug_com_sondas(e, a["nome"], wt, logs, injetadas, info_sondas)
                _gravar_sondas(e, a["nome"], info_sondas)
                _marcar_itens_sem_sonda(e, a["nome"], info_sondas["ausentes"])
            else:
                r = build.compilar(a["nome"], wt, cfg, logs)
            dados_build = r.como_dict()
            if info_sondas and info_sondas["excluidas"]:
                dados_build["sondas_excluidas"] = info_sondas["excluidas"]
            sandbox.escrever_json(e.pasta / "builds" / f"{a['nome']}-{cfg}.json", dados_build)
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


def _detalhes_das_sondas(detalhes: list[str]) -> list[str]:
    """As linhas de 'Detalhe das falhas' dos blocos das sondas (o complemento do que
    `sondas.separar_blocos` deixa na parte da MAW)."""
    out, dentro = [], False
    for linha in detalhes:
        m = _RX_DETALHE_CABECALHO.match(linha)
        if m:
            dentro = sondas.e_sonda(m.group(1))
        if dentro:
            out.append(linha)
    return out


def _motivos_sondas_ausentes(e: estado.Estado, alvo: str) -> dict[str, str]:
    """Por que cada sonda não rodou neste alvo (nome da sonda → motivo), pelo `sondas.json` da sprint:
    gravado pelo `compilar` (com `ausentes`) ou pelo `sondas injetar` (só `puladas`/`erro`)."""
    info = (_ler_json(e.pasta / "sondas.json", {}) or {}).get(alvo) or {}
    if "ausentes" in info:
        return dict(info["ausentes"] or {})
    arquivos = {a.name: a for a in sondas.listar()}
    if info.get("erro"):
        return {n: f"sondas não injetadas em {alvo}: {info['erro']}" for a in arquivos.values()
                for n in _nomes_das_sondas(a)}
    return {n: f"sonda pulada em {alvo}: {motivo}" for arq, motivo in (info.get("puladas") or {}).items()
            if arq in arquivos for n in _nomes_das_sondas(arquivos[arq])}


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
        # os blocos "SONDA ..." são do agente: nunca contam contra a suíte existente da MAW
        da_maw, blocos_sondas = sondas.separar_blocos(d)
        if blocos_sondas:
            da_maw["sondas"] = {"blocos": blocos_sondas, "total_ok": sum(b["ok"] for b in blocos_sondas),
                                "total_falhas": sum(b["falhas"] for b in blocos_sondas),
                                "detalhes": _detalhes_das_sondas(d.get("detalhes", []))}
        sandbox.escrever_json(e.pasta / "suites" / f"{nome}.json", da_maw)
        for i, ach in enumerate(achados_da_suite(a, da_maw)):
            sandbox.escrever_json(e.pasta / "achados-brutos" / f"suite-{nome}-{i:03d}.json", ach)
        mapa = _mapa_sondas(e)
        arquivos = {n: arq.name for arq in sondas.listar() for n in _nomes_das_sondas(arq)}
        injetadas = ((_ler_json(e.pasta / "sondas.json", {}) or {}).get(nome) or {}).get("injetadas")
        if da_maw.get("incoerencias") and (blocos_sondas or injetadas):
            _acrescentar_limitacoes(e, "sondas", [
                f"a suíte de {nome} ficou incoerente com as sondas do agente injetadas: o relatório não permite "
                "separar se a causa é da MAW ou de uma sonda"])
        for i, ach in enumerate(achados_das_sondas(a, blocos_sondas, d.get("detalhes", []), mapa, arquivos)):
            sandbox.escrever_json(e.pasta / "achados-brutos" / f"sonda-{nome}-{i:03d}.json", ach)
        _acrescentar_limitacoes(e, "sondas", sorted({
            f"sonda '{b['nome']}' falhou em {nome} e não está no mapa de itens das sondas: o achado ficou em "
            "saude/geral" for b in blocos_sondas if b["falhas"] and b["nome"] not in mapa}))
        catalogo.registrar_resultado(e.pasta, "saude/suite-existente", nome,
                                     "passou" if da_maw["passou"] and not d["assercoes"] else "falhou", fonte="suite")
        cobertas = COBRIVEIS_NO_M1 if benchmark_ok else ("suite",)
        # todos os blocos (da MAW e das sondas): um item pode citar os dois
        for item, (res, motivo) in resultados_suite_por_item(itens, d["blocos"], cobertas,
                                                             _motivos_sondas_ausentes(e, nome)).items():
            catalogo.registrar_resultado(e.pasta, item, nome, res, motivo, fonte="suite")
    else:
        _registrar_suite_nao_rodou(e, nome, itens, _MOTIVO_BUILD.format(cfg="Debug"))
    detalhe = {cfg.lower(): (valido[cfg][1] or "binário desta sprint") for cfg in _CONFIGS}
    e.concluir(passo, detalhe)
    return {"alvo": nome, **detalhe}


def _rodar_suites(e: estado.Estado, pendentes: list[dict], itens: list[dict],
                  backup: Path) -> tuple[list[dict], dict]:
    """(backup do %APPDATA%\\MAW já feito) → bandeira appdata-sujo.json → roda → restaura e confere →
    apaga a bandeira → apaga o backup. A pasta de música é listada antes e depois."""
    env = suite.ambiente_com_ffmpeg(dict(os.environ), config.FERRAMENTAS / "ffmpeg" / "bin")
    musica_antes = _arquivos_musica()
    resumo: list[dict] = []
    try:
        sandbox.escrever_json(e.pasta / "appdata-sujo.json", {"backup": str(backup), "desde": _agora()})
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
    # itens cobertos por sonda citam o bloco dela mesmo que o catálogo ainda não o tenha
    itens = itens_com_sondas(_itens_catalogo(), _mapa_sondas(e))
    todos = _alvos(e)
    pendentes = [a for a in _alvos(e, args.alvo)
                 if not a["compartilha_com"] and not e.feito(f"suite:{a['nome']}")]
    saida: dict = {"suites": []}
    ok = True
    if pendentes:
        recusa = None
        # a trava do %APPDATA%\\MAW do começo ao fim (backup → roda → restaura): nenhuma sessão de GUI,
        # calibração ou restauração de outro processo mexe nele no meio
        posse = trava_appdata.adquirir(ESPERA_TRAVA_APPDATA)
        try:
            if posse is None:
                recusa = (f"outra sessão do agente usou a MAW por mais de {ESPERA_TRAVA_APPDATA:g} s (trava do "
                          "%APPDATA%\\MAW ocupada): a suíte não rodou")
            elif suite.maw_aberta():
                recusa = _MOTIVO_MAW_ABERTA  # e o %APPDATA%\MAW nem é tocado
            else:
                restaurar_pendencias_ambiente("sprint suite", e)
                sujas = pendencias_ambiente()
                if sujas:  # em qualquer lugar: a suíte nunca faz backup de um ambiente já sujo
                    recusa = (f"restauração pendente do %APPDATA%\\MAW ({', '.join(map(_descrever_bandeira, sujas))}) "
                              "não foi concluída: a suíte não roda sobre um ambiente sujo")
            backup = None
            if recusa is None:
                try:
                    backup = sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
                except OSError as ex:  # sem backup, a MAW não roda
                    recusa = f"não foi possível fazer o backup do %APPDATA%\\MAW: {ex}"
            if backup is not None:
                saida["suites"], restauracao = _rodar_suites(e, pendentes, itens, backup)
                saida["ambiente_restaurado"] = restauracao["verificado"]
                ok = restauracao["verificado"]
        finally:
            if posse is not None:
                posse.liberar()
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
    p.add_argument("acao", choices=["validar", "rascunho", "publicar"])
    p.add_argument("rascunho", nargs="?",
                   help="caminho do rascunho.yaml: destino da cópia ('rascunho', padrão "
                        "privado/catalogo/funcionalidades.rascunho.yaml) ou arquivo a publicar ('publicar')")


def _validar_catalogo_completo(itens: list[dict], princ: list[dict]) -> list[str]:
    erros = catalogo.validar(itens)
    ids = {i["id"] for i in itens}
    for p in princ:
        if f"principio/{p['id']}" not in ids:
            erros.append(f"princípio {p['id']} sem item principio/{p['id']} no catálogo")
    for obrig in ("saude/build-release", "saude/build-debug", "saude/suite-existente", "saude/benchmark"):
        if obrig not in ids:
            erros.append(f"item obrigatório ausente: {obrig}")
    return erros


@registrar("catalogo", "valida ou publica o catálogo de funcionalidades", _cfg_catalogo)
def cmd_catalogo(args: argparse.Namespace) -> int:
    princ = catalogo.carregar(config.PRINCIPIOS) if config.PRINCIPIOS.exists() else []
    if args.acao == "rascunho":
        # o ponto de partida do catalogador: cópia do catálogo publicado, feita pelo sandbox (sem `cp`,
        # que a execução noturna não libera)
        destino = Path(args.rascunho) if args.rascunho else config.CATALOGO.with_name("funcionalidades.rascunho.yaml")
        sandbox.escrever_texto(destino, config.CATALOGO.read_text(encoding="utf-8"))
        return _saida({"ok": True, "rascunho": str(destino)})
    if args.acao == "publicar":
        if not args.rascunho:
            return _saida({"ok": False, "erros": ["informe o caminho do rascunho.yaml"]}, False)
        rascunho = Path(args.rascunho)
        if not rascunho.exists():
            return _saida({"ok": False, "erros": [f"rascunho não encontrado: {rascunho}"]}, False)
        try:
            itens = catalogo.carregar(rascunho)
        except yaml.YAMLError as ex:
            return _saida({"ok": False, "erros": [f"YAML inválido em {rascunho}: {ex}"]}, False)
        erros = _validar_catalogo_completo(itens, princ)
        if config.CATALOGO.exists():  # o catálogo só cresce: um rascunho parcial não apaga itens publicados
            publicado = catalogo.carregar(config.CATALOGO)
            atuais = ({i["id"] for i in publicado if isinstance(i, dict) and i.get("id")}
                      if isinstance(publicado, list) else set())
            sumidos = sorted(atuais - {i.get("id") for i in itens})
            if sumidos:
                erros.append(f"o rascunho perde {len(sumidos)} item(ns) já publicado(s): "
                             f"{', '.join(sumidos[:20])}{' …' if len(sumidos) > 20 else ''}")
        if erros:
            return _saida({"ok": False, "erros": erros}, False)
        # troca atômica: só grava depois de validar tudo, e sandbox.escrever_texto já
        # escreve num .tmp e faz os.replace por cima do arquivo final.
        sandbox.escrever_texto(config.CATALOGO, rascunho.read_text(encoding="utf-8"))
        return _saida({"ok": True, "itens": len(itens), "publicado": str(config.CATALOGO)})
    itens = catalogo.carregar(config.CATALOGO)
    erros = _validar_catalogo_completo(itens, princ)
    return _saida({"itens": len(itens), "principios": len(princ), "erros": erros}, not erros)


def _alvos_removidos(e: estado.Estado, hist_antes: dict) -> tuple[set[str], list[str]]:
    """Alvos dos achados em aberto do histórico que não estão nesta sprint e cuja branch sumiu do origin
    do espelho (lido só aqui, sem escrever; o `preparar` buscou com --prune). Sem `alvos.json` ou sem
    candidato, nada é lido. Espelho ilegível: nada é removido e o motivo vai para os erros do agente."""
    if not (e.pasta / "alvos.json").exists():
        return set(), []
    atuais = {a["nome"] for a in _alvos(e)}
    candidatos = {x["alvo"] for reg in hist_antes.get("itens", {}).values()
                  if reg.get("estado") in _ESTADOS_ABERTOS
                  for x in (reg.get("ultimo") or {}).get("alvos", [])} - atuais
    if not candidatos:
        return set(), []
    try:
        origin = alvos.branches_do_origin(config.ESPELHO)
    except (RuntimeError, OSError) as ex:
        return set(), [f"branches do origin ilegíveis no espelho ({ex}): nenhum achado marcado como alvo removido; "
                       f"os de {', '.join(sorted(candidatos))} ficaram sem reverificação"]
    return alvos.alvos_removidos(candidatos, atuais, origin), []


# ---------- achado da E2E derrubado: a matriz e a bancada seguem o veredito ----------

FONTE_VERIFICACAO = "verificacao"
MARCA_DERRUBADO = "(achado derrubado na verificação adversarial)"
# só as linhas que esta consolidação escreve (as de "verificacao" escritas à mão ficam)
_RX_LINHA_DERRUBADO = re.compile(r"(?:^|: )o cenário \S+ mediu errado " + re.escape(MARCA_DERRUBADO))


def _primeira_frase(texto: str, limite: int = 300) -> str:
    """A primeira frase (até o primeiro . ! ou ? seguido de espaço ou do fim), sem quebras de linha."""
    t = " ".join(str(texto or "").split())
    m = re.match(r"(.+?[.!?])(?=\s|$)", t)
    frase = (m.group(1) if m else t).strip()
    return frase if len(frase) <= limite else frase[:limite - 1].rstrip() + "…"


def motivo_derrubado(cenario: str, justificativa: str) -> str:
    frase = _primeira_frase(justificativa)
    return f"o cenário {cenario} mediu errado {MARCA_DERRUBADO}" + (f": {frase}" if frase else "")


def derrubados_da_e2e(brutos: list[dict]) -> dict[tuple[str, str], dict]:
    """(alvo, id do cenário) → {"motivo", "item"} de cada achado bruto da E2E com veredito 'derrubado'."""
    out: dict[tuple[str, str], dict] = {}
    for a in brutos:
        assin = a.get("assinatura") or ""
        veredito = a.get("veredito") or {}
        if a.get("fonte") != "e2e" or veredito.get("resultado") != "derrubado" or not assin.startswith("e2e::"):
            continue
        cenario = assin[len("e2e::"):].split("::")[0]
        for x in a.get("alvos") or []:
            out[(x["alvo"], cenario)] = {"motivo": motivo_derrubado(cenario, veredito.get("justificativa", "")),
                                         "item": a.get("item_catalogo")}
    return out


def _cenarios_declarados() -> tuple[dict[str, dict], list[str]]:
    """{id: {"itens", "bancada", "mira_main"}} do que cada cenário de privado/cenarios declara (importa os
    arquivos, não roda nada). Para passos gravados por uma fase e2e que ainda não guardava isso no passo."""
    try:
        return ({c.id: {"itens": list(c.itens), "bancada": list(c.bancada), "mira_main": e2e.cenario_mira_main(c)}
                 for c in e2e.descobrir(config.PRIVADO / "cenarios")}, [])
    except Exception as ex:
        return {}, [f"achados da E2E derrubados: os cenários de privado/cenarios não puderam ser lidos ({ex}); "
                    f"os itens e os códigos da bancada vieram do achado, do catálogo e de privado/bancada/mapa.yaml"]


def _passo_e2e(passos: dict, alvo: str, cenario: str) -> dict | None:
    p = passos.get(f"e2e:{alvo}:{cenario}")
    return p if p and p.get("status") == "concluido" else None


def _derrubado_vale(passos: dict, alvo: str, cenario: str) -> bool:
    """O veredito vale para o que está na sprint: o passo do cenário ainda é o 'problema' que gerou o achado
    (se ele rodou de novo e passou ou pulou, vale o que rodou). Passo ausente (tirado para refazer): vale."""
    p = _passo_e2e(passos, alvo, cenario)
    return p is None or (p.get("detalhe") or {}).get("estado") == "problema"


def _aplicar_derrubados_na_matriz(pasta: Path, derrubados: dict, passos: dict, info: dict, itens_cat: list[dict],
                                  alvos_lista: list[dict]) -> int:
    """Cada célula 'falhou' gravada pela E2E para um cenário derrubado (no alvo do achado e nos alvos que
    herdam a árvore dele) ganha uma linha 'nao_testavel' de fonte 'verificacao' com o motivo. Só a célula que
    ainda é a do cenário (fonte e2e, 'falhou' e o motivo = a nota do passo) muda."""
    res = catalogo.carregar_resultados(pasta)
    n = 0
    for (alvo, cenario), d in sorted(derrubados.items()):
        if not _derrubado_vale(passos, alvo, cenario):
            continue
        p = _passo_e2e(passos, alvo, cenario)
        det = (p or {}).get("detalhe") or {}
        if "itens" in det:
            itens = list(det["itens"])
        elif cenario in info:
            itens = info[cenario]["itens"]
        else:
            itens = sorted({d["item"]} | {it["id"] for it in itens_cat if cenario in (it.get("cenarios") or [])}
                           - {None})
        nota = det.get("nota") if p is not None else None
        herdeiros = [b["nome"] for b in alvos_lista if b.get("compartilha_com") == alvo]
        for item in itens:
            for nome in [alvo, *herdeiros]:
                cel = res.get((item, nome))
                if not cel or cel.get("fonte") != "e2e" or cel.get("resultado") != "falhou":
                    continue
                if nome == alvo:
                    esperado, motivo = (nota or None), d["motivo"]
                else:
                    esperado = f"mesma árvore de código de {alvo}" + (f": {nota}" if nota else "")
                    motivo = f"mesma árvore de código de {alvo}: {d['motivo']}"
                if p is not None and (cel.get("motivo") or None) != esperado:
                    continue  # a célula é de outro cenário com o mesmo item
                catalogo.registrar_resultado(pasta, item, nome, "nao_testavel", motivo, [], FONTE_VERIFICACAO)
                res[(item, nome)] = {"item": item, "alvo": nome, "resultado": "nao_testavel", "motivo": motivo,
                                     "achados": [], "fonte": FONTE_VERIFICACAO}
                n += 1
    return n


def _onde_contribui(cenario: str, alvo: str, det: dict, info: dict, mapa: dict, codigo: str) -> str | None:
    """Com que alvo o passo `e2e:<alvo>:<cenario>` entrou no código `codigo` da bancada (None: não entrou) —
    a mesma regra de `fase_e2e._alvo_para_bancada`: cenário que mira main só conta na execução em main."""
    if "bancada" in det:  # gravado pela própria fase e2e
        return det.get("bancada_alvo") if codigo in (det.get("bancada") or []) else None
    if cenario in info:
        if codigo not in info[cenario]["bancada"]:
            return None
        mira_main = info[cenario]["mira_main"]
    else:
        m = mapa.get(codigo) or {}
        if cenario not in (m.get("cenarios") or []):
            return None
        mira_main = m.get("onde") != "proxima"
    return alvo if (alvo == "main" or not mira_main) else None


def _codigos_do_cenario(cenario: str, det: dict, info: dict, mapa: dict) -> list[str]:
    if "bancada" in det:
        return list(det.get("bancada") or [])
    if cenario in info:
        return list(info[cenario]["bancada"])
    return [c for c, m in mapa.items() if cenario in ((m or {}).get("cenarios") or [])]


def _refazer_bancada(pasta: Path, derrubados: dict, passos: dict, info: dict, mapa: dict) -> list[str]:
    """Refaz, pela regra de sempre (o pior vence, notas juntas), cada código da bancada alimentado por um
    cenário derrubado, a partir do resultado de cada cenário no estado da sprint — o derrubado entra como
    'pulei' com o motivo. Também refaz o código que uma consolidação anterior refez e já não precisa (o
    veredito mudou): ele volta a ser o que a E2E registrou."""
    afetados: set[str] = set()
    for (alvo, cenario) in derrubados:
        p = _passo_e2e(passos, alvo, cenario)
        det = (p or {}).get("detalhe") or {}
        for codigo in _codigos_do_cenario(cenario, det, info, mapa):
            if p is not None and _onde_contribui(cenario, alvo, det, info, mapa, codigo) is not None:
                afetados.add(codigo)
    afetados |= {c for c, r in bancada.carregar(pasta).items() if MARCA_DERRUBADO in ((r or {}).get("nota") or "")}
    refeitos = []
    for codigo in sorted(afetados):
        contrib = []
        for chave, p in passos.items():
            if not chave.startswith("e2e:") or p.get("status") != "concluido" or chave.count(":") < 2:
                continue
            _, alvo, cenario = chave.split(":", 2)
            det = p.get("detalhe") or {}
            alvo_b = _onde_contribui(cenario, alvo, det, info, mapa, codigo)
            if alvo_b is None or det.get("estado") not in bancada.ESTADOS:
                continue
            est, nota = det["estado"], det.get("nota") or ""
            if est == "problema" and (alvo, cenario) in derrubados:
                est, nota = "pulei", derrubados[(alvo, cenario)]["motivo"]
            contrib.append((p.get("fim") or p.get("inicio") or "", (est, nota, alvo_b)))
        contrib.sort(key=lambda x: x[0])  # a ordem em que a E2E registrou
        if bancada.reconstruir(pasta, codigo, [c for _, c in contrib]) is not None:
            refeitos.append(codigo)
    return refeitos


def aplicar_derrubados_da_e2e(pasta: Path, brutos: list[dict]) -> tuple[dict, list[str]]:
    """O veredito 'derrubado' de um achado da E2E (o cenário mediu errado) chega à matriz e à bancada: as
    células 'falhou' do cenário viram 'nao_testavel' e os códigos dele na bancada são refeitos sem o
    'problema'. Idempotente: as linhas desta função são refeitas a cada consolidação (a limpeza é em
    `_consolidar`, antes da matriz) e a bancada é refeita do estado da sprint."""
    pasta = Path(pasta)
    derrubados = derrubados_da_e2e(brutos)
    passos = (_ler_json(pasta / "estado.json") or {}).get("passos") or {}
    ja_refeitos = any(MARCA_DERRUBADO in ((r or {}).get("nota") or "") for r in bancada.carregar(pasta).values())
    if not derrubados and not ja_refeitos:
        return {"celulas": 0, "bancada": []}, []
    # passos de uma fase e2e que ainda não gravava itens/bancada no passo (ou passo tirado para refazer):
    # o que cada cenário declara vem dos próprios arquivos de cenário
    precisa_info = (any(_passo_e2e(passos, a, c) is None for a, c in derrubados)
                    or any("bancada" not in (p.get("detalhe") or {}) for k, p in passos.items()
                           if k.startswith("e2e:") and p.get("status") == "concluido"))
    info, mensagens = _cenarios_declarados() if precisa_info else ({}, [])
    mapa_arq = config.PRIVADO / "bancada" / "mapa.yaml"
    try:
        mapa = (yaml.safe_load(mapa_arq.read_text(encoding="utf-8")) or {}) if mapa_arq.exists() else {}
    except (OSError, yaml.YAMLError) as ex:
        mapa = {}
        mensagens.append(f"achados da E2E derrubados: {mapa_arq.name} ilegível ({ex})")
    alvos_lista = (_ler_json(pasta / "alvos.json") or {}).get("alvos") or []
    n = _aplicar_derrubados_na_matriz(pasta, derrubados, passos, info, _itens_catalogo(), alvos_lista)
    codigos = _refazer_bancada(pasta, derrubados, passos, info, mapa) if (pasta / "bancada.json").exists() else []
    return {"celulas": n, "bancada": codigos}, mensagens


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
    # as linhas que a consolidação anterior escreveu para achados da E2E derrubados (refeitas no fim)
    catalogo.descartar_fonte(e.pasta, FONTE_VERIFICACAO,
                             se=lambda l: bool(_RX_LINHA_DERRUBADO.search(l.get("motivo") or "")))
    entradas, sem_verificacao, erros_leitura = ler_brutos(e.pasta)
    brutos, invalidos = separar_validos(entradas)
    sem_verif = sum(1 for a in brutos if id(a) in sem_verificacao)
    derivadas = reverificacoes_automaticas(hist_antes, _alvos(e) if (e.pasta / "alvos.json").exists() else [],
                                           e.pasta)
    rever, conflitos_rever = juntar_reverificacoes(e.pasta, incluir_base=False,
                                                   extras=[("automatico", derivadas)])
    removidos, erros_removidos = _alvos_removidos(e, hist_antes)
    sandbox.escrever_json(e.pasta / "reverificacoes.json", rever)
    erros = erros_leitura + invalidos + conflitos_rever + erros_removidos
    sandbox.escrever_json(e.pasta / "erros_agente.json", erros)
    # a promoção/rebaixamento de confiança pelo veredito já aconteceu em ler_brutos (spec §11.3)
    derrubados = [{"titulo": a["titulo"], "justificativa": a["veredito"].get("justificativa", "")}
                  for a in brutos if (a.get("veredito") or {}).get("resultado") == "derrubado"]
    principal = next(({"alvo": a["nome"], "commit": a["commit"]} for a in _alvos(e) if a["nome"] == "main"), None) \
        if (e.pasta / "alvos.json").exists() else None
    lista, hist = achados.consolidar(brutos, hist_antes, f"{e.numero:02d}", rever, removidos, principal)
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
    # depois dos 'falhou' acima: um achado confirmado de outra fonte no mesmo item continua mandando na célula
    derrubados_e2e, erros_derrubados = aplicar_derrubados_da_e2e(e.pasta, brutos)
    if erros_derrubados:
        sandbox.escrever_json(e.pasta / "erros_agente.json", erros + erros_derrubados)
    detalhe = {"achados": len(lista), "derrubados": len(derrubados), "achados_invalidos": len(invalidos),
               "sem_verificacao_adversarial": sem_verif, "reverificacoes_automaticas": len(derivadas),
               "alvos_removidos": sorted(removidos), "derrubados_e2e": derrubados_e2e}
    e.concluir("consolidar", detalhe)
    return _saida(detalhe)


def _encerrar(args) -> int:
    e = _sprint_atual()
    if e.feito("encerrar"):  # já encerrada: devolve o que foi registrado, sem restaurar nada de novo
        info = e.dados("encerrar") or {}
        return _saida({**info, "retomado": True},
                      bool(info.get("verificado")) and bool(info.get("ambiente_restaurado")))
    e.iniciar("encerrar")
    # caso algo tenha caído sem restaurar, em qualquer lugar (adiada se a MAW estiver aberta)
    restaurar_pendencias_ambiente("sprint encerrar", e)
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
