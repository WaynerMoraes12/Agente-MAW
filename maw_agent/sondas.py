"""Sondas C++ do agente: testes `juce::UnitTest` que entram na suíte do alvo só no build Debug.

`injetar(worktree, arquivos)` copia as sondas para `Source/Tests/Sondas/` da worktree do AGENTE e
mantém no `.vcxproj` um grupo rotulado com um `<ClCompile>` por sonda, condicionado ao Debug (o
Release, que o E2E e o benchmark usam, nunca leva sonda). É uma sincronização: chamar de novo com a
mesma lista não muda nada, uma lista menor tira o que sobrou e a lista vazia devolve o projeto
exatamente como era. O nome de exibição de cada sonda começa com "SONDA ", então o intérprete da
suíte e os cenários `suite:<nome exato>` do catálogo as enxergam como blocos comuns.

Uma sonda pode declarar do que precisa no alvo, em linhas `// SONDA-REQUER: <arquivo>: <ident> ...`;
se algum identificador não aparece no arquivo daquela worktree (branch antiga, API diferente), a
sonda é pulada naquele alvo em vez de quebrar o build dele.
"""
from __future__ import annotations
import copy
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from . import config, sandbox

PREFIXO = "SONDA "
ROTULO = "SondasDoAgente"
PASTA_NA_WORKTREE = Path("Source") / "Tests" / "Sondas"
PROJETO = Path("Builds") / "VisualStudio2022" / "MAW_APP_App.vcxproj"
PASTA_PRIVADA = config.PRIVADO / "sondas"
MAPA = PASTA_PRIVADA / "itens.yaml"

_RX_NOME = re.compile(r'juce::UnitTest\s*\(\s*"(' + re.escape(PREFIXO) + r'[^"]*)"')
_RX_REQUER = re.compile(r"^\s*//\s*SONDA-REQUER:\s*(?P<arq>[^:]+?)\s*:\s*(?P<ids>.+?)\s*$", re.MULTILINE)
_RX_GRUPO = re.compile(r'^[ \t]*<ItemGroup Label="' + ROTULO + r'"[^>]*>.*?</ItemGroup>[ \t]*\r?\n',
                       re.DOTALL | re.MULTILINE)


def listar(pasta: Path | None = None) -> list[Path]:
    pasta = Path(pasta or PASTA_PRIVADA)
    return sorted(pasta.glob("*.cpp")) if pasta.is_dir() else []


def nomes(arquivo: Path) -> list[str]:
    """Nomes de exibição ("SONDA ...") das classes de teste declaradas no arquivo."""
    return _RX_NOME.findall(Path(arquivo).read_text(encoding="utf-8"))


def requisitos(arquivo: Path) -> list[tuple[str, list[str]]]:
    texto = Path(arquivo).read_text(encoding="utf-8")
    return [(m["arq"].strip(), m["ids"].split()) for m in _RX_REQUER.finditer(texto)]


def carregar_mapa(caminho: Path | None = None) -> dict[str, list[str]]:
    """{nome de exibição da sonda: [ids do catálogo que ela cobre]}."""
    p = Path(caminho or MAPA)
    if not p.exists():
        return {}
    return {str(k): list(v or []) for k, v in (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).items()}


def e_sonda(nome_bloco: str) -> bool:
    return nome_bloco.startswith(PREFIXO)


def _falta_no_alvo(worktree: Path, arquivo: Path) -> str | None:
    for rel, ids in requisitos(arquivo):
        alvo = Path(worktree) / rel
        if not alvo.is_file():
            return f"{rel} não existe neste alvo"
        texto = alvo.read_text(encoding="utf-8", errors="replace")
        faltam = [i for i in ids if not re.search(r"\b" + re.escape(i) + r"\b", texto)]
        if faltam:
            return f"{rel} não tem {', '.join(faltam)}"
    return None


def _grupo(nomes_cpp: list[str], nl: str) -> str:
    linhas = [f'  <ItemGroup Label="{ROTULO}" Condition="\'$(Configuration)\'==\'Debug\'">']
    linhas += [f'    <ClCompile Include="..\\..\\Source\\Tests\\Sondas\\{n}"/>' for n in nomes_cpp]
    linhas.append("  </ItemGroup>")
    return nl.join(linhas) + nl


def _projeto_com(texto: str, nomes_cpp: list[str]) -> str:
    """O texto do projeto sem o grupo das sondas e, se houver sondas, com o grupo novo na linha
    antes do Import do Microsoft.Cpp.targets (ou antes de </Project>)."""
    limpo = _RX_GRUPO.sub("", texto)
    if not nomes_cpp:
        return limpo
    nl = "\r\n" if "\r\n" in limpo else "\n"
    m = (re.search(r"^[ \t]*<Import Project=\"\$\(VCTargetsPath\)\\Microsoft\.Cpp\.targets\"", limpo, re.MULTILINE)
         or re.search(r"^[ \t]*</Project>", limpo, re.MULTILINE))
    if m is None:
        raise ValueError("projeto sem </Project>: não sei onde pôr as sondas")
    return limpo[:m.start()] + _grupo(nomes_cpp, nl) + limpo[m.start():]


def injetar(worktree: Path, arquivos: list[Path]) -> dict:
    """Sincroniza `Source/Tests/Sondas/` e o grupo do `.vcxproj` com `arquivos` (só .cpp).
    Devolve {"injetadas": [...], "puladas": {arquivo: motivo}, "alterado": bool}."""
    worktree = Path(worktree)
    projeto = worktree / PROJETO
    if not projeto.is_file():
        raise FileNotFoundError(f"projeto não existe: {projeto}")
    arquivos = [Path(a) for a in arquivos]
    for a in arquivos:
        if a.suffix.lower() != ".cpp":
            raise ValueError(f"sonda tem de ser .cpp: {a.name}")
    destino = worktree / PASTA_NA_WORKTREE
    sandbox.garantir_escrita(destino)
    sandbox.garantir_escrita(projeto)

    injetadas: list[str] = []
    puladas: dict[str, str] = {}
    for a in arquivos:
        motivo = _falta_no_alvo(worktree, a)
        if motivo:
            puladas[a.name] = motivo
        else:
            injetadas.append(a.name)
    injetadas.sort()

    alterado = False
    if destino.is_dir():
        for velho in destino.iterdir():
            if velho.name not in injetadas:
                sandbox.remover(velho)
                alterado = True
    for a in arquivos:
        if a.name in injetadas:
            copia = destino / a.name
            if not copia.is_file() or copia.read_bytes() != a.read_bytes():
                sandbox.escrever_bytes(copia, a.read_bytes())
                alterado = True
    if destino.is_dir() and not injetadas and not any(destino.iterdir()):
        sandbox.remover(destino)

    bruto = projeto.read_bytes()
    texto = bruto.decode("utf-8")
    novo = _projeto_com(texto, injetadas)
    if novo != texto:
        ET.fromstring(novo.encode("utf-8"))  # só grava XML que o MSBuild consegue ler
        sandbox.escrever_bytes(projeto, novo.encode("utf-8"))
        alterado = True
    return {"injetadas": injetadas, "puladas": puladas, "alterado": alterado}


def separar_blocos(d: dict) -> tuple[dict, list[dict]]:
    """Divide o resultado da suíte (dict de `ResultadoSuite.como_dict()`) em (a suíte do próprio
    alvo, os blocos das sondas). Na parte da suíte, os totais e o `passou` são refeitos sem as
    sondas: uma sonda que falha não pode fazer a suíte existente parecer quebrada."""
    suite = copy.deepcopy(d)
    blocos = d.get("blocos", [])
    suite["blocos"] = [b for b in blocos if not e_sonda(b["nome"])]
    das_sondas = [dict(b) for b in blocos if e_sonda(b["nome"])]
    if das_sondas:
        suite["total_ok"] = sum(b["ok"] for b in suite["blocos"])
        suite["total_falhas"] = sum(b["falhas"] for b in suite["blocos"])
        suite["blocos_declarados"] = len(suite["blocos"])
        detalhes, dentro = [], True
        for linha in d.get("detalhes", []):
            m = re.match(r"^-\s*(.+?)\s+/\s+", linha)
            if m:
                dentro = not e_sonda(m.group(1))
            if dentro:
                detalhes.append(linha)
        suite["detalhes"] = detalhes
        suite["passou"] = not suite.get("incoerencias") and suite["total_falhas"] == 0
    return suite, das_sondas


def resultados_por_item(mapa: dict[str, list[str]], blocos: list[dict]) -> dict[str, tuple[str, str | None]]:
    """Resultado de cada item do catálogo coberto por sonda (mapa de `itens.yaml`): falhou se algum
    bloco de alguma sonda dele falhou; nao_testavel se alguma sonda dele não rodou; senão passou."""
    por_item: dict[str, list[str]] = {}
    for nome, itens in mapa.items():
        for item in itens:
            por_item.setdefault(item, []).append(nome)
    out: dict[str, tuple[str, str | None]] = {}
    for item, nomes_sonda in por_item.items():
        casados = [b for b in blocos if b["nome"] in nomes_sonda]
        ausentes = [n for n in nomes_sonda if n not in {b["nome"] for b in casados}]
        if any(b["falhas"] for b in casados):
            out[item] = ("falhou", None)
        elif ausentes:
            out[item] = ("nao_testavel", f"sonda não rodou neste alvo: {', '.join(ausentes)}")
        else:
            out[item] = ("passou", None)
    return out
