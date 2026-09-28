# M1 — Esqueleto confiável: plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Um `/sprint` que, sem alterar a MAW, descobre os alvos (main + branches abertas), compila cada um, roda a suíte e o benchmark, recebe achados de revisão de código dos subagentes, verifica, consolida com IDs estáveis e gera um PDF real com a cobertura parcial declarada.

**Architecture:** Pacote Python `maw_agent` com um módulo por responsabilidade e uma CLI (`python -m maw_agent ...`) que grava tudo em `privado/relatorios/sprint-NN/`. A camada do Claude Code (`CLAUDE.md`, `/sprint`, subagentes) orquestra a CLI e escreve os achados de julgamento como JSON validado por esquema. Nada escreve nas pastas da MAW do usuário: a única porta de escrita (`sandbox.py`) recusa esses caminhos.

**Tech Stack:** Python 3.13 (`.venv` via uv), pytest, PyYAML, Jinja2, jsonschema, Playwright (canal `msedge`), pypdf, psutil, pyaudiowpatch, mido/python-rtmidi; MSBuild do VS 2022 Build Tools; git.

**Spec:** `docs/superpowers/specs/2026-09-27-agente-maw-design.md` (público) + `privado/docs/2026-09-27-apendice-maw.md` (privado).

## Global Constraints

- Nunca escrever, commitar ou dar push em `C:\Users\User\MAW*`; git nessas pastas só leitura com `--no-optional-locks`.
- Toda escrita em disco do agente passa por `maw_agent.sandbox` (exceto o próprio `pytest` em `tmp_path`).
- Repositório público sem conhecimento interno da MAW, sem resultados, sem segredos; testes do público usam nomes sintéticos, nunca nomes reais de classes/arquivos da MAW.
- Chave do Gemini nunca é gravada; toda saída de processo passa por `redacao.redigir` antes de ir para disco.
- Textos do relatório e mensagens da CLI em português do Brasil.
- Matriz de cobertura nunca tem célula vazia: ausência de resultado vira `nao_testavel` com motivo.
- Um achado só é `corrigido` quando o critério de aceite passa; nunca por ausência.
- Espelho da MAW com `pushurl = nao-faca-push`.
- Saída da suíte vem com CRLF e linhas vazias duplicadas, e pode ter linhas de aviso antes do cabeçalho: o leitor normaliza.
- Executável da MAW é `/SUBSYSTEM:Windows`: sempre `subprocess.run` (que espera) e nunca confiar no exit code sem conferir o texto.

## Review Focus

- Caminho protegido escrito com outra caixa, barra invertida/normal misturada, `..`, ou via junction/symlink → `sandbox` precisa recusar igual (teste em Task 2).
- Suíte que imprime `RESULTADO: TUDO PASSOU` mas sai com código 1, ou que morre no meio sem totais → não pode virar "passou" (teste em Task 6).
- Clone local com a mesma branch do GitHub, à frente, atrás ou divergente → um alvo só quando iguais; o mais novo quando um é ancestral; os dois quando divergem (teste em Task 4).
- Sprint interrompida no meio de uma fase e retomada → passos concluídos não rodam de novo, passo em andamento roda de novo (teste em Task 9).
- Achado corrigido numa sprint e que volta na seguinte → `regressao`, mesmo ID (teste em Task 7).

---

## Estrutura de arquivos (M1)

```
pyproject.toml                         dependências e config do pytest
CLAUDE.md                              constituição do agente
.claude/commands/sprint.md             orquestração do /sprint
.claude/agents/*.md                    10 subagentes
ferramentas/hooks/pre-commit           hook do repositório público
maw_agent/
  __init__.py
  __main__.py                          CLI (argparse, subcomandos)
  config.py                            caminhos e constantes
  sandbox.py                           escrita protegida, backup/restauração de pasta
  redacao.py                           depurador de segredos
  hook.py                              verificação de pre-commit
  alvos.py                             espelho, alvos, worktrees, prova de intocada
  build.py                             MSBuild
  suite.py                             --run-tests / --benchmark
  saude.py                             captura de OutputDebugString (jassert)
  achados.py                           modelo, esquema, impressão digital, histórico
  esquemas/achado.schema.json          esquema JSON dos achados
  catalogo.py                          catálogo, resultados, matriz
  estado.py                            estado da sprint e retomada
  preflight.py                         pré-voo e retrato do ambiente
  fases.py                             implementação das fases da sprint
  relatorio/
    __init__.py
    contexto.py                        monta o dicionário do relatório
    pdf.py                             HTML → PDF (Edge) + anexo
    modelos/relatorio.html.j2
    modelos/estilo.css
tests/
  conftest.py
  test_cli.py  test_sandbox.py  test_redacao.py  test_hook.py  test_alvos.py
  test_build.py  test_suite.py  test_saude.py  test_achados.py  test_catalogo.py
  test_estado.py  test_preflight.py  test_relatorio.py  test_fases.py
privado/ (repositório privado)
  catalogo/funcionalidades.yaml  catalogo/principios.yaml  catalogo/termos-proibidos.txt
  ferramentas/hooks/pre-commit
```

---

### Task 1: Esqueleto do pacote, configuração e CLI

**Files:**
- Create: `pyproject.toml`, `maw_agent/__init__.py`, `maw_agent/config.py`, `maw_agent/cli.py`, `maw_agent/__main__.py`, `tests/conftest.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `config.RAIZ`, `config.WORK`, `config.PRIVADO`, `config.ESPELHO`, `config.ALVOS_DIR`, `config.BACKUPS`, `config.RELATORIOS`, `config.HISTORICO`, `config.APPDATA_MAW`, `config.URL_MAW`, `config.pastas_protegidas() -> list[Path]`; `cli.main(argv: list[str] | None = None) -> int` e `cli.registrar(nome: str, ajuda: str, configurar: Callable[[ArgumentParser], None]) -> decorator` para os módulos adicionarem subcomandos. O registro fica em `cli.py`, e não em `__main__.py`, porque `python -m maw_agent` carrega `__main__` duas vezes (como `__main__` e como `maw_agent.__main__`) e os subcomandos cairiam na cópia errada.

- [ ] **Step 1: Escrever o teste que falha**

```python
# tests/test_cli.py
import subprocess, sys
from pathlib import Path
from maw_agent import config
from maw_agent.cli import main

def test_ajuda_lista_subcomandos(capsys):
    assert main(["--help-json"]) == 0
    saida = capsys.readouterr().out
    assert '"subcomandos"' in saida

def test_modulo_executavel():
    p = subprocess.run([sys.executable, "-m", "maw_agent", "--help-json"],
                       capture_output=True, text=True)
    assert p.returncode == 0

def test_caminhos_relativos_a_raiz():
    assert config.WORK == config.RAIZ / "work"
    assert config.PRIVADO == config.RAIZ / "privado"
    assert config.RELATORIOS == config.PRIVADO / "relatorios"

def test_pastas_protegidas_aceita_override(monkeypatch, tmp_path):
    a, b = tmp_path / "MAW", tmp_path / "MAW_x"
    a.mkdir(); b.mkdir()
    monkeypatch.setenv("MAW_AGENTE_PROTEGIDAS", f"{a};{b}")
    assert config.pastas_protegidas() == [a.resolve(), b.resolve()]
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_cli.py -v`
Expected: FAIL (`ModuleNotFoundError: maw_agent`)

- [ ] **Step 3: Implementar**

```toml
# pyproject.toml
[project]
name = "maw-agent"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["pyyaml", "jinja2", "jsonschema", "playwright", "pypdf", "psutil",
                "numpy", "soundfile", "pyloudnorm", "pywinauto", "pillow", "requests",
                "mido", "python-rtmidi", "pyaudiowpatch"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["lento: usa MSBuild, rede ou a MAW real (rodar com -m lento)"]
addopts = "-m 'not lento'"
```

```python
# maw_agent/__init__.py
"""Agente de testes da MAW."""
```

```python
# maw_agent/config.py
"""Caminhos e constantes do agente. Nada aqui escreve em disco."""
from __future__ import annotations
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
WORK = RAIZ / "work"
PRIVADO = RAIZ / "privado"
ESPELHO = WORK / "espelho"
ALVOS_DIR = WORK / "alvos"
BACKUPS = WORK / "appdata-backup"
FERRAMENTAS = WORK / "ferramentas"
RELATORIOS = PRIVADO / "relatorios"
HISTORICO = PRIVADO / "historico" / "achados.json"
CATALOGO = PRIVADO / "catalogo" / "funcionalidades.yaml"
PRINCIPIOS = PRIVADO / "catalogo" / "principios.yaml"
TERMOS_PROIBIDOS = PRIVADO / "catalogo" / "termos-proibidos.txt"
APPDATA_MAW = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "MAW"
URL_MAW = "https://github.com/WaynerMoraes12/MAW.git"
USUARIO = Path(os.environ.get("USERPROFILE", str(Path.home())))


def pastas_protegidas() -> list[Path]:
    """Pastas da MAW do usuário: só leitura para o agente.

    `MAW_AGENTE_PROTEGIDAS` (separado por `;`) substitui a descoberta — usado nos testes.
    """
    override = os.environ.get("MAW_AGENTE_PROTEGIDAS")
    if override:
        return [Path(p).resolve() for p in override.split(";") if p]
    return sorted(p.resolve() for p in USUARIO.glob("MAW*") if p.is_dir())
```

```python
# maw_agent/cli.py
"""CLI: python -m maw_agent <subcomando> [...]. Toda saída de dados é JSON em stdout."""
from __future__ import annotations
import argparse
import json
import sys
from typing import Callable

_SUBCOMANDOS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None], Callable[[argparse.Namespace], int]]] = {}


def registrar(nome: str, ajuda: str, configurar: Callable[[argparse.ArgumentParser], None]):
    """Decorator: registra `executar(args) -> int` como subcomando `nome`."""
    def deco(executar: Callable[[argparse.Namespace], int]):
        _SUBCOMANDOS[nome] = (ajuda, configurar, executar)
        return executar
    return deco


def _carregar_modulos() -> None:
    # importar registra os subcomandos; a lista cresce nas tasks seguintes
    import importlib
    for mod in ("hook", "fases"):
        try:
            importlib.import_module(f"maw_agent.{mod}")
        except ModuleNotFoundError as e:
            if e.name != f"maw_agent.{mod}":
                raise


def main(argv: list[str] | None = None) -> int:
    _carregar_modulos()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--help-json"]:
        print(json.dumps({"subcomandos": {k: v[0] for k, v in sorted(_SUBCOMANDOS.items())}},
                         ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(prog="maw_agent")
    sub = parser.add_subparsers(dest="comando", required=True)
    for nome, (ajuda, configurar, _) in sorted(_SUBCOMANDOS.items()):
        configurar(sub.add_parser(nome, help=ajuda))
    args = parser.parse_args(argv)
    return _SUBCOMANDOS[args.comando][2](args)
```

```python
# maw_agent/__main__.py
import sys

from maw_agent.cli import main

sys.exit(main())
```

```python
# tests/conftest.py
import pytest

@pytest.fixture(autouse=True)
def _sem_pastas_reais(monkeypatch, tmp_path):
    """Nenhum teste enxerga as pastas reais da MAW: protegidas = uma pasta falsa."""
    falsa = tmp_path / "_maw_do_usuario"
    falsa.mkdir()
    monkeypatch.setenv("MAW_AGENTE_PROTEGIDAS", str(falsa))
    return falsa
```

Nota: o teste `test_pastas_protegidas_aceita_override` redefine a variável depois da fixture — é o comportamento esperado.

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_cli.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml maw_agent tests
git commit -m "feat(agente): esqueleto do pacote, configuracao e CLI"
```

---

### Task 2: Escrita protegida e backup/restauração (`sandbox.py`)

**Files:**
- Create: `maw_agent/sandbox.py`, `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `config.pastas_protegidas()`
- Produces: `EscritaProibida(Exception)`, `RestauracaoFalhou(Exception)`, `caminho_protegido(p: Path) -> bool`, `garantir_escrita(p: Path) -> Path`, `escrever_texto(p: Path, texto: str) -> Path`, `escrever_bytes(p: Path, dados: bytes) -> Path`, `escrever_json(p: Path, obj) -> Path` (atômico: temp + `os.replace`), `criar_pasta(p: Path) -> Path`, `copiar(origem: Path, destino: Path) -> Path` (arquivo ou pasta), `remover(p: Path) -> None`, `manifesto(pasta: Path) -> dict[str, str]` (relpath POSIX → sha256), `backup_pasta(origem: Path, destino_raiz: Path) -> Path` (devolve a pasta do backup, com `manifesto.json` = `{"origem": str, "existia": bool, "arquivos": {...}}` e os arquivos em `dados/`), `restaurar_pasta(backup: Path) -> None` (deixa a origem idêntica ao backup ou levanta `RestauracaoFalhou`).

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_sandbox.py
import os
import pytest
from pathlib import Path
from maw_agent import sandbox

def test_recusa_escrita_na_pasta_protegida(_sem_pastas_reais):
    alvo = _sem_pastas_reais / "Source" / "x.cpp"
    with pytest.raises(sandbox.EscritaProibida):
        sandbox.escrever_texto(alvo, "oi")
    assert not alvo.exists()

@pytest.mark.parametrize("variar", [
    lambda p: Path(str(p).upper()),
    lambda p: Path(str(p).replace("\\", "/")),
    lambda p: p / "sub" / ".." / "arquivo.txt",
])
def test_recusa_variacoes_do_caminho(_sem_pastas_reais, variar):
    with pytest.raises(sandbox.EscritaProibida):
        sandbox.escrever_texto(variar(_sem_pastas_reais) / "a.txt", "x")

def test_recusa_via_junction(_sem_pastas_reais, tmp_path):
    ponte = tmp_path / "ponte"
    os.system(f'mklink /J "{ponte}" "{_sem_pastas_reais}" >NUL')
    if not ponte.exists():
        pytest.skip("mklink /J indisponível")
    with pytest.raises(sandbox.EscritaProibida):
        sandbox.escrever_texto(ponte / "a.txt", "x")

def test_permite_fora(tmp_path):
    p = sandbox.escrever_texto(tmp_path / "ok" / "a.txt", "olá")
    assert p.read_text(encoding="utf-8") == "olá"

def test_remover_e_copiar_protegidos(_sem_pastas_reais, tmp_path):
    (tmp_path / "f.txt").write_text("x")
    with pytest.raises(sandbox.EscritaProibida):
        sandbox.copiar(tmp_path / "f.txt", _sem_pastas_reais / "f.txt")
    with pytest.raises(sandbox.EscritaProibida):
        sandbox.remover(_sem_pastas_reais)

def test_escrever_json_atomico(tmp_path):
    p = sandbox.escrever_json(tmp_path / "a.json", {"a": "ç"})
    assert p.read_text(encoding="utf-8") == '{\n  "a": "ç"\n}\n'
    assert not list(tmp_path.glob("*.tmp"))

def test_backup_e_restauracao_ida_e_volta(tmp_path):
    origem = tmp_path / "AppData" / "MAW"
    (origem / "autosave").mkdir(parents=True)
    (origem / "MAW.settings").write_text("<a/>")
    (origem / "autosave" / "x.maw").write_bytes(b"\x00\x01")
    bk = sandbox.backup_pasta(origem, tmp_path / "bk")
    # o teste bagunça a origem
    (origem / "MAW.settings").write_text("<mudou/>")
    (origem / "novo.txt").write_text("lixo")
    (origem / "autosave" / "x.maw").unlink()
    sandbox.restaurar_pasta(bk)
    assert sandbox.manifesto(origem) == sandbox.manifesto(bk / "dados")
    assert not (origem / "novo.txt").exists()

def test_restauracao_de_origem_que_nao_existia(tmp_path):
    origem = tmp_path / "nao_existe"
    bk = sandbox.backup_pasta(origem, tmp_path / "bk")
    origem.mkdir(); (origem / "a").write_text("x")
    sandbox.restaurar_pasta(bk)
    assert not origem.exists()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_sandbox.py -v`
Expected: FAIL (`ImportError: cannot import name 'sandbox'`)

- [ ] **Step 3: Implementar**

```python
# maw_agent/sandbox.py
"""A única porta de escrita em disco do agente.

Recusa qualquer caminho dentro das pastas da MAW do usuário, resolvendo
caixa, barras, `..`, symlinks e junctions antes de comparar.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from . import config


class EscritaProibida(Exception):
    """Tentativa de escrever numa pasta da MAW do usuário."""


class RestauracaoFalhou(Exception):
    """A pasta restaurada não ficou idêntica ao backup."""


def _real(p: Path) -> Path:
    # resolve(strict=False) segue symlinks/junctions das partes que existem
    return Path(os.path.realpath(Path(p).absolute()))


def caminho_protegido(p: Path) -> bool:
    alvo = str(_real(p)).casefold().rstrip("\\/") + "\\"
    for prot in config.pastas_protegidas():
        base = str(_real(prot)).casefold().rstrip("\\/") + "\\"
        if alvo.startswith(base):
            return True
    return False


def garantir_escrita(p: Path) -> Path:
    if caminho_protegido(p):
        raise EscritaProibida(f"escrita recusada: {p} fica numa pasta da MAW do usuário")
    return Path(p)


def criar_pasta(p: Path) -> Path:
    garantir_escrita(p).mkdir(parents=True, exist_ok=True)
    return Path(p)


def escrever_bytes(p: Path, dados: bytes) -> Path:
    p = garantir_escrita(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(dados)
    os.replace(tmp, p)
    return p


def escrever_texto(p: Path, texto: str) -> Path:
    return escrever_bytes(p, texto.encode("utf-8"))


def escrever_json(p: Path, obj) -> Path:
    return escrever_texto(p, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def copiar(origem: Path, destino: Path) -> Path:
    garantir_escrita(destino)
    if Path(origem).is_dir():
        shutil.copytree(origem, destino, dirs_exist_ok=True)
    else:
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origem, destino)
    return Path(destino)


def remover(p: Path) -> None:
    garantir_escrita(p)
    p = Path(p)
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()


def manifesto(pasta: Path) -> dict[str, str]:
    pasta = Path(pasta)
    if not pasta.exists():
        return {}
    out: dict[str, str] = {}
    for arq in sorted(pasta.rglob("*")):
        if arq.is_file():
            out[arq.relative_to(pasta).as_posix()] = hashlib.sha256(arq.read_bytes()).hexdigest()
    return out


def backup_pasta(origem: Path, destino_raiz: Path) -> Path:
    origem = Path(origem)
    destino = Path(destino_raiz) / time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while destino.exists():
        n += 1
        destino = Path(destino_raiz) / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    criar_pasta(destino)
    existia = origem.exists()
    if existia:
        copiar(origem, destino / "dados")
    escrever_json(destino / "manifesto.json",
                  {"origem": str(origem), "existia": existia, "arquivos": manifesto(origem)})
    return destino


def restaurar_pasta(backup: Path) -> None:
    info = json.loads((Path(backup) / "manifesto.json").read_text(encoding="utf-8"))
    origem = Path(info["origem"])
    if origem.exists():
        remover(origem)
    if info["existia"]:
        copiar(Path(backup) / "dados", origem)
    obtido = manifesto(origem)
    if obtido != info["arquivos"]:
        raise RestauracaoFalhou(f"{origem} não ficou idêntica ao backup {backup}")
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_sandbox.py -v`
Expected: todos passam (o de junction pode ser `skipped`)

- [ ] **Step 5: Commit**

```bash
git add maw_agent/sandbox.py tests/test_sandbox.py
git commit -m "feat(sandbox): escrita protegida e backup/restauracao de pasta"
```

---

### Task 3: Depurador de segredos, hook de pre-commit e instalação

**Files:**
- Create: `maw_agent/redacao.py`, `maw_agent/hook.py`, `ferramentas/hooks/pre-commit`, `tests/test_redacao.py`, `tests/test_hook.py`
- Create (privado): `privado/ferramentas/hooks/pre-commit`

**Interfaces:**
- Consumes: `config.TERMOS_PROIBIDOS`, `sandbox`
- Produces: `redacao.redigir(texto: str) -> str`, `redacao.segredos_em(texto: str) -> list[str]`; `hook.carregar_termos(caminho: Path) -> list[str] | None` (linha comum = substring, `re:` = regex, `#` = comentário), `hook.verificar(arquivos: dict[str, str], repo: str, termos: list[str] | None) -> list[str]` (repo `"publico"`/`"privado"`; conteúdo `""` para binários), subcomandos CLI `hook-precommit --repo {publico,privado}` e `instalar-hooks`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_redacao.py
from maw_agent import redacao

CHAVE = "AIza" + "B" * 35

def test_redige_chave_google():
    assert redacao.redigir(f"key={CHAVE} fim") == "key=[REDACTED] fim"

def test_redige_tokens_github():
    t = "gho_" + "a" * 36
    assert "[REDACTED]" in redacao.redigir(f"Token: {t}")

def test_nao_mexe_em_texto_comum():
    assert redacao.redigir("RESULTADO: TUDO PASSOU.") == "RESULTADO: TUDO PASSOU."

def test_segredos_em_lista_os_tipos():
    assert redacao.segredos_em(f"x {CHAVE}") == ["chave-google"]
```

```python
# tests/test_hook.py
from maw_agent import hook

TERMOS = ["ClasseSecreta", "re:Segredo[A-Z]\\w+"]

def test_publico_bloqueia_caminhos_de_resultado():
    v = hook.verificar({"privado/a.md": "", "work/x": "", "relatorios/s/a.pdf": ""}, "publico", TERMOS)
    assert len(v) == 3

def test_publico_bloqueia_termo_interno():
    v = hook.verificar({"maw_agent/x.py": "usa ClasseSecreta aqui"}, "publico", TERMOS)
    assert v and "ClasseSecreta" in v[0]

def test_publico_bloqueia_termo_regex():
    assert hook.verificar({"a.md": "SegredoMotor"}, "publico", TERMOS)

def test_publico_sem_lista_recusa():
    v = hook.verificar({"a.md": "nada"}, "publico", None)
    assert v and "termos" in v[0]

def test_bloqueia_chave_nos_dois():
    chave = "AIza" + "C" * 35
    for repo in ("publico", "privado"):
        assert hook.verificar({"a.txt": chave}, repo, TERMOS)

def test_privado_aceita_pdf_mas_nao_evidencia_bruta():
    assert hook.verificar({"relatorios/sprint-01/MAW-Sprint-01.pdf": ""}, "privado", None) == []
    assert hook.verificar({"relatorios/sprint-01/evidencias/a.png": ""}, "privado", None)

def test_privado_bloqueia_settings_e_chave_em_arquivo():
    assert hook.verificar({"x/MAW.settings": ""}, "privado", None)
    assert hook.verificar({"gemini_api_key.txt": ""}, "privado", None)

def test_carregar_termos(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("# comentario\nClasseSecreta\n\nre:X\\d\n", encoding="utf-8")
    assert hook.carregar_termos(p) == ["ClasseSecreta", "re:X\\d"]
    assert hook.carregar_termos(tmp_path / "nao.txt") is None
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_redacao.py tests/test_hook.py -v`
Expected: FAIL (módulos inexistentes)

- [ ] **Step 3: Implementar**

```python
# maw_agent/redacao.py
"""Troca segredos por [REDACTED] antes de qualquer texto ir para disco."""
from __future__ import annotations
import re

PADROES: dict[str, re.Pattern[str]] = {
    "chave-google": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "token-github": re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{22,})"),
    "chave-openai-anthropic": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}"),
}


def segredos_em(texto: str) -> list[str]:
    return [nome for nome, rx in PADROES.items() if rx.search(texto)]


def redigir(texto: str) -> str:
    for rx in PADROES.values():
        texto = rx.sub("[REDACTED]", texto)
    return texto
```

```python
# maw_agent/hook.py
"""Pre-commit dos dois repositórios: nada de resultado, segredo ou conhecimento interno no público."""
from __future__ import annotations
import argparse
import re
import subprocess
import sys
from pathlib import Path

from . import config, redacao
from .cli import registrar

PREFIXOS_PUBLICO = ("privado/", "work/", ".venv/")
PARTES_PROIBIDAS_PUBLICO = ("relatorios/", "evidencias/")
PARTES_PROIBIDAS_PRIVADO = ("/evidencias/", "appdata-backup/", "work/")
NOMES_PROIBIDOS = ("gemini_api_key.txt", "MAW.settings", "estado.json")
EXT_PROIBIDAS_PUBLICO = (".pdf", ".wav", ".flac", ".ogg", ".mp3", ".maw", ".settings")


def carregar_termos(caminho: Path) -> list[str] | None:
    if not Path(caminho).exists():
        return None
    termos = []
    for linha in Path(caminho).read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#"):
            termos.append(linha)
    return termos


def _achar_termo(texto: str, termos: list[str]) -> str | None:
    for t in termos:
        if t.startswith("re:"):
            m = re.search(t[3:], texto)
            if m:
                return m.group(0)
        elif t in texto:
            return t
    return None


def verificar(arquivos: dict[str, str], repo: str, termos: list[str] | None) -> list[str]:
    violacoes: list[str] = []
    if repo == "publico" and termos is None:
        return ["lista de termos proibidos ausente (privado/catalogo/termos-proibidos.txt): commit recusado"]
    for caminho, conteudo in sorted(arquivos.items()):
        c = caminho.replace("\\", "/")
        nome = c.rsplit("/", 1)[-1]
        if nome in NOMES_PROIBIDOS:
            violacoes.append(f"{c}: arquivo que nunca vai para repositório")
            continue
        if repo == "publico":
            if c.startswith(PREFIXOS_PUBLICO) or any(p in c for p in PARTES_PROIBIDAS_PUBLICO) \
                    or c.lower().endswith(EXT_PROIBIDAS_PUBLICO):
                violacoes.append(f"{c}: caminho proibido no repositório público")
                continue
        else:
            if any(p in "/" + c for p in PARTES_PROIBIDAS_PRIVADO):
                violacoes.append(f"{c}: evidência bruta ou backup não vai nem para o privado")
                continue
        tipos = redacao.segredos_em(conteudo)
        if tipos:
            violacoes.append(f"{c}: contém segredo ({', '.join(tipos)})")
        if repo == "publico":
            termo = _achar_termo(c + "\n" + conteudo, termos or [])
            if termo:
                violacoes.append(f"{c}: contém termo interno da MAW ({termo})")
    return violacoes


def _arquivos_em_stage(raiz: Path) -> dict[str, str]:
    nomes = subprocess.run(["git", "-C", str(raiz), "diff", "--cached", "--name-only", "-z",
                            "--diff-filter=ACMR"], capture_output=True, check=True).stdout
    out: dict[str, str] = {}
    for nome in filter(None, nomes.decode("utf-8").split("\0")):
        dados = subprocess.run(["git", "-C", str(raiz), "show", f":{nome}"],
                               capture_output=True, check=True).stdout
        try:
            out[nome] = dados.decode("utf-8")
        except UnicodeDecodeError:
            out[nome] = ""
    return out


def _cfg_hook(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", choices=["publico", "privado"], required=True)


@registrar("hook-precommit", "verifica o que está em stage antes do commit", _cfg_hook)
def executar_hook(args: argparse.Namespace) -> int:
    raiz = config.RAIZ if args.repo == "publico" else config.PRIVADO
    termos = carregar_termos(config.TERMOS_PROIBIDOS) if args.repo == "publico" else None
    violacoes = verificar(_arquivos_em_stage(raiz), args.repo, termos)
    for v in violacoes:
        print(f"[pre-commit] {v}", file=sys.stderr)
    return 1 if violacoes else 0


@registrar("instalar-hooks", "liga os hooks de pre-commit nos dois repositórios", lambda p: None)
def instalar_hooks(args: argparse.Namespace) -> int:
    subprocess.run(["git", "-C", str(config.RAIZ), "config", "core.hooksPath", "ferramentas/hooks"], check=True)
    if (config.PRIVADO / ".git").exists():
        subprocess.run(["git", "-C", str(config.PRIVADO), "config", "core.hooksPath", "ferramentas/hooks"], check=True)
    print('{"ok": true}')
    return 0
```

```sh
# ferramentas/hooks/pre-commit
#!/bin/sh
# Hook do repositório PÚBLICO: bloqueia resultado, segredo e termo interno da MAW.
raiz="$(git rev-parse --show-toplevel)"
exec "$raiz/.venv/Scripts/python.exe" -m maw_agent hook-precommit --repo publico
```

```sh
# privado/ferramentas/hooks/pre-commit
#!/bin/sh
# Hook do repositório PRIVADO: bloqueia segredo, evidência bruta e backup.
raiz="$(git rev-parse --show-toplevel)/.."
cd "$raiz" && exec "$raiz/.venv/Scripts/python.exe" -m maw_agent hook-precommit --repo privado
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_redacao.py tests/test_hook.py -v`
Expected: todos passam

- [ ] **Step 5: Instalar e conferir o hook de verdade**

Run: `.venv/Scripts/python -m maw_agent instalar-hooks` e, no público, `echo "AIza$(printf 'D%.0s' $(seq 35))" > t.txt && git add t.txt && git commit -m t` 
Expected: commit recusado com `contém segredo`; depois `git reset t.txt && rm t.txt`.

- [ ] **Step 6: Commit (nos dois repositórios)**

```bash
git add maw_agent/redacao.py maw_agent/hook.py ferramentas tests/test_redacao.py tests/test_hook.py
git commit -m "feat(seguranca): depurador de segredos e hook de pre-commit"
git -C privado add ferramentas && git -C privado commit -m "feat(seguranca): hook de pre-commit do privado"
```

---

### Task 4: Espelho, alvos, worktrees e prova de intocada (`alvos.py`)

**Files:**
- Create: `maw_agent/alvos.py`, `tests/test_alvos.py`

**Interfaces:**
- Consumes: `config.ESPELHO`, `config.ALVOS_DIR`, `config.URL_MAW`, `config.pastas_protegidas()`, `sandbox`
- Produces:
  - `@dataclass(frozen=True) Alvo(nome: str, branch: str, ref: str, commit: str, origem: str, assinatura: str, compartilha_com: str | None = None)` + `Alvo.como_dict() -> dict`
  - `CAMINHOS_DE_CODIGO: tuple[str, ...]`
  - `git(args: list[str], cwd: Path, leitura: bool = False) -> str` (com `leitura=True` injeta `--no-optional-locks`)
  - `slug(branch: str) -> str`
  - `garantir_espelho(espelho: Path = ESPELHO, url: str = URL_MAW, buscar: bool = True) -> Path`
  - `importar_clones_locais(espelho: Path, pastas: list[Path]) -> list[dict]` (cada dict: `{"pasta", "branch", "commit", "ref", "sujo": bool}`)
  - `descobrir_alvos(espelho: Path, locais: list[dict]) -> tuple[list[Alvo], list[str]]` (alvos, avisos)
  - `criar_worktree(espelho: Path, alvo: Alvo, raiz: Path = ALVOS_DIR) -> Path`
  - `prova_intocada(pastas: list[Path]) -> dict[str, dict]`
  - `comparar_provas(antes: dict, depois: dict) -> list[str]`

- [ ] **Step 1: Escrever os testes que falham**

Os testes montam um "GitHub" falso (repositório bare), um espelho e clones "do usuário" em `tmp_path`.

```python
# tests/test_alvos.py
import subprocess
from pathlib import Path
import pytest
from maw_agent import alvos

def sh(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

def commit(repo: Path, arquivo: str, texto: str, msg: str) -> str:
    (repo / arquivo).parent.mkdir(parents=True, exist_ok=True)
    (repo / arquivo).write_text(texto)
    sh("add", "-A", cwd=repo); sh("commit", "-qm", msg, cwd=repo)
    return sh("rev-parse", "HEAD", cwd=repo)

@pytest.fixture
def mundo(tmp_path):
    origem = tmp_path / "origem"; origem.mkdir()
    sh("init", "-q", "-b", "main", cwd=origem)
    sh("config", "user.email", "t@t", cwd=origem); sh("config", "user.name", "t", cwd=origem)
    commit(origem, "Source/a.cpp", "1", "c1")
    sh("checkout", "-qb", "feature/x", cwd=origem); commit(origem, "Source/a.cpp", "2", "x")
    sh("checkout", "-q", "main", cwd=origem)
    sh("checkout", "-qb", "docs/y", cwd=origem); commit(origem, "README.md", "doc", "y")
    sh("checkout", "-q", "main", cwd=origem)
    sh("checkout", "-qb", "feature/mesclada", cwd=origem); commit(origem, "Source/b.cpp", "b", "m")
    sh("checkout", "-q", "main", cwd=origem); sh("merge", "-q", "--no-ff", "-m", "merge", "feature/mesclada", cwd=origem)
    bare = tmp_path / "github.git"
    sh("clone", "-q", "--bare", str(origem), str(bare), cwd=tmp_path)
    return {"tmp": tmp_path, "origem": origem, "bare": bare}

def test_espelho_sem_push(mundo):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    assert sh("remote", "get-url", "--push", "origin", cwd=esp) == "nao-faca-push"

def test_descobre_main_e_branches_nao_mescladas(mundo):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    lista, avisos = alvos.descobrir_alvos(esp, [])
    nomes = sorted(a.nome for a in lista)
    assert nomes == ["docs-y", "feature-x", "main"]

def test_mesma_arvore_de_codigo_compartilha(mundo):
    # docs/z sai do main atual e só muda o README: mesma árvore de código
    sh("checkout", "-qb", "docs/z", cwd=mundo["origem"]); commit(mundo["origem"], "README.md", "z", "z")
    sh("push", "-q", str(mundo["bare"]), "docs/z", cwd=mundo["origem"])
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    lista, _ = alvos.descobrir_alvos(esp, [])
    por_nome = {a.nome: a for a in lista}
    assert por_nome["main"].compartilha_com is None
    assert por_nome["docs-z"].compartilha_com == "main"
    # docs/y saiu do main ANTES do merge: árvore diferente, não compartilha
    assert por_nome["docs-y"].compartilha_com is None

def _clone_do_usuario(mundo, nome, branch):
    pasta = mundo["tmp"] / nome
    sh("clone", "-q", "-b", branch, str(mundo["bare"]), str(pasta), cwd=mundo["tmp"])
    sh("config", "user.email", "t@t", cwd=pasta); sh("config", "user.name", "t", cwd=pasta)
    return pasta

def test_clone_local_a_frente_vence(mundo):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    pasta = _clone_do_usuario(mundo, "MAW_a1", "feature/x")
    novo = commit(pasta, "Source/a.cpp", "3", "local")
    locais = alvos.importar_clones_locais(esp, [pasta])
    lista, _ = alvos.descobrir_alvos(esp, locais)
    x = [a for a in lista if a.branch == "feature/x"]
    assert len(x) == 1 and x[0].commit == novo and x[0].origem == "local:MAW_a1"

def test_clone_local_igual_nao_duplica(mundo):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    pasta = _clone_do_usuario(mundo, "MAW_a1", "feature/x")
    lista, _ = alvos.descobrir_alvos(esp, alvos.importar_clones_locais(esp, [pasta]))
    assert len([a for a in lista if a.branch == "feature/x"]) == 1

def test_clone_local_divergente_entra_os_dois(mundo):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    pasta = _clone_do_usuario(mundo, "MAW_a1", "feature/x")
    commit(pasta, "Source/a.cpp", "local", "local")
    # GitHub anda por outro caminho
    sh("checkout", "-q", "feature/x", cwd=mundo["origem"]); commit(mundo["origem"], "Source/c.cpp", "gh", "gh")
    sh("push", "-q", str(mundo["bare"]), "feature/x", cwd=mundo["origem"])
    alvos.garantir_espelho(esp, str(mundo["bare"]))
    lista, _ = alvos.descobrir_alvos(esp, alvos.importar_clones_locais(esp, [pasta]))
    assert len([a for a in lista if a.branch == "feature/x"]) == 2

def test_clone_so_local_entra(mundo):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    pasta = _clone_do_usuario(mundo, "MAW_a6", "main")
    sh("checkout", "-qb", "feature/so-local", cwd=pasta); c = commit(pasta, "Source/z.cpp", "z", "z")
    lista, _ = alvos.descobrir_alvos(esp, alvos.importar_clones_locais(esp, [pasta]))
    assert any(a.branch == "feature/so-local" and a.commit == c for a in lista)

def test_clone_sujo_gera_aviso(mundo):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    pasta = _clone_do_usuario(mundo, "MAW_a1", "feature/x")
    (pasta / "Source" / "a.cpp").write_text("sujo")
    _, avisos = alvos.descobrir_alvos(esp, alvos.importar_clones_locais(esp, [pasta]))
    assert any("MAW_a1" in a and "não commitadas" in a for a in avisos)

def test_prova_intocada_detecta_mudanca_e_nao_escreve(mundo):
    pasta = _clone_do_usuario(mundo, "MAW", "main")
    idx = (pasta / ".git" / "index").stat().st_mtime_ns
    antes = alvos.prova_intocada([pasta])
    assert (pasta / ".git" / "index").stat().st_mtime_ns == idx
    assert alvos.comparar_provas(antes, alvos.prova_intocada([pasta])) == []
    (pasta / "Source" / "a.cpp").write_text("mexi")
    difs = alvos.comparar_provas(antes, alvos.prova_intocada([pasta]))
    assert difs and "MAW" in difs[0]

def test_worktree_no_commit_do_alvo(mundo, tmp_path):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    lista, _ = alvos.descobrir_alvos(esp, [])
    x = next(a for a in lista if a.nome == "feature-x")
    wt = alvos.criar_worktree(esp, x, tmp_path / "alvos")
    assert (wt / "Source" / "a.cpp").read_text() == "2"
    assert alvos.criar_worktree(esp, x, tmp_path / "alvos") == wt  # reaproveita
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_alvos.py -v`
Expected: FAIL (`cannot import name 'alvos'`)

- [ ] **Step 3: Implementar**

```python
# maw_agent/alvos.py
"""Espelho próprio da MAW, descoberta de alvos, worktrees e prova de intocada."""
from __future__ import annotations
import hashlib
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config, sandbox

CAMINHOS_DE_CODIGO = ("Source", "Builds", "JuceLibraryCode", "MAW_APP.jucer",
                      "server_mapp.py", "requirements.txt", "pretrained_models")


@dataclass(frozen=True)
class Alvo:
    nome: str
    branch: str
    ref: str
    commit: str
    origem: str
    assinatura: str
    compartilha_com: str | None = None

    def como_dict(self) -> dict:
        return asdict(self)


def git(args: list[str], cwd: Path, leitura: bool = False) -> str:
    base = ["git", "--no-optional-locks"] if leitura else ["git"]
    p = subprocess.run(base + args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falhou em {cwd}: {p.stderr.strip()}")
    return p.stdout.strip()


def slug(branch: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", branch.lower()).strip("-")


def garantir_espelho(espelho: Path = config.ESPELHO, url: str = config.URL_MAW,
                     buscar: bool = True) -> Path:
    espelho = Path(espelho)
    if not (espelho / ".git").exists():
        sandbox.criar_pasta(espelho.parent)
        subprocess.run(["git", "clone", "-q", "--no-checkout", url, str(espelho)], check=True)
    git(["remote", "set-url", "--push", "origin", "nao-faca-push"], espelho)
    if buscar:
        git(["fetch", "-q", "--prune", "origin"], espelho)
    return espelho


def importar_clones_locais(espelho: Path, pastas: list[Path]) -> list[dict]:
    """Lê o HEAD de cada clone do usuário para dentro do espelho (só lê a origem)."""
    out = []
    for pasta in pastas:
        if not (Path(pasta) / ".git").exists():
            continue
        branch = git(["branch", "--show-current"], pasta, leitura=True)
        if not branch:
            continue
        commit = git(["rev-parse", "HEAD"], pasta, leitura=True)
        sujo = bool(git(["status", "--porcelain", "--untracked-files=no"], pasta, leitura=True))
        ref = f"refs/locais/{Path(pasta).name}/{branch}"
        git(["fetch", "-q", "--no-tags", str(pasta), f"+HEAD:{ref}"], espelho)
        out.append({"pasta": str(pasta), "branch": branch, "commit": commit, "ref": ref, "sujo": sujo})
    return out


def _e_ancestral(espelho: Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=espelho).returncode == 0


def _assinatura(espelho: Path, commit: str) -> str:
    h = hashlib.sha256()
    for c in CAMINHOS_DE_CODIGO:
        try:
            h.update(f"{c}={git(['rev-parse', f'{commit}:{c}'], espelho)}\n".encode())
        except RuntimeError:
            h.update(f"{c}=ausente\n".encode())
    return h.hexdigest()[:16]


def descobrir_alvos(espelho: Path, locais: list[dict]) -> tuple[list[Alvo], list[str]]:
    avisos: list[str] = []
    main = git(["rev-parse", "origin/main"], espelho)
    candidatos: dict[str, list[dict]] = {"main": [{"branch": "main", "ref": "origin/main",
                                                   "commit": main, "origem": "github"}]}
    for ref in git(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"], espelho).splitlines():
        branch = ref.removeprefix("origin/")
        if branch in ("HEAD", "main", "origin") or not ref.startswith("origin/"):
            continue
        commit = git(["rev-parse", ref], espelho)
        if _e_ancestral(espelho, commit, main):
            continue
        candidatos.setdefault(branch, []).append({"branch": branch, "ref": ref, "commit": commit,
                                                  "origem": "github"})
    for loc in locais:
        nome_pasta = Path(loc["pasta"]).name
        if loc["sujo"]:
            avisos.append(f"{nome_pasta}: há alterações não commitadas em {loc['branch']}; não foram testadas")
        if _e_ancestral(espelho, loc["commit"], main):
            continue
        existentes = candidatos.setdefault(loc["branch"], [])
        novo = {"branch": loc["branch"], "ref": loc["ref"], "commit": loc["commit"],
                "origem": f"local:{nome_pasta}"}
        substituido = False
        for i, ex in enumerate(existentes):
            if ex["commit"] == novo["commit"] or _e_ancestral(espelho, novo["commit"], ex["commit"]):
                substituido = True  # igual ou atrás: o existente já cobre
                break
            if _e_ancestral(espelho, ex["commit"], novo["commit"]):
                existentes[i] = novo  # local à frente: vence
                substituido = True
                break
        if not substituido:
            existentes.append(novo)  # divergente ou só local
    lista: list[Alvo] = []
    vistos: dict[str, str] = {}
    for branch in ["main"] + sorted(b for b in candidatos if b != "main"):
        grupo = candidatos[branch]
        for c in grupo:
            nome = slug(branch) if len(grupo) == 1 or c["origem"] == "github" else f"{slug(branch)}-local"
            assin = _assinatura(espelho, c["commit"])
            lista.append(Alvo(nome, branch, c["ref"], c["commit"], c["origem"], assin, vistos.get(assin)))
            vistos.setdefault(assin, nome)
    return lista, avisos


def criar_worktree(espelho: Path, alvo: Alvo, raiz: Path = config.ALVOS_DIR) -> Path:
    destino = Path(raiz) / alvo.nome
    if (destino / ".git").exists():
        if git(["rev-parse", "HEAD"], destino) == alvo.commit:
            return destino
        git(["checkout", "-q", "--detach", "--force", alvo.commit], destino)
        return destino
    sandbox.criar_pasta(Path(raiz))
    git(["worktree", "prune"], espelho)
    git(["worktree", "add", "-q", "--force", "--detach", str(destino), alvo.commit], espelho)
    return destino


def prova_intocada(pastas: list[Path]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for pasta in pastas:
        pasta = Path(pasta)
        if not (pasta / ".git").exists():
            continue
        status = git(["status", "--porcelain=v1", "--untracked-files=all"], pasta, leitura=True)
        diff = subprocess.run(["git", "--no-optional-locks", "diff", "HEAD", "--binary"],
                              cwd=pasta, capture_output=True).stdout
        out[pasta.name] = {
            "head": git(["rev-parse", "HEAD"], pasta, leitura=True),
            "branch": git(["branch", "--show-current"], pasta, leitura=True),
            "hash_status": hashlib.sha256(status.encode()).hexdigest(),
            "hash_diff": hashlib.sha256(diff).hexdigest(),
        }
    return out


def comparar_provas(antes: dict, depois: dict) -> list[str]:
    difs = []
    for nome in sorted(set(antes) | set(depois)):
        a, d = antes.get(nome), depois.get(nome)
        if a != d:
            campos = sorted(k for k in set((a or {})) | set((d or {})) if (a or {}).get(k) != (d or {}).get(k))
            difs.append(f"{nome}: mudou ({', '.join(campos)})")
    return difs
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_alvos.py -v`
Expected: todos passam

- [ ] **Step 5: Commit**

```bash
git add maw_agent/alvos.py tests/test_alvos.py
git commit -m "feat(alvos): espelho sem push, descoberta de alvos, worktrees e prova de intocada"
```

---

### Task 5: Build com MSBuild (`build.py`)

**Files:**
- Create: `maw_agent/build.py`, `tests/test_build.py`

**Interfaces:**
- Consumes: `sandbox`, `redacao`
- Produces: `@dataclass ResultadoBuild(alvo: str, config: str, ok: bool, segundos: float, avisos: list[str], erros: list[str], exe: str | None, log: str)` + `como_dict()`; `localizar_msbuild() -> Path`; `extrair_diagnosticos(texto: str) -> tuple[list[str], list[str]]` (avisos, erros; únicos, ordem de aparição); `caminho_exe(worktree: Path, config: str) -> Path`; `compilar(alvo: str, worktree: Path, config: str, pasta_logs: Path, msbuild: Path | None = None, timeout: int = 5400) -> ResultadoBuild`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_build.py
import pytest
from pathlib import Path
from maw_agent import build

LOG = """
a.cpp(10,5): warning C4100: 'x': parâmetro formal não referenciado [C:\\p\\App.vcxproj]
a.cpp(10,5): warning C4100: 'x': parâmetro formal não referenciado [C:\\p\\App.vcxproj]
b.cpp(3,1): aviso C4244: conversão de 'double' para 'float' [C:\\p\\App.vcxproj]
c.cpp(7,2): error C2065: 'y': identificador não declarado [C:\\p\\App.vcxproj]
LINK : fatal error LNK1104: não é possível abrir o arquivo 'App.exe'
"""

def test_extrai_avisos_e_erros_sem_repetir():
    avisos, erros = build.extrair_diagnosticos(LOG)
    assert len(avisos) == 2 and "C4100" in avisos[0] and "C4244" in avisos[1]
    assert len(erros) == 2 and "C2065" in erros[0] and "LNK1104" in erros[1]

def test_caminho_do_exe(tmp_path):
    assert build.caminho_exe(tmp_path, "Release") == \
        tmp_path / "Builds" / "VisualStudio2022" / "x64" / "Release" / "App" / "MAW_APP.exe"

def test_localiza_msbuild():
    p = build.localizar_msbuild()
    assert p.name.lower() == "msbuild.exe" and p.exists()

def test_projeto_inexistente_nao_levanta(tmp_path):
    r = build.compilar("x", tmp_path, "Release", tmp_path / "logs")
    assert r.ok is False and r.erros and "não existe" in r.erros[0]

@pytest.mark.lento
def test_compila_main_de_verdade():
    from maw_agent import config
    wt = config.ALVOS_DIR / "main"
    r = build.compilar("main", wt, "Release", config.WORK / "logs-teste")
    assert r.ok and Path(r.exe).exists()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_build.py -v`
Expected: FAIL (`cannot import name 'build'`)

- [ ] **Step 3: Implementar**

```python
# maw_agent/build.py
"""Compila um alvo com o MSBuild do VS 2022 Build Tools."""
from __future__ import annotations
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import redacao, sandbox

_RX_AVISO = re.compile(r"\b(?:warning|aviso)\s+([A-Z]{1,4}\d{3,5})\s*:", re.IGNORECASE)
_RX_ERRO = re.compile(r"\b(?:error|erro)\s+([A-Z]{1,4}\d{3,5})\s*:", re.IGNORECASE)


@dataclass
class ResultadoBuild:
    alvo: str
    config: str
    ok: bool
    segundos: float
    avisos: list[str]
    erros: list[str]
    exe: str | None
    log: str

    def como_dict(self) -> dict:
        return asdict(self)


def localizar_msbuild() -> Path:
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if vswhere.exists():
        p = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                            "Microsoft.Component.MSBuild", "-find", r"MSBuild\**\Bin\MSBuild.exe"],
                           capture_output=True, text=True)
        for linha in p.stdout.splitlines():
            if linha.strip() and Path(linha.strip()).exists():
                return Path(linha.strip())
    raise FileNotFoundError("MSBuild não encontrado (VS 2022 Build Tools)")


def extrair_diagnosticos(texto: str) -> tuple[list[str], list[str]]:
    def coletar(rx: re.Pattern[str]) -> list[str]:
        vistos: dict[str, None] = {}
        for linha in texto.splitlines():
            if rx.search(linha):
                vistos.setdefault(linha.strip().split(" [")[0], None)
        return list(vistos)
    return coletar(_RX_AVISO), coletar(_RX_ERRO)


def caminho_exe(worktree: Path, config: str) -> Path:
    return Path(worktree) / "Builds" / "VisualStudio2022" / "x64" / config / "App" / "MAW_APP.exe"


def compilar(alvo: str, worktree: Path, config: str, pasta_logs: Path,
             msbuild: Path | None = None, timeout: int = 5400) -> ResultadoBuild:
    projeto = Path(worktree) / "Builds" / "VisualStudio2022" / "MAW_APP_App.vcxproj"
    log = Path(pasta_logs) / f"build-{alvo}-{config}.log"
    if not projeto.exists():
        return ResultadoBuild(alvo, config, False, 0.0, [], [f"projeto não existe: {projeto}"], None, str(log))
    msbuild = msbuild or localizar_msbuild()
    inicio = time.monotonic()
    try:
        p = subprocess.run([str(msbuild), str(projeto), f"/p:Configuration={config}", "/p:Platform=x64",
                            "/m", "/nologo", "/v:minimal"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        saida, codigo = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        saida, codigo = f"{e.stdout or ''}\nTIMEOUT depois de {timeout}s", -1
    segundos = round(time.monotonic() - inicio, 1)
    sandbox.escrever_texto(log, redacao.redigir(saida))
    avisos, erros = extrair_diagnosticos(saida)
    exe = caminho_exe(worktree, config)
    ok = codigo == 0 and exe.exists()
    if codigo != 0 and not erros:
        erros = [f"MSBuild saiu com código {codigo} sem erro reconhecível; ver {log.name}"]
    return ResultadoBuild(alvo, config, ok, segundos, avisos, erros, str(exe) if ok else None, str(log))
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_build.py -v`
Expected: 4 passed, 1 deselected

- [ ] **Step 5: Commit**

```bash
git add maw_agent/build.py tests/test_build.py
git commit -m "feat(build): compilacao com MSBuild e extracao de avisos e erros"
```

---

### Task 6: Suíte e benchmark (`suite.py`)

**Files:**
- Create: `maw_agent/suite.py`, `tests/test_suite.py`

**Interfaces:**
- Consumes: `redacao`
- Produces: `@dataclass BlocoSuite(nome: str, sub: str, ok: int, falhas: int)`; `@dataclass ResultadoSuite(exit_code: int | None, segundos: float, blocos: list[BlocoSuite], total_ok: int | None, total_falhas: int | None, blocos_declarados: int | None, tempo_ms: int | None, declarado: str, detalhes: list[str], preambulo: list[str], incoerencias: list[str], bruto: str)` + `como_dict()` + propriedade `passou -> bool` (verdadeiro só com exit 0, declarado `PASSOU`, zero falhas e nenhuma incoerência); `interpretar_suite(texto: str, exit_code: int | None, segundos: float) -> ResultadoSuite`; `@dataclass LinhaBenchmark(trilhas: int, medio_ms: float, max_ms: float, carga_media: float, carga_max: float)`; `interpretar_benchmark(texto: str) -> tuple[list[LinhaBenchmark], list[str]]`; `executar(exe: Path, argumento: str, cwd: Path, timeout: int) -> tuple[int | None, str, float]`; `rodar_suite(exe, cwd, timeout=1800) -> ResultadoSuite`; `rodar_benchmark(exe, cwd, timeout=1800) -> dict` (`{"exit_code", "segundos", "linhas", "cabecalho", "bruto"}`).

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_suite.py
from maw_agent import suite

OK = ("  (aviso qualquer)\r\n\r\nNOME - suite de testes automatizados (juce::UnitTest)\r\n\r\n"
      "=====\r\n\r\n"
      "[ok]     Bloco A  ->  faz x   (5 ok, 0 falha(s))\r\n\r\n"
      "[ok]     Bloco B  ->  faz y   (3 ok, 0 falha(s))\r\n\r\n"
      "Blocos de teste ........ 2\r\n\r\nVerificacoes que deram ok 8\r\n\r\n"
      "Verificacoes que falharam 0\r\n\r\nTempo total ............ 120 ms\r\n\r\n"
      "RESULTADO: TUDO PASSOU.\r\n")

FALHA = ("NOME - suite de testes automatizados (juce::UnitTest)\n"
         "[ok]     Bloco A  ->  faz x   (5 ok, 0 falha(s))\n"
         "[FALHOU] Bloco B  ->  faz y   (2 ok, 1 falha(s))\n"
         "Blocos de teste ........ 2\nVerificacoes que deram ok 7\nVerificacoes que falharam 1\n"
         "Tempo total ............ 99 ms\n"
         "RESULTADO: FALHOU - 1 verificacao(oes) em 1 bloco(s).\n\nDetalhe das falhas:\n"
         "  - Bloco B / faz y\n      !!! Test 1 failed: esperado 1, obtido 2\n")

def test_interpreta_sucesso_com_crlf_e_preambulo():
    r = suite.interpretar_suite(OK, 0, 1.0)
    assert [b.nome for b in r.blocos] == ["Bloco A", "Bloco B"]
    assert r.total_ok == 8 and r.total_falhas == 0 and r.blocos_declarados == 2
    assert r.preambulo == ["(aviso qualquer)"]
    assert r.passou and r.incoerencias == []

def test_interpreta_falha_com_detalhe():
    r = suite.interpretar_suite(FALHA, 1, 1.0)
    assert not r.passou and r.total_falhas == 1
    assert r.blocos[1].falhas == 1 and r.blocos[1].sub == "faz y"
    assert any("esperado 1, obtido 2" in d for d in r.detalhes)

def test_passou_com_exit_1_e_incoerente():
    r = suite.interpretar_suite(OK, 1, 1.0)
    assert not r.passou and any("código de saída" in i for i in r.incoerencias)

def test_saida_truncada_nao_e_sucesso():
    r = suite.interpretar_suite("NOME - suite de testes automatizados\n[ok]     Bloco A  ->  x   (1 ok, 0 falha(s))\n", 0, 1.0)
    assert not r.passou and r.declarado == "AUSENTE"
    assert any("totais" in i for i in r.incoerencias)

def test_soma_dos_blocos_confere_com_totais():
    texto = OK.replace("Verificacoes que deram ok 8", "Verificacoes que deram ok 9")
    r = suite.interpretar_suite(texto, 0, 1.0)
    assert not r.passou and any("soma" in i for i in r.incoerencias)

BENCH = """ trilhas | tempo medio (ms) | tempo maximo (ms) | carga media (%) | carga maxima (%)
---------+------------------+-------------------+-----------------+------------------
       1 |            0.004 |             0.032 |            0.04 |             0.30
      32 |            0.031 |             0.080 |            0.29 |             0.75
"""

def test_interpreta_benchmark():
    linhas, cab = suite.interpretar_benchmark("Plugin do master descarregado\n" + BENCH)
    assert [l.trilhas for l in linhas] == [1, 32]
    assert linhas[1].carga_max == 0.75
    assert "Plugin do master descarregado" in cab[0]
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_suite.py -v`
Expected: FAIL (`cannot import name 'suite'`)

- [ ] **Step 3: Implementar**

```python
# maw_agent/suite.py
"""Roda `--run-tests` / `--benchmark` do executável e interpreta o texto.

O exe é /SUBSYSTEM:Windows: subprocess.run espera o processo, e o resultado só
é "passou" quando o código de saída, a linha RESULTADO e os totais concordam.
"""
from __future__ import annotations
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import redacao

_RX_BLOCO = re.compile(r"^\[(ok|FALHOU)\]\s+(.+?)\s{2,}->\s{2,}(.+?)\s+\((\d+) ok, (\d+) falha\(s\)\)\s*$")
_RX_NUM = {
    "blocos": re.compile(r"^Blocos de teste \.+ (\d+)"),
    "ok": re.compile(r"^Verificacoes que deram ok (\d+)"),
    "falhas": re.compile(r"^Verificacoes que falharam (\d+)"),
    "tempo": re.compile(r"^Tempo total \.+ (\d+) ms"),
}


@dataclass
class BlocoSuite:
    nome: str
    sub: str
    ok: int
    falhas: int


@dataclass
class ResultadoSuite:
    exit_code: int | None
    segundos: float
    blocos: list[BlocoSuite] = field(default_factory=list)
    total_ok: int | None = None
    total_falhas: int | None = None
    blocos_declarados: int | None = None
    tempo_ms: int | None = None
    declarado: str = "AUSENTE"
    detalhes: list[str] = field(default_factory=list)
    preambulo: list[str] = field(default_factory=list)
    incoerencias: list[str] = field(default_factory=list)
    bruto: str = ""

    @property
    def passou(self) -> bool:
        return (self.exit_code == 0 and self.declarado == "PASSOU" and self.total_falhas == 0
                and not self.incoerencias)

    def como_dict(self) -> dict:
        d = asdict(self)
        d["passou"] = self.passou
        return d


def _linhas(texto: str) -> list[str]:
    return [l.rstrip() for l in texto.replace("\r\n", "\n").replace("\r", "\n").split("\n") if l.strip()]


def interpretar_suite(texto: str, exit_code: int | None, segundos: float) -> ResultadoSuite:
    r = ResultadoSuite(exit_code=exit_code, segundos=segundos, bruto=texto)
    no_detalhe = False
    viu_cabecalho = False
    for l in _linhas(texto):
        s = l.strip()
        if "suite de testes automatizados" in s:
            viu_cabecalho = True
            continue
        if not viu_cabecalho:
            r.preambulo.append(s)
            continue
        if set(s) <= {"="}:
            continue
        m = _RX_BLOCO.match(s)
        if m:
            r.blocos.append(BlocoSuite(m.group(2), m.group(3), int(m.group(4)), int(m.group(5))))
            continue
        casou = False
        for chave, rx in _RX_NUM.items():
            mm = rx.match(s)
            if mm:
                casou = True
                v = int(mm.group(1))
                if chave == "blocos":
                    r.blocos_declarados = v
                elif chave == "ok":
                    r.total_ok = v
                elif chave == "falhas":
                    r.total_falhas = v
                else:
                    r.tempo_ms = v
        if casou:
            continue
        if s.startswith("RESULTADO: TUDO PASSOU"):
            r.declarado = "PASSOU"
        elif s.startswith("RESULTADO: FALHOU"):
            r.declarado = "FALHOU"
        elif s.startswith("Detalhe das falhas"):
            no_detalhe = True
        elif no_detalhe:
            r.detalhes.append(s)
    if not viu_cabecalho:
        r.incoerencias.append("cabeçalho da suíte ausente: o executável não rodou os testes")
    if r.total_ok is None or r.total_falhas is None or r.blocos_declarados is None:
        r.incoerencias.append("totais ausentes: a execução pode ter morrido antes do fim")
    else:
        if sum(b.ok for b in r.blocos) != r.total_ok or sum(b.falhas for b in r.blocos) != r.total_falhas:
            r.incoerencias.append("a soma dos blocos não bate com os totais declarados")
        if len(r.blocos) != r.blocos_declarados:
            r.incoerencias.append("o número de blocos listados não bate com o declarado")
    if r.declarado == "PASSOU" and exit_code not in (0, None):
        r.incoerencias.append(f"relatório diz TUDO PASSOU mas o código de saída foi {exit_code}")
    if r.declarado == "FALHOU" and exit_code == 0:
        r.incoerencias.append("relatório diz FALHOU mas o código de saída foi 0")
    return r


@dataclass
class LinhaBenchmark:
    trilhas: int
    medio_ms: float
    max_ms: float
    carga_media: float
    carga_max: float


def interpretar_benchmark(texto: str) -> tuple[list[LinhaBenchmark], list[str]]:
    linhas: list[LinhaBenchmark] = []
    cab: list[str] = []
    for l in _linhas(texto):
        partes = [p.strip() for p in l.split("|")]
        if len(partes) == 5 and partes[0].isdigit():
            linhas.append(LinhaBenchmark(int(partes[0]), *(float(p.replace(",", ".")) for p in partes[1:])))
        elif not linhas and "trilhas" not in l and not set(l.strip()) <= set("-+"):
            cab.append(l.strip())
    return linhas, cab


def executar(exe: Path, argumento: str, cwd: Path, timeout: int) -> tuple[int | None, str, float]:
    inicio = time.monotonic()
    try:
        p = subprocess.run([str(exe), argumento], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        codigo, saida = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        saida_parcial = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        codigo, saida = None, f"{saida_parcial}\nTIMEOUT depois de {timeout}s"
    return codigo, redacao.redigir(saida), round(time.monotonic() - inicio, 1)


def rodar_suite(exe: Path, cwd: Path, timeout: int = 1800) -> ResultadoSuite:
    codigo, saida, seg = executar(exe, "--run-tests", cwd, timeout)
    return interpretar_suite(saida, codigo, seg)


def rodar_benchmark(exe: Path, cwd: Path, timeout: int = 1800) -> dict:
    codigo, saida, seg = executar(exe, "--benchmark", cwd, timeout)
    linhas, cab = interpretar_benchmark(saida)
    return {"exit_code": codigo, "segundos": seg, "linhas": [asdict(l) for l in linhas],
            "cabecalho": cab, "bruto": saida}
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_suite.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add maw_agent/suite.py tests/test_suite.py
git commit -m "feat(suite): executa e interpreta --run-tests e --benchmark conferindo coerencia"
```

---

### Task 7: Captura de `jassert` via OutputDebugString (`saude.py`)

**Files:**
- Create: `maw_agent/saude.py`, `tests/test_saude.py`

**Interfaces:**
- Produces: `class CapturaDepuracao` (context manager; `.mensagens: list[tuple[int, str]]` com (pid, texto); `.erro: str | None` quando não conseguiu escutar); `filtrar(mensagens, pid: int | None = None) -> list[str]`; `assercoes(mensagens: list[str]) -> list[str]`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_saude.py
import ctypes, os, time
from maw_agent import saude

def test_captura_outputdebugstring_do_proprio_processo():
    with saude.CapturaDepuracao() as cap:
        time.sleep(0.2)
        ctypes.windll.kernel32.OutputDebugStringW("JUCE Assertion failure in x.cpp:10")
        time.sleep(0.5)
    assert cap.erro is None
    textos = saude.filtrar(cap.mensagens, os.getpid())
    assert saude.assercoes(textos) == ["JUCE Assertion failure in x.cpp:10"]

def test_assercoes_ignora_resto():
    assert saude.assercoes(["oi", "JUCE Assertion failure in a.h:1"]) == ["JUCE Assertion failure in a.h:1"]
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_saude.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar**

```python
# maw_agent/saude.py
"""Escuta OutputDebugString (protocolo DBWIN) para pegar jassert do build Debug.

Só um ouvinte DBWIN existe por sessão; se outro (DebugView) já estiver ativo,
`.erro` diz isso e nada é capturado — sem fingir que não houve asserção.
"""
from __future__ import annotations
import ctypes
import ctypes.wintypes as wt
import struct
import threading

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateEventW.restype = wt.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_k32.CreateFileMappingW.restype = wt.HANDLE
_k32.CreateFileMappingW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
_k32.MapViewOfFile.restype = ctypes.c_void_p
_k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
_k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.SetEvent.argtypes = [wt.HANDLE]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_INVALID = wt.HANDLE(-1)
_PAGE_READWRITE, _FILE_MAP_READ, _WAIT_OBJECT_0, _ERROR_ALREADY_EXISTS = 0x04, 0x0004, 0, 183


class CapturaDepuracao:
    def __init__(self) -> None:
        self.mensagens: list[tuple[int, str]] = []
        self.erro: str | None = None
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._pronto = self._dados = self._map = None
        self._view = None

    def __enter__(self) -> "CapturaDepuracao":
        self._pronto = _k32.CreateEventW(None, False, False, "DBWIN_BUFFER_READY")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            self.erro = "outro ouvinte de OutputDebugString já está ativo; jassert não capturado"
            return self
        self._dados = _k32.CreateEventW(None, False, False, "DBWIN_DATA_READY")
        self._map = _k32.CreateFileMappingW(_INVALID, None, _PAGE_READWRITE, 0, 4096, "DBWIN_BUFFER")
        self._view = _k32.MapViewOfFile(self._map, _FILE_MAP_READ, 0, 0, 4096)
        if not self._view:
            self.erro = "não foi possível mapear DBWIN_BUFFER"
            return self
        self._thread = threading.Thread(target=self._laco, daemon=True)
        self._thread.start()
        return self

    def _laco(self) -> None:
        _k32.SetEvent(self._pronto)
        while not self._parar.is_set():
            if _k32.WaitForSingleObject(self._dados, 100) == _WAIT_OBJECT_0:
                bruto = ctypes.string_at(self._view, 4096)
                pid = struct.unpack("<I", bruto[:4])[0]
                texto = bruto[4:].split(b"\0", 1)[0].decode("mbcs", errors="replace").rstrip()
                self.mensagens.append((pid, texto))
                _k32.SetEvent(self._pronto)

    def __exit__(self, *exc) -> None:
        self._parar.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._view:
            _k32.UnmapViewOfFile(self._view)
        for h in (self._map, self._dados, self._pronto):
            if h:
                _k32.CloseHandle(h)


def filtrar(mensagens: list[tuple[int, str]], pid: int | None = None) -> list[str]:
    return [t for p, t in mensagens if pid is None or p == pid]


def assercoes(mensagens: list[str]) -> list[str]:
    return [m.strip() for m in mensagens if "JUCE Assertion failure" in m]
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_saude.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add maw_agent/saude.py tests/test_saude.py
git commit -m "feat(saude): captura de OutputDebugString para pegar jassert no Debug"
```

---

### Task 8: Achados — esquema, impressão digital, deduplicação e histórico (`achados.py`)

**Files:**
- Create: `maw_agent/esquemas/achado.schema.json`, `maw_agent/achados.py`, `tests/test_achados.py`

**Interfaces:**
- Consumes: `sandbox`
- Produces:
  - constantes `TIPOS`, `SEVERIDADES`, `PRIORIDADES`, `CONFIANCAS`, `ESTADOS`, `ORDEM_SEVERIDADE`
  - `validar(obj: dict) -> list[str]` (vazio = válido)
  - `normalizar_assinatura(s: str) -> str`
  - `impressao_digital(obj: dict) -> str` (16 hex; de `item_catalogo`, `tipo`, `assinatura`)
  - `deduplicar(lista: list[dict]) -> list[dict]`
  - `marcar_introducao(lista: list[dict], alvo_principal: str = "main") -> None`
  - `consolidar(achados: list[dict], historico: dict, sprint: str, reverificacoes: dict[str, str]) -> tuple[list[dict], dict]` (`reverificacoes`: id → `"corrigido" | "persiste" | "nao_verificavel"`)
  - `carregar_historico(p: Path) -> dict` (`{"proximo": int, "itens": {impressao: registro}}`), `salvar_historico(p: Path, h: dict) -> None`

Formato de um achado de entrada: campos do esquema; `alvos` = lista de `{"alvo", "commit"}`; `evidencias` = lista de `{"arquivo", "legenda", "embutir"}`; `veredito` opcional = `{"resultado": "confirmado"|"provavel"|"derrubado", "justificativa"}`; `fonte` = quem gerou (`"suite"`, `"build"`, `"testador-motor"`, ...).

- [ ] **Step 1: Escrever o esquema** em `maw_agent/esquemas/achado.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Achado do Agente MAW",
  "type": "object",
  "required": ["titulo", "tipo", "alvos", "item_catalogo", "passos", "esperado", "obtido",
               "evidencias", "sugestao", "criterio_aceite", "confianca", "assinatura"],
  "properties": {
    "titulo": {"type": "string", "minLength": 8},
    "tipo": {"enum": ["erro", "bug", "violacao", "afirmacao_falsa", "lacuna", "melhoria"]},
    "severidade": {"enum": ["critica", "alta", "media", "baixa", null]},
    "prioridade": {"enum": ["alta", "media", "baixa", null]},
    "alvos": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["alvo", "commit"],
              "properties": {"alvo": {"type": "string"}, "commit": {"type": "string"}}}},
    "item_catalogo": {"type": "string", "minLength": 3},
    "principio": {"type": ["string", "null"]},
    "passos": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    "esperado": {"type": "string", "minLength": 3},
    "obtido": {"type": "string", "minLength": 3},
    "evidencias": {"type": "array", "items": {"type": "object", "required": ["arquivo", "legenda"],
                   "properties": {"arquivo": {"type": "string"}, "legenda": {"type": "string"},
                                  "embutir": {"type": "boolean"}}}},
    "causa_provavel": {"type": ["object", "null"],
                       "properties": {"arquivo_linha": {"type": "string"}, "texto": {"type": "string"}}},
    "sugestao": {"type": "string", "minLength": 3},
    "criterio_aceite": {"type": "string", "minLength": 8},
    "confianca": {"enum": ["confirmado", "provavel"]},
    "assinatura": {"type": "string", "minLength": 3},
    "fonte": {"type": "string"},
    "veredito": {"type": ["object", "null"]}
  }
}
```

- [ ] **Step 2: Escrever os testes que falham**

```python
# tests/test_achados.py
from maw_agent import achados

def base(**over):
    a = {"titulo": "Exportar sem ffmpeg trava", "tipo": "bug", "severidade": "alta", "prioridade": None,
         "alvos": [{"alvo": "main", "commit": "aaa"}], "item_catalogo": "exportacao/mp3",
         "principio": "P4", "passos": ["abrir", "exportar"], "esperado": "mensagem clara",
         "obtido": "travou sem aviso", "evidencias": [{"arquivo": "e/1.png", "legenda": "tela", "embutir": True}],
         "causa_provavel": {"arquivo_linha": "x.cpp:10", "texto": "hipótese"}, "sugestao": "avisar",
         "criterio_aceite": "cenário exportacao/mp3 passa", "confianca": "confirmado",
         "assinatura": "Export.cpp:123 encode()"}
    a.update(over)
    return a

def test_valido_e_invalido():
    assert achados.validar(base()) == []
    assert len(achados.validar(base(tipo="palpite", passos=[]))) >= 2

def test_bug_exige_severidade_melhoria_exige_prioridade():
    assert achados.validar(base(severidade=None))
    assert achados.validar(base(tipo="melhoria", severidade=None, prioridade=None))
    assert achados.validar(base(tipo="melhoria", severidade=None, prioridade="media")) == []

def test_impressao_ignora_numero_de_linha_e_espacos():
    a = achados.impressao_digital(base(assinatura="Export.cpp:123   encode()"))
    b = achados.impressao_digital(base(assinatura="export.cpp:130 encode()"))
    assert a == b and len(a) == 16

def test_deduplica_entre_alvos():
    x = base(); y = base(alvos=[{"alvo": "feature-x", "commit": "bbb"}], severidade="critica")
    [u] = achados.deduplicar([x, y])
    assert {a["alvo"] for a in u["alvos"]} == {"main", "feature-x"}
    assert u["severidade"] == "critica"

def test_introduzido_por_uma_branch():
    l = [base(alvos=[{"alvo": "feature-x", "commit": "b"}]), base(assinatura="outra")]
    achados.marcar_introducao(l)
    assert l[0]["introduzido_por"] == "feature-x" and l[1]["introduzido_por"] is None

def test_ciclo_de_estados_entre_sprints():
    h = {"proximo": 1, "itens": {}}
    [a1], h = achados.consolidar([base()], h, "01", {})
    assert a1["id"] == "MAW-0001" and a1["estado"] == "novo"
    [a2], h = achados.consolidar([base()], h, "02", {})
    assert a2["id"] == "MAW-0001" and a2["estado"] == "aberto"
    [a3], h = achados.consolidar([], h, "03", {"MAW-0001": "corrigido"})
    assert a3["estado"] == "corrigido"
    [a4], h = achados.consolidar([base()], h, "04", {})
    assert a4["estado"] == "regressao" and a4["id"] == "MAW-0001"
    assert a4["historico"] == ["01", "02", "03", "04"]

def test_corrigido_antigo_nao_reaparece_nas_sprints_seguintes():
    h = {"proximo": 1, "itens": {}}
    _, h = achados.consolidar([base()], h, "01", {})
    _, h = achados.consolidar([], h, "02", {"MAW-0001": "corrigido"})
    l, h = achados.consolidar([], h, "03", {})
    assert l == []

def test_sem_reverificacao_vira_nao_verificavel_mantendo_anterior():
    h = {"proximo": 1, "itens": {}}
    _, h = achados.consolidar([base()], h, "01", {})
    [x], h = achados.consolidar([], h, "02", {})
    assert x["estado"] == "nao_verificavel" and x["estado_anterior"] == "novo"

def test_ids_nunca_reaproveitados():
    h = {"proximo": 1, "itens": {}}
    l, h = achados.consolidar([base(), base(assinatura="b2")], h, "01", {})
    assert sorted(a["id"] for a in l) == ["MAW-0001", "MAW-0002"]
    l, h = achados.consolidar([base(assinatura="b3")], h, "02", {"MAW-0001": "corrigido", "MAW-0002": "corrigido"})
    assert [a["id"] for a in l if a["estado"] == "novo"] == ["MAW-0003"]

def test_derrubado_nao_entra():
    l, _ = achados.consolidar([base(veredito={"resultado": "derrubado", "justificativa": "x"})],
                              {"proximo": 1, "itens": {}}, "01", {})
    assert l == []

def test_historico_ida_e_volta(tmp_path):
    h = {"proximo": 3, "itens": {"abc": {"id": "MAW-0002"}}}
    achados.salvar_historico(tmp_path / "h.json", h)
    assert achados.carregar_historico(tmp_path / "h.json") == h
    assert achados.carregar_historico(tmp_path / "nao.json") == {"proximo": 1, "itens": {}}
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_achados.py -v`
Expected: FAIL

- [ ] **Step 4: Implementar**

```python
# maw_agent/achados.py
"""Achados: validação por esquema, impressão digital estável, deduplicação e histórico."""
from __future__ import annotations
import copy
import hashlib
import json
import re
from pathlib import Path

import jsonschema

from . import sandbox

TIPOS = ("erro", "bug", "violacao", "afirmacao_falsa", "lacuna", "melhoria")
SEVERIDADES = ("critica", "alta", "media", "baixa")
PRIORIDADES = ("alta", "media", "baixa")
CONFIANCAS = ("confirmado", "provavel")
ESTADOS = ("novo", "aberto", "corrigido", "regressao", "nao_verificavel")
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


def consolidar(achados: list[dict], historico: dict, sprint: str,
               reverificacoes: dict[str, str]) -> tuple[list[dict], dict]:
    h = copy.deepcopy(historico)
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
        else:
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
        if anterior == "corrigido":
            if reg.get("sprint_do_estado") != sprint:
                continue  # corrigido numa sprint anterior: não é notícia
        rv = reverificacoes.get(reg["id"])
        novo = {"corrigido": "corrigido", "persiste": "aberto"}.get(rv, "nao_verificavel")
        reg["historico"] = reg["historico"] + [sprint]
        reg.update({"estado": novo, "estado_anterior": anterior, "sprint_do_estado": sprint})
        saida.append({**reg["ultimo"], "id": reg["id"], "estado": novo, "historico": reg["historico"],
                      "estado_anterior": anterior})
    saida.sort(key=lambda a: (_rank(a), a["id"]))
    return saida, h
```

- [ ] **Step 5: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_achados.py -v`
Expected: todos passam

- [ ] **Step 6: Commit**

```bash
git add maw_agent/achados.py maw_agent/esquemas tests/test_achados.py
git commit -m "feat(achados): esquema, impressao digital, deduplicacao e historico entre sprints"
```

---

### Task 9: Catálogo, resultados e matriz de cobertura (`catalogo.py`)

**Files:**
- Create: `maw_agent/catalogo.py`, `tests/test_catalogo.py`

**Interfaces:**
- Consumes: `sandbox`
- Produces:
  - `VERIFICACOES = ("e2e", "sonda", "servico", "revisao", "suite", "benchmark")`, `RESULTADOS = ("passou", "falhou", "nao_testavel", "na")`
  - `carregar(caminho: Path) -> list[dict]`, `validar(itens: list[dict]) -> list[str]`
  - `registrar_resultado(pasta_sprint: Path, item: str, alvo: str, resultado: str, motivo: str | None = None, achados: list[str] | None = None, fonte: str = "") -> None` (append em `resultados.jsonl`)
  - `carregar_resultados(pasta_sprint: Path) -> dict[tuple[str, str], dict]` (último registro vence)
  - `montar_matriz(itens: list[dict], alvos: list[str], resultados: dict, ausentes: dict[tuple[str, str], bool] | None = None) -> dict[str, dict[str, dict]]`
  - `resumo(matriz) -> dict[str, int]`

Item do catálogo (YAML): `id`, `area`, `titulo`, `descricao`, `origem` (lista), `verificacao` (lista ⊂ `VERIFICACOES`), `cenarios` (lista, pode ser vazia), `requisitos` (lista), `marco` (`M1`..`M5`) e, opcional, `somente_em` (lista de nomes de alvo: a funcionalidade só existe neles; nos demais a célula é `na`).

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_catalogo.py
import yaml
from maw_agent import catalogo

ITENS = [
    {"id": "a/um", "area": "a", "titulo": "Um", "descricao": "d", "origem": ["r"], "verificacao": ["e2e"],
     "cenarios": [], "requisitos": [], "marco": "M2"},
    {"id": "a/dois", "area": "a", "titulo": "Dois", "descricao": "d", "origem": ["r"], "verificacao": ["suite"],
     "cenarios": [], "requisitos": [], "marco": "M1"},
]

def test_carregar_e_validar(tmp_path):
    p = tmp_path / "c.yaml"; p.write_text(yaml.safe_dump(ITENS, allow_unicode=True), encoding="utf-8")
    assert catalogo.validar(catalogo.carregar(p)) == []

def test_validar_pega_duplicado_e_verificacao_invalida():
    erros = catalogo.validar([dict(ITENS[0]), dict(ITENS[0], verificacao=["palpite"])])
    assert any("duplicado" in e for e in erros) and any("palpite" in e for e in erros)

def test_matriz_nunca_vazia(tmp_path):
    catalogo.registrar_resultado(tmp_path, "a/dois", "main", "passou", fonte="suite")
    m = catalogo.montar_matriz(ITENS, ["main", "feature-x"], catalogo.carregar_resultados(tmp_path),
                               {("a/um", "feature-x"): True})
    assert m["a/dois"]["main"]["resultado"] == "passou"
    assert m["a/dois"]["feature-x"]["resultado"] == "nao_testavel"
    assert "sem resultado" in m["a/dois"]["feature-x"]["motivo"]
    assert m["a/um"]["feature-x"]["resultado"] == "na"
    assert "M2" in m["a/um"]["main"]["motivo"]
    assert catalogo.resumo(m) == {"passou": 1, "falhou": 0, "nao_testavel": 2, "na": 1}

def test_ultimo_registro_vence(tmp_path):
    catalogo.registrar_resultado(tmp_path, "a/um", "main", "falhou", achados=["X"])
    catalogo.registrar_resultado(tmp_path, "a/um", "main", "passou")
    assert catalogo.carregar_resultados(tmp_path)[("a/um", "main")]["resultado"] == "passou"

def test_somente_em_vira_na_nos_outros(tmp_path):
    it = dict(ITENS[0], id="a/nova", somente_em=["feature-x"])
    m = catalogo.montar_matriz([it], ["main", "feature-x"], {})
    assert m["a/nova"]["main"]["resultado"] == "na"
    assert m["a/nova"]["feature-x"]["resultado"] == "nao_testavel"

def test_resultado_invalido_levanta(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        catalogo.registrar_resultado(tmp_path, "a/um", "main", "talvez")
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_catalogo.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar**

```python
# maw_agent/catalogo.py
"""Catálogo de funcionalidades, resultados por célula e a matriz de cobertura."""
from __future__ import annotations
import json
from pathlib import Path

import yaml

from . import sandbox

VERIFICACOES = ("e2e", "sonda", "servico", "revisao", "suite", "benchmark")
RESULTADOS = ("passou", "falhou", "nao_testavel", "na")
_OBRIGATORIOS = ("id", "area", "titulo", "descricao", "origem", "verificacao", "cenarios", "requisitos", "marco")


def carregar(caminho: Path) -> list[dict]:
    return yaml.safe_load(Path(caminho).read_text(encoding="utf-8")) or []


def validar(itens: list[dict]) -> list[str]:
    erros, vistos = [], set()
    for i, it in enumerate(itens):
        for campo in _OBRIGATORIOS:
            if campo not in it:
                erros.append(f"item {i} ({it.get('id')}): falta '{campo}'")
        if it.get("id") in vistos:
            erros.append(f"id duplicado: {it.get('id')}")
        vistos.add(it.get("id"))
        for v in it.get("verificacao", []):
            if v not in VERIFICACOES:
                erros.append(f"{it.get('id')}: verificação desconhecida '{v}'")
    return erros


def registrar_resultado(pasta_sprint: Path, item: str, alvo: str, resultado: str,
                        motivo: str | None = None, achados: list[str] | None = None, fonte: str = "") -> None:
    if resultado not in RESULTADOS:
        raise ValueError(f"resultado inválido: {resultado}")
    p = Path(pasta_sprint) / "resultados.jsonl"
    linha = json.dumps({"item": item, "alvo": alvo, "resultado": resultado, "motivo": motivo,
                        "achados": achados or [], "fonte": fonte}, ensure_ascii=False)
    anterior = p.read_text(encoding="utf-8") if p.exists() else ""
    sandbox.escrever_texto(p, anterior + linha + "\n")


def carregar_resultados(pasta_sprint: Path) -> dict[tuple[str, str], dict]:
    p = Path(pasta_sprint) / "resultados.jsonl"
    out: dict[tuple[str, str], dict] = {}
    if p.exists():
        for linha in p.read_text(encoding="utf-8").splitlines():
            if linha.strip():
                r = json.loads(linha)
                out[(r["item"], r["alvo"])] = r
    return out


def montar_matriz(itens: list[dict], alvos: list[str], resultados: dict,
                  ausentes: dict[tuple[str, str], bool] | None = None) -> dict[str, dict[str, dict]]:
    ausentes = ausentes or {}
    m: dict[str, dict[str, dict]] = {}
    for it in itens:
        linha = {}
        for alvo in alvos:
            fora = it.get("somente_em") is not None and alvo not in it["somente_em"]
            if fora or ausentes.get((it["id"], alvo)):
                linha[alvo] = {"resultado": "na", "motivo": "a funcionalidade não existe neste alvo", "achados": []}
            elif (it["id"], alvo) in resultados:
                r = resultados[(it["id"], alvo)]
                linha[alvo] = {"resultado": r["resultado"], "motivo": r.get("motivo"), "achados": r.get("achados", [])}
            else:
                motivo = "sem resultado registrado nesta sprint"
                if not it.get("cenarios"):
                    motivo += f" (cenário previsto para o marco {it.get('marco', '?')})"
                linha[alvo] = {"resultado": "nao_testavel", "motivo": motivo, "achados": []}
        m[it["id"]] = linha
    return m


def resumo(matriz: dict[str, dict[str, dict]]) -> dict[str, int]:
    cont = {r: 0 for r in RESULTADOS}
    for linha in matriz.values():
        for cel in linha.values():
            cont[cel["resultado"]] += 1
    return cont
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_catalogo.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add maw_agent/catalogo.py tests/test_catalogo.py
git commit -m "feat(catalogo): catalogo, resultados por celula e matriz sem celula vazia"
```

---

### Task 10: Estado da sprint e retomada (`estado.py`)

**Files:**
- Create: `maw_agent/estado.py`, `tests/test_estado.py`

**Interfaces:**
- Consumes: `sandbox`, `config.RELATORIOS`
- Produces: `FASES: tuple[str, ...]` (… `"consolidar", "encerrar", "relatorio"`), `FINAL = "relatorio"`; `class Estado(pasta: Path, numero: int, passos: dict | None = None, criado: str | None = None)` com atributos `numero`, `nome` (`"sprint-NN"`), `pasta`, `passos`, `criado` e métodos `feito(passo) -> bool`, `dados(passo) -> dict | None`, `iniciar(passo)`, `concluir(passo, detalhe=None)`, `falhar(passo, erro)`, `salvar()`; funções `nova_sprint(raiz=RELATORIOS) -> Estado`, `carregar(pasta) -> Estado`, `em_andamento(raiz=RELATORIOS) -> Estado | None` (a mais recente sem `FINAL` concluído), `anterior(estado: Estado) -> Path | None` (pasta da sprint anterior concluída, para comparação).

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_estado.py
from maw_agent import estado

def test_numeracao_sequencial(tmp_path):
    a = estado.nova_sprint(tmp_path); b = estado.nova_sprint(tmp_path)
    assert (a.nome, b.nome) == ("sprint-01", "sprint-02")

def test_passos_persistem_e_retomam(tmp_path):
    e = estado.nova_sprint(tmp_path)
    e.iniciar("compilar:main:Release"); e.concluir("compilar:main:Release", {"ok": True})
    e.iniciar("compilar:feature-x:Release")  # interrompido aqui
    r = estado.carregar(e.pasta)
    assert r.feito("compilar:main:Release") and r.dados("compilar:main:Release") == {"ok": True}
    assert not r.feito("compilar:feature-x:Release")

def test_em_andamento_ignora_concluidas(tmp_path):
    a = estado.nova_sprint(tmp_path); a.concluir("encerrar"); a.concluir("relatorio")
    b = estado.nova_sprint(tmp_path); b.concluir("encerrar")  # restaurou, mas o PDF não saiu
    assert estado.em_andamento(tmp_path).nome == b.nome
    b.concluir("relatorio")
    assert estado.em_andamento(tmp_path) is None

def test_anterior_e_a_ultima_concluida(tmp_path):
    a = estado.nova_sprint(tmp_path); a.concluir("relatorio")
    b = estado.nova_sprint(tmp_path)
    assert estado.anterior(b) == a.pasta
    assert estado.anterior(a) is None

def test_falha_registrada(tmp_path):
    e = estado.nova_sprint(tmp_path); e.iniciar("x"); e.falhar("x", "boom")
    assert estado.carregar(e.pasta).passos["x"]["status"] == "falhou"
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_estado.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar**

```python
# maw_agent/estado.py
"""Estado persistente de uma sprint: cada passo concluído fica gravado, e a retomada o pula."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from . import config, sandbox

FASES = ("preflight", "preparar", "compilar", "catalogar", "executar", "verificar",
         "consolidar", "encerrar", "relatorio")
FINAL = "relatorio"  # a sprint só está concluída quando o PDF existe


class Estado:
    def __init__(self, pasta: Path, numero: int, passos: dict | None = None, criado: str | None = None):
        self.pasta = Path(pasta)
        self.numero = numero
        self.nome = f"sprint-{numero:02d}"
        self.passos: dict[str, dict] = passos or {}
        self.criado = criado or time.strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _agora() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    def feito(self, passo: str) -> bool:
        return self.passos.get(passo, {}).get("status") == "concluido"

    def dados(self, passo: str) -> dict | None:
        return self.passos.get(passo, {}).get("detalhe")

    def iniciar(self, passo: str) -> None:
        self.passos[passo] = {"status": "em_andamento", "inicio": self._agora()}
        self.salvar()

    def concluir(self, passo: str, detalhe: dict | None = None) -> None:
        p = self.passos.setdefault(passo, {"inicio": self._agora()})
        p.update({"status": "concluido", "fim": self._agora(), "detalhe": detalhe})
        self.salvar()

    def falhar(self, passo: str, erro: str) -> None:
        p = self.passos.setdefault(passo, {"inicio": self._agora()})
        p.update({"status": "falhou", "fim": self._agora(), "erro": erro})
        self.salvar()

    def salvar(self) -> None:
        sandbox.escrever_json(self.pasta / "estado.json",
                              {"numero": self.numero, "criado": self.criado, "passos": self.passos})


def carregar(pasta: Path) -> Estado:
    d = json.loads((Path(pasta) / "estado.json").read_text(encoding="utf-8"))
    return Estado(pasta, d["numero"], d["passos"], d.get("criado"))


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
    for p in reversed(_existentes(raiz)):
        if (p / "estado.json").exists():
            e = carregar(p)
            if not e.feito(FINAL):
                return e
    return None


def anterior(e: Estado) -> Path | None:
    for p in reversed(_existentes(e.pasta.parent)):
        if int(p.name.split("-")[1]) < e.numero and (p / "estado.json").exists() and carregar(p).feito(FINAL):
            return p
    return None
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_estado.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add maw_agent/estado.py tests/test_estado.py
git commit -m "feat(estado): estado persistente da sprint e retomada"
```

---

### Task 11: Pré-voo e retrato do ambiente (`preflight.py`)

**Files:**
- Create: `maw_agent/preflight.py`, `tests/test_preflight.py`

**Interfaces:**
- Consumes: `config`, `build.localizar_msbuild`
- Produces: `@dataclass Verificacao(nome: str, ok: bool, detalhe: str, bloqueia: bool, afeta: list[str])`; `verificar_tudo() -> list[Verificacao]` com os nomes fixos `maw_fechada`, `msbuild`, `edge`, `disco`, `vb_cable`, `python310`, `ffmpeg`, `midi_loopback`, `nao_perturbe`, `tela`; `requisitos_ausentes(vs: list[Verificacao]) -> set[str]` (união de `afeta` das que falharam); `ambiente() -> dict` (windows, python, cpu, ram_gb, dpi, resolucao, dispositivos_audio, portas_midi, msbuild, edge).

Requisitos usados em `afeta` (e nos campos `requisitos` do catálogo): `gui`, `vb-cable`, `python310`, `ffmpeg`, `midi-loopback`, `gemini`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_preflight.py
from maw_agent import preflight

NOMES = {"maw_fechada", "msbuild", "edge", "disco", "vb_cable", "python310", "ffmpeg",
         "midi_loopback", "nao_perturbe", "tela"}

def test_todas_as_verificacoes_presentes():
    vs = preflight.verificar_tudo()
    assert {v.nome for v in vs} == NOMES
    assert all(isinstance(v.detalhe, str) and v.detalhe for v in vs)

def test_requisitos_ausentes_une_afeta():
    vs = [preflight.Verificacao("a", False, "x", False, ["vb-cable"]),
          preflight.Verificacao("b", True, "x", False, ["gui"]),
          preflight.Verificacao("c", False, "x", False, ["python310", "gui"])]
    assert preflight.requisitos_ausentes(vs) == {"vb-cable", "python310", "gui"}

def test_maw_aberta_bloqueia_gui(monkeypatch):
    monkeypatch.setattr(preflight, "_processos", lambda: ["explorer.exe", "MAW_APP.exe"])
    v = next(v for v in preflight.verificar_tudo() if v.nome == "maw_fechada")
    assert not v.ok and "gui" in v.afeta

def test_ambiente_tem_campos():
    amb = preflight.ambiente()
    for k in ("windows", "python", "cpu", "ram_gb", "dpi", "resolucao", "dispositivos_audio", "portas_midi"):
        assert k in amb
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_preflight.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar**

```python
# maw_agent/preflight.py
"""Pré-voo: o que falta vira limitação declarada, nunca um teste pulado em silêncio."""
from __future__ import annotations
import ctypes
import platform
import shutil
import subprocess
import winreg
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

from . import config


@dataclass
class Verificacao:
    nome: str
    ok: bool
    detalhe: str
    bloqueia: bool
    afeta: list[str]

    def como_dict(self) -> dict:
        return asdict(self)


EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def _processos() -> list[str]:
    return [p.info["name"] or "" for p in psutil.process_iter(["name"])]


def _dispositivos_audio() -> list[str]:
    try:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        try:
            return sorted({p.get_device_info_by_index(i)["name"] for i in range(p.get_device_count())})
        finally:
            p.terminate()
    except Exception as e:  # sem PortAudio utilizável: registrado, não escondido
        return [f"(erro ao listar: {e})"]


def _portas_midi() -> list[str]:
    try:
        import mido
        return sorted(set(mido.get_input_names()) | set(mido.get_output_names()))
    except Exception as e:
        return [f"(erro ao listar: {e})"]


def _python310() -> str | None:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")
    try:
        p = subprocess.run([uv, "python", "find", "3.10"], capture_output=True, text=True, timeout=30)
        return p.stdout.strip() or None if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ffmpeg() -> str | None:
    local = config.FERRAMENTAS / "ffmpeg" / "bin" / "ffmpeg.exe"
    return str(local) if local.exists() else shutil.which("ffmpeg")


def _nao_perturbe() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings") as k:
            v, _ = winreg.QueryValueEx(k, "NOC_GLOBAL_SETTING_TOASTS_ENABLED")
            return v == 0
    except OSError:
        return False


def _dpi_e_tela() -> tuple[int, str]:
    u = ctypes.windll.user32
    try:
        dpi = u.GetDpiForSystem()
    except AttributeError:
        dpi = 96
    return dpi, f"{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}"


def verificar_tudo() -> list[Verificacao]:
    vs: list[Verificacao] = []
    aberta = any(n.lower() == "maw_app.exe" for n in _processos())
    vs.append(Verificacao("maw_fechada", not aberta,
                          "MAW está aberta: feche-a antes da fase de GUI" if aberta else "MAW fechada",
                          False, ["gui"]))
    try:
        from .build import localizar_msbuild
        vs.append(Verificacao("msbuild", True, str(localizar_msbuild()), True, []))
    except FileNotFoundError as e:
        vs.append(Verificacao("msbuild", False, str(e), True, []))
    vs.append(Verificacao("edge", EDGE.exists(), str(EDGE) if EDGE.exists() else "Edge não encontrado", True, []))
    livre = shutil.disk_usage(config.RAIZ).free / 1e9
    vs.append(Verificacao("disco", livre >= 30, f"{livre:.0f} GB livres (mínimo 30)", True, []))
    audio = _dispositivos_audio()
    cabo = any("CABLE" in d.upper() for d in audio)
    vs.append(Verificacao("vb_cable", cabo, "VB-Cable presente" if cabo else "VB-Cable não instalado",
                          False, ["vb-cable"]))
    py = _python310()
    vs.append(Verificacao("python310", bool(py), py or "Python 3.10 ausente (uv python install 3.10)",
                          False, ["python310"]))
    ff = _ffmpeg()
    vs.append(Verificacao("ffmpeg", bool(ff), ff or "ffmpeg ausente", False, ["ffmpeg"]))
    midi = _portas_midi()
    loop = any("loopback" in m.lower() for m in midi)
    vs.append(Verificacao("midi_loopback", loop, ", ".join(midi) or "nenhuma porta MIDI", False, ["midi-loopback"]))
    np_ok = _nao_perturbe()
    vs.append(Verificacao("nao_perturbe", np_ok, "Não perturbe ligado" if np_ok else
                          "Não perturbe desligado: notificações podem roubar o foco da GUI", False, []))
    dpi, tela = _dpi_e_tela()
    vs.append(Verificacao("tela", True, f"{tela} a {dpi} dpi ({dpi * 100 // 96}%)", False, []))
    return vs


def requisitos_ausentes(vs: list[Verificacao]) -> set[str]:
    return {r for v in vs if not v.ok for r in v.afeta}


def ambiente() -> dict:
    dpi, tela = _dpi_e_tela()
    try:
        from .build import localizar_msbuild
        msb = str(localizar_msbuild())
    except FileNotFoundError:
        msb = None
    return {
        "windows": f"{platform.system()} {platform.release()} ({platform.version()})",
        "python": platform.python_version(),
        "cpu": platform.processor(),
        "nucleos": psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "dpi": dpi,
        "resolucao": tela,
        "dispositivos_audio": _dispositivos_audio(),
        "portas_midi": _portas_midi(),
        "msbuild": msb,
        "edge": str(EDGE) if EDGE.exists() else None,
    }
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_preflight.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add maw_agent/preflight.py tests/test_preflight.py
git commit -m "feat(preflight): pre-voo com requisitos ausentes declarados e retrato do ambiente"
```

---

### Task 12: Relatório — contexto, modelo HTML e PDF com anexo (`relatorio/`)

**Files:**
- Create: `maw_agent/relatorio/__init__.py`, `maw_agent/relatorio/contexto.py`, `maw_agent/relatorio/pdf.py`, `maw_agent/relatorio/modelos/relatorio.html.j2`, `maw_agent/relatorio/modelos/estilo.css`, `tests/test_relatorio.py`

**Interfaces:**
- Consumes: `catalogo.montar_matriz/resumo/carregar_resultados`, `achados`, `sandbox`
- Produces:
  - `contexto.semaforo(achados_do_alvo: list[dict], build_ok: bool) -> str` (`"bloqueado"` se build falhou ou há achado `critica`/`alta` confirmado e não corrigido; `"com_ressalvas"` se há qualquer achado aberto; senão `"pronto"`)
  - `contexto.montar(pasta: Path, itens: list[dict], principios: list[dict], anterior: Path | None, logo: Path | None) -> dict`
  - `pdf.renderizar(ctx: dict, destino: Path) -> Path`
  - `pdf.anexar(pdf: Path, arquivo: Path, nome: str) -> None`
  - `pdf.ler_anexo(pdf: Path, nome: str) -> bytes`

Arquivos que `montar` lê da pasta da sprint (todos opcionais, com ausência declarada): `estado.json`, `alvos.json` (`{"alvos": [...], "avisos": [...]}`), `preflight.json` (lista de Verificacao), `ambiente.json`, `builds/*.json`, `suites/*.json`, `benchmarks/*.json`, `achados.json`, `resultados.jsonl`, `intocada.json` (`{"verificado": bool, "diferencas": [...], "ambiente_restaurado": bool}`), `textos.json` (`{"veredito": str, "resumo": str, "por_alvo": {alvo: str}}`), `derrubados.json` (lista de `{"titulo", "justificativa"}`), `erros_agente.json` (lista de str).

Chaves do contexto (o modelo usa exatamente estas): `sprint`, `data`, `duracao`, `alvos` (lista de dicts do alvo + `semaforo`, `build`, `suite`, `benchmark`, `achados_ids`, `texto`), `avisos_alvos`, `veredito`, `resumo`, `contagens` (`{"por_severidade": {...}, "por_estado": {...}, "por_tipo": {...}}`), `top5`, `achados` (não-melhorias, ordenados), `melhorias`, `corrigidos`, `regressoes`, `principios` (cada um com `celulas` por alvo), `matriz` (`{"alvos": [...], "linhas": [{"item", "titulo", "area", "celulas": [...]}]}`), `cobertura` (resumo), `limitacoes` (lista de str), `derrubados`, `erros_agente`, `intocada`, `ambiente`, `preflight`, `logo_data_uri`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_relatorio.py
import json
from pathlib import Path
from pypdf import PdfReader
from maw_agent.relatorio import contexto, pdf

ACHADO = {"id": "MAW-0001", "estado": "novo", "titulo": "Suíte diz passou mas sai com 1", "tipo": "violacao",
          "severidade": "alta", "prioridade": None, "alvos": [{"alvo": "feature-x", "commit": "abc"}],
          "item_catalogo": "saude/suite-existente", "principio": "P4", "passos": ["rodar"],
          "esperado": "coerente", "obtido": "incoerente", "evidencias": [], "causa_provavel": None,
          "sugestao": "corrigir", "criterio_aceite": "suite coerente", "confianca": "confirmado",
          "assinatura": "x", "introduzido_por": "feature-x", "historico": ["01"]}

def _sprint(tmp_path: Path) -> Path:
    s = tmp_path / "sprint-01"; s.mkdir()
    (s / "estado.json").write_text(json.dumps({"numero": 1, "criado": "2026-09-28T01:00:00", "passos": {}}))
    (s / "alvos.json").write_text(json.dumps({"alvos": [
        {"nome": "main", "branch": "main", "commit": "a" * 40, "origem": "github", "compartilha_com": None},
        {"nome": "feature-x", "branch": "feature/x", "commit": "b" * 40, "origem": "github", "compartilha_com": None}],
        "avisos": ["MAW_a1: há alterações não commitadas"]}))
    (s / "achados.json").write_text(json.dumps([ACHADO]))
    (s / "intocada.json").write_text(json.dumps({"verificado": True, "diferencas": [], "ambiente_restaurado": True}))
    (s / "resultados.jsonl").write_text(json.dumps({"item": "saude/suite-existente", "alvo": "main",
                                                    "resultado": "passou", "motivo": None, "achados": [], "fonte": "suite"}) + "\n")
    return s

ITENS = [{"id": "saude/suite-existente", "area": "saude", "titulo": "Suíte existente passa", "descricao": "d",
          "origem": ["x"], "verificacao": ["suite"], "cenarios": [], "requisitos": [], "marco": "M1"},
         {"id": "edicao/split", "area": "edicao", "titulo": "Split", "descricao": "d",
          "origem": ["x"], "verificacao": ["e2e"], "cenarios": [], "requisitos": ["gui"], "marco": "M2"}]

def test_semaforo():
    assert contexto.semaforo([], True) == "pronto"
    assert contexto.semaforo([], False) == "bloqueado"
    assert contexto.semaforo([ACHADO], True) == "bloqueado"
    assert contexto.semaforo([dict(ACHADO, severidade="baixa")], True) == "com_ressalvas"
    assert contexto.semaforo([dict(ACHADO, confianca="provavel")], True) == "com_ressalvas"

def test_contexto_completo_e_matriz_sem_buraco(tmp_path):
    ctx = contexto.montar(_sprint(tmp_path), ITENS, [], None, None)
    assert ctx["sprint"] == "sprint-01"
    assert [a["nome"] for a in ctx["alvos"]] == ["main", "feature-x"]
    assert {a["nome"]: a["semaforo"] for a in ctx["alvos"]}["feature-x"] == "bloqueado"
    assert all(len(l["celulas"]) == 2 for l in ctx["matriz"]["linhas"])
    assert ctx["cobertura"]["passou"] == 1 and ctx["cobertura"]["nao_testavel"] == 3
    assert any("não commitadas" in l for l in ctx["limitacoes"])
    assert ctx["intocada"]["verificado"] is True

def test_pdf_real_com_anexo(tmp_path):
    s = _sprint(tmp_path)
    ctx = contexto.montar(s, ITENS, [], None, None)
    destino = pdf.renderizar(ctx, s / "MAW-Sprint-01.pdf")
    texto = "\n".join(p.extract_text() for p in PdfReader(destino).pages)
    assert "MAW intocada" in texto and "MAW-0001" in texto and "Matriz de cobertura" in texto
    pdf.anexar(destino, s / "achados.json", "achados.json")
    assert json.loads(pdf.ler_anexo(destino, "achados.json"))[0]["id"] == "MAW-0001"
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_relatorio.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar `contexto.py`**

```python
# maw_agent/relatorio/__init__.py
"""Geração do PDF da sprint."""
```

```python
# maw_agent/relatorio/contexto.py
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
    limitacoes: list[str] = list(alvos_info.get("avisos", []))
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
        alvos.append({**a, "semaforo": semaforo(doalvo, build_ok),
                      "semaforo_rotulo": _ROTULO_SEMAFORO[semaforo(doalvo, build_ok)],
                      "build": b_rel, "build_debug": b_dbg,
                      "suite": _ler(pasta / "suites" / f"{origem}.json"),
                      "benchmark": _ler(pasta / "benchmarks" / f"{origem}.json"),
                      "achados_ids": [x["id"] for x in doalvo],
                      "introduzidos": [x["id"] for x in doalvo if x.get("introduzido_por") == a["nome"]],
                      "texto": textos.get("por_alvo", {}).get(a["nome"], "")})
    resultados = catalogo.carregar_resultados(pasta)
    m = catalogo.montar_matriz(itens, nomes, resultados)
    linhas = [{"item": it["id"], "titulo": it["titulo"], "area": it["area"],
               "celulas": [m[it["id"]][n] for n in nomes]} for it in itens]
    cobertura = catalogo.resumo(m)
    motivos = Counter(c["motivo"] for l in m.values() for c in l.values() if c["resultado"] == "nao_testavel")
    for motivo, n in motivos.most_common():
        limitacoes.append(f"{n} célula(s) não testável(is): {motivo}")
    problemas = [a for a in achados_lista if a["tipo"] != "melhoria"]
    melhorias = [a for a in achados_lista if a["tipo"] == "melhoria"]
    provaveis = [a for a in achados_lista if a.get("confianca") == "provavel"]
    if provaveis:
        limitacoes.append(f"{len(provaveis)} achado(s) marcado(s) como provável(is): evidentes no código, não reproduzidos dinamicamente")
    derrubados = _ler(pasta / "derrubados.json", [])
    erros_agente = _ler(pasta / "erros_agente.json", [])
    for passo, info in estado.get("passos", {}).items():
        if info.get("status") == "falhou":
            erros_agente.append(f"passo {passo} falhou: {info.get('erro')}")
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
        "resumo": textos.get("resumo", ""),
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
```

- [ ] **Step 4: Implementar `pdf.py`**

```python
# maw_agent/relatorio/pdf.py
"""HTML (Jinja2) → PDF pelo Edge do Windows (Playwright, canal msedge) + anexo JSON (pypdf)."""
from __future__ import annotations
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter

from .. import sandbox

_MODELOS = Path(__file__).parent / "modelos"
_ROTULOS = {"passou": "✓", "falhou": "✗", "nao_testavel": "–", "na": "n/a"}


def _html(ctx: dict) -> str:
    env = Environment(loader=FileSystemLoader(_MODELOS), autoescape=select_autoescape(["html", "j2"]))
    env.filters["simbolo"] = lambda r: _ROTULOS.get(r, "?")
    css = (_MODELOS / "estilo.css").read_text(encoding="utf-8")
    return env.get_template("relatorio.html.j2").render(**ctx, css=css)


def renderizar(ctx: dict, destino: Path) -> Path:
    destino = sandbox.garantir_escrita(Path(destino))
    html = _html(ctx)
    rodape = ('<div style="font-size:8px;width:100%;padding:0 12mm;color:#666;display:flex;justify-content:space-between">'
              f'<span>MAW — Relatório de testes · {ctx["sprint"]}</span>'
              '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
    with sync_playwright() as pw:
        nav = pw.chromium.launch(channel="msedge")
        try:
            pagina = nav.new_page()
            pagina.set_content(html, wait_until="load")
            dados = pagina.pdf(format="A4", print_background=True, display_header_footer=True,
                               header_template="<span></span>", footer_template=rodape,
                               margin={"top": "14mm", "bottom": "16mm", "left": "12mm", "right": "12mm"},
                               prefer_css_page_size=True)
        finally:
            nav.close()
    sandbox.escrever_bytes(destino, dados)
    return destino


def anexar(pdf: Path, arquivo: Path, nome: str) -> None:
    escritor = PdfWriter(clone_from=PdfReader(pdf))
    escritor.add_attachment(nome, Path(arquivo).read_bytes())
    tmp = Path(pdf).with_suffix(".tmp.pdf")
    sandbox.garantir_escrita(tmp)
    with open(tmp, "wb") as f:
        escritor.write(f)
    sandbox.remover(Path(pdf))
    tmp.rename(pdf)


def ler_anexo(pdf: Path, nome: str) -> bytes:
    anexos = PdfReader(pdf).attachments
    if nome not in anexos:
        raise KeyError(f"{nome} não está anexado a {pdf}")
    return anexos[nome][0]
```

- [ ] **Step 5: Escrever o modelo e o estilo**

```jinja
{# maw_agent/relatorio/modelos/relatorio.html.j2 #}
<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>MAW — {{ sprint }}</title>
<style>{{ css }}</style></head>
<body>

<section class="capa">
  {% if logo_data_uri %}<img class="logo" src="{{ logo_data_uri }}" alt="MAW">{% else %}<div class="logo-texto">MAW</div>{% endif %}
  <h1>Relatório de testes</h1>
  <p class="sub">{{ sprint }} · {{ data }} · duração {{ duracao }}</p>
  <table class="alvos-capa">
    <tr><th>Alvo</th><th>Branch</th><th>Commit</th><th>Origem</th><th>Situação</th></tr>
    {% for a in alvos %}<tr><td>{{ a.nome }}</td><td>{{ a.branch }}</td><td class="mono">{{ a.commit[:10] }}</td>
      <td>{{ a.origem }}</td><td><span class="sem {{ a.semaforo }}">{{ a.semaforo_rotulo }}</span></td></tr>{% endfor %}
  </table>
  <p class="veredito">{{ veredito }}</p>
  <p class="intocada {{ 'ok' if intocada.verificado else 'falha' }}">
    {% if intocada.verificado %}MAW intocada: verificado{% else %}MAW intocada: NÃO verificado — {{ intocada.diferencas | join('; ') }}{% endif %}
  </p>
</section>

<section>
  <h2>1. Resumo executivo</h2>
  {% if resumo %}<div class="texto">{{ resumo }}</div>{% endif %}
  <div class="cartoes">
    {% for sev in ['critica','alta','media','baixa'] %}
      <div class="cartao {{ sev }}"><b>{{ contagens.por_severidade.get(sev, 0) }}</b><span>{{ sev }}</span></div>
    {% endfor %}
    <div class="cartao"><b>{{ contagens.por_estado.get('novo', 0) }}</b><span>novos</span></div>
    <div class="cartao"><b>{{ contagens.por_estado.get('corrigido', 0) }}</b><span>corrigidos</span></div>
    <div class="cartao"><b>{{ contagens.por_estado.get('regressao', 0) }}</b><span>regressões</span></div>
  </div>
  <h3>Os mais graves</h3>
  {% if top5 %}<ol>{% for a in top5 %}<li><b>{{ a.id }}</b> [{{ a.severidade }}] {{ a.titulo }} — {{ a.alvos | map(attribute='alvo') | join(', ') }}</li>{% endfor %}</ol>
  {% else %}<p>Nenhum achado aberto.</p>{% endif %}
  <h3>Cobertura</h3>
  <p>{{ cobertura.passou }} passaram · {{ cobertura.falhou }} falharam · {{ cobertura.nao_testavel }} não testáveis · {{ cobertura.na }} não se aplicam
     (de {{ matriz.linhas | length }} itens × {{ matriz.alvos | length }} alvos).</p>
</section>

<section>
  <h2>2. Comparação com a sprint anterior</h2>
  {% if anterior %}<p>Comparado com {{ anterior }}.</p>{% else %}<p>Primeira sprint: não há comparação.</p>{% endif %}
  <p><b>Corrigidos:</b> {% for a in corrigidos %}{{ a.id }} {% else %}nenhum{% endfor %} ·
     <b>Regressões:</b> {% for a in regressoes %}{{ a.id }} {% else %}nenhuma{% endfor %}</p>
</section>

<section>
  <h2>3. Por alvo</h2>
  {% for a in alvos %}
  <div class="alvo">
    <h3>{{ a.nome }} <span class="sem {{ a.semaforo }}">{{ a.semaforo_rotulo }}</span></h3>
    <p class="mono">{{ a.branch }} @ {{ a.commit[:10] }} ({{ a.origem }}){% if a.compartilha_com %} — mesma árvore de código de {{ a.compartilha_com }}: execução compartilhada{% endif %}</p>
    {% if a.texto %}<p>{{ a.texto }}</p>{% endif %}
    <p>Achados neste alvo: {{ a.achados_ids | join(', ') or 'nenhum' }}{% if a.introduzidos %} · introduzidos por esta branch: {{ a.introduzidos | join(', ') }}{% endif %}</p>
  </div>
  {% endfor %}
</section>

<section class="quebra">
  <h2>4. Instruções para o agente de correção</h2>
  <ol>
    <li>Reproduza cada achado seguindo os passos antes de mudar qualquer código.</li>
    <li>Respeite os princípios da MAW; uma correção que viola um princípio não é correção.</li>
    <li>Nunca altere um teste para que ele passe.</li>
    <li>Um achado por commit, citando o ID (ex.: <span class="mono">fix: … (MAW-0001)</span>).</li>
    <li>O achado só está corrigido quando o critério de aceite passa.</li>
    <li>Os dados exatos de todos os achados estão no arquivo <span class="mono">achados.json</span> anexado a este PDF.</li>
  </ol>
</section>

<section>
  <h2>5. Achados</h2>
  {% for a in achados %}
  <article class="ficha {{ a.severidade }}">
    <h3>{{ a.id }} · {{ a.titulo }}</h3>
    <p class="meta">{{ a.tipo }} · {{ a.severidade }} · {{ a.confianca }} · {{ a.estado }}{% if a.principio %} · princípio {{ a.principio }}{% endif %} · item <span class="mono">{{ a.item_catalogo }}</span></p>
    <p class="meta">Alvos: {% for x in a.alvos %}{{ x.alvo }}@{{ x.commit[:8] }} {% endfor %}{% if a.introduzido_por %}· introduzido por {{ a.introduzido_por }}{% endif %}</p>
    <h4>Passos</h4><ol>{% for p in a.passos %}<li>{{ p }}</li>{% endfor %}</ol>
    <div class="duas"><div><h4>Esperado</h4><p>{{ a.esperado }}</p></div><div><h4>Obtido</h4><p>{{ a.obtido }}</p></div></div>
    {% if a.causa_provavel %}<h4>Causa provável (hipótese)</h4><p><span class="mono">{{ a.causa_provavel.arquivo_linha }}</span> — {{ a.causa_provavel.texto }}</p>{% endif %}
    <h4>Sugestão</h4><p>{{ a.sugestao }}</p>
    <h4>Critério de aceite</h4><p>{{ a.criterio_aceite }}</p>
    {% for ev in a.evidencias if ev.embutir and ev.data_uri %}<figure><img src="{{ ev.data_uri }}"><figcaption>{{ ev.legenda }}</figcaption></figure>{% endfor %}
  </article>
  {% else %}<p>Nenhum achado.</p>{% endfor %}
</section>

<section>
  <h2>6. Melhorias propostas</h2>
  {% for a in melhorias %}
  <article class="ficha melhoria"><h3>{{ a.id }} · {{ a.titulo }}</h3>
    <p class="meta">prioridade {{ a.prioridade }}{% if a.principio %} · serve ao princípio {{ a.principio }}{% endif %}</p>
    <p>{{ a.obtido }}</p><h4>Proposta</h4><p>{{ a.sugestao }}</p><h4>Critério de aceite</h4><p>{{ a.criterio_aceite }}</p></article>
  {% else %}<p>Nenhuma melhoria proposta.</p>{% endfor %}
</section>

<section>
  <h2>7. Aderência à ideia da MAW</h2>
  {% if principios %}
  <table class="grade"><tr><th>Princípio</th>{% for n in matriz.alvos %}<th>{{ n }}</th>{% endfor %}<th>Violações</th></tr>
  {% for p in principios %}<tr><td>{{ p.id }} — {{ p.titulo }}</td>{% for c in p.celulas %}<td class="c {{ c.resultado }}">{{ c.resultado | simbolo }}</td>{% endfor %}<td>{{ p.violacoes | join(', ') }}</td></tr>{% endfor %}
  </table>{% else %}<p>Princípios não carregados nesta execução.</p>{% endif %}
</section>

<section>
  <h2>8. Saúde técnica</h2>
  <table class="grade"><tr><th>Alvo</th><th>Build Release</th><th>Build Debug</th><th>Suíte</th><th>Benchmark (32 trilhas, carga máx.)</th></tr>
  {% for a in alvos %}<tr><td>{{ a.nome }}</td>
    <td>{% if a.build %}{{ 'ok' if a.build.ok else 'FALHOU' }} · {{ a.build.segundos }} s · {{ a.build.avisos | length }} avisos{% else %}não registrado{% endif %}</td>
    <td>{% if a.build_debug %}{{ 'ok' if a.build_debug.ok else 'FALHOU' }} · {{ a.build_debug.segundos }} s{% else %}não registrado{% endif %}</td>
    <td>{% if a.suite %}{{ a.suite.total_ok }} ok / {{ a.suite.total_falhas }} falhas em {{ a.suite.blocos | length }} blocos{% if a.suite.assercoes %} · {{ a.suite.assercoes | length }} jassert{% endif %}{% else %}não registrada{% endif %}</td>
    <td>{% if a.benchmark and a.benchmark.linhas %}{{ a.benchmark.linhas[-1].carga_max }} %{% else %}não registrado{% endif %}</td></tr>{% endfor %}
  </table>
</section>

<section class="paisagem">
  <h2>9. Matriz de cobertura</h2>
  <p class="legenda">✓ passou · ✗ falhou · – não testável (motivo nas limitações) · n/a não se aplica</p>
  <table class="matriz"><thead><tr><th>Item</th>{% for n in matriz.alvos %}<th>{{ n }}</th>{% endfor %}</tr></thead>
  <tbody>{% for l in matriz.linhas %}<tr><td><span class="mono">{{ l.item }}</span> {{ l.titulo }}</td>
    {% for c in l.celulas %}<td class="c {{ c.resultado }}">{{ c.resultado | simbolo }}{% if c.achados %}<br><small>{{ c.achados | join(' ') }}</small>{% endif %}</td>{% endfor %}</tr>{% endfor %}</tbody></table>
</section>

<section>
  <h2>10. Limitações desta sprint</h2>
  <ul>{% for l in limitacoes %}<li>{{ l }}</li>{% else %}<li>Nenhuma.</li>{% endfor %}</ul>
  <h3>Achados derrubados na verificação ({{ derrubados | length }})</h3>
  <ul>{% for d in derrubados %}<li>{{ d.titulo }} — {{ d.justificativa }}</li>{% else %}<li>Nenhum.</li>{% endfor %}</ul>
  <h3>Erros do próprio agente</h3>
  <ul>{% for e in erros_agente %}<li>{{ e }}</li>{% else %}<li>Nenhum.</li>{% endfor %}</ul>
</section>

<section>
  <h2>11. Ambiente e método</h2>
  <table class="grade">{% for k, v in ambiente.items() %}<tr><td>{{ k }}</td><td>{{ v if v is string else (v | join(', ') if v is iterable else v) }}</td></tr>{% endfor %}</table>
  <h3>Pré-voo</h3>
  <ul>{% for v in preflight %}<li>{{ '✓' if v.ok else '✗' }} {{ v.nome }} — {{ v.detalhe }}</li>{% endfor %}</ul>
</section>
</body></html>
```

```css
/* maw_agent/relatorio/modelos/estilo.css */
:root { --roxo: #7b4dff; --roxo-escuro: #2a1d4d; --fundo-capa: #0e0e11; --texto: #1d1d22; --cinza: #6b6b76;
        --verde: #1f9d55; --amarelo: #c98a00; --vermelho: #d33a3a; }
@page { size: A4; }
@page paisagem { size: A4 landscape; }
body { font-family: "Segoe UI", Arial, sans-serif; color: var(--texto); font-size: 10.5pt; line-height: 1.45; margin: 0; }
h1 { font-size: 30pt; margin: 8mm 0 2mm; }
h2 { font-size: 16pt; color: var(--roxo-escuro); border-bottom: 2px solid var(--roxo); padding-bottom: 2mm; margin-top: 8mm; }
h3 { font-size: 12pt; margin: 5mm 0 2mm; }
h4 { font-size: 10pt; margin: 3mm 0 1mm; color: var(--cinza); text-transform: uppercase; letter-spacing: .04em; }
.mono { font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; }
section { break-inside: auto; }
.quebra { break-before: page; }
.capa { background: var(--fundo-capa); color: #eee; min-height: 262mm; padding: 18mm 14mm; box-sizing: border-box;
        break-after: page; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.capa .logo { height: 34mm; } .capa .logo-texto { font-size: 44pt; font-weight: 800; color: var(--roxo); }
.capa .sub { color: #aaa; } .capa table { width: 100%; border-collapse: collapse; margin: 8mm 0; }
.capa th, .capa td { border-bottom: 1px solid #333; padding: 2mm; text-align: left; font-size: 9.5pt; }
.veredito { font-size: 13pt; border-left: 4px solid var(--roxo); padding-left: 4mm; }
.intocada { font-weight: 700; } .intocada.ok { color: #58d68d; } .intocada.falha { color: #ff6b6b; }
.sem { padding: .5mm 2mm; border-radius: 2mm; font-size: 8.5pt; font-weight: 700; color: #fff; }
.sem.pronto { background: var(--verde); } .sem.com_ressalvas { background: var(--amarelo); } .sem.bloqueado { background: var(--vermelho); }
.cartoes { display: flex; gap: 3mm; flex-wrap: wrap; margin: 4mm 0; }
.cartao { border: 1px solid #ddd; border-radius: 2mm; padding: 2mm 4mm; min-width: 22mm; text-align: center; }
.cartao b { display: block; font-size: 16pt; } .cartao span { color: var(--cinza); font-size: 8.5pt; }
.cartao.critica b { color: var(--vermelho); } .cartao.alta b { color: #e8692c; }
.ficha { border: 1px solid #ddd; border-left: 4px solid var(--roxo); border-radius: 1.5mm; padding: 3mm 4mm; margin: 4mm 0; break-inside: avoid; }
.ficha.critica { border-left-color: var(--vermelho); } .ficha.alta { border-left-color: #e8692c; }
.ficha.media { border-left-color: var(--amarelo); } .ficha.melhoria { border-left-color: var(--verde); }
.meta { color: var(--cinza); font-size: 9pt; margin: 0; }
.duas { display: flex; gap: 6mm; } .duas > div { flex: 1; }
figure img { max-width: 100%; max-height: 90mm; border: 1px solid #ccc; } figcaption { font-size: 8.5pt; color: var(--cinza); }
table.grade { width: 100%; border-collapse: collapse; font-size: 9pt; }
table.grade th, table.grade td { border: 1px solid #ddd; padding: 1.5mm 2mm; text-align: left; vertical-align: top; }
.paisagem { page: paisagem; break-before: page; }
table.matriz { width: 100%; border-collapse: collapse; font-size: 7.5pt; }
table.matriz th, table.matriz td { border: 1px solid #e3e3e3; padding: .8mm 1.2mm; }
table.matriz thead { display: table-header-group; } table.matriz tr { break-inside: avoid; }
td.c { text-align: center; font-weight: 700; }
td.c.passou { color: var(--verde); } td.c.falhou { color: var(--vermelho); background: #fdeaea; }
td.c.nao_testavel { color: #999; } td.c.na { color: #bbb; font-weight: 400; }
.legenda { font-size: 8.5pt; color: var(--cinza); }
```

- [ ] **Step 6: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_relatorio.py -v`
Expected: 3 passed (o teste do PDF abre o Edge em modo headless)

- [ ] **Step 7: Commit**

```bash
git add maw_agent/relatorio tests/test_relatorio.py
git commit -m "feat(relatorio): contexto, modelo HTML e PDF pelo Edge com achados.json anexado"
```

---

### Task 13: Fases da sprint na CLI (`fases.py`)

**Files:**
- Create: `maw_agent/fases.py`, `tests/test_fases.py`
- (nenhuma mudança em `cli.py`: `fases` já está na lista de `_carregar_modulos`)

**Interfaces:**
- Consumes: todos os módulos anteriores
- Produces: subcomando `sprint` com ações, cada uma imprimindo um JSON-resumo em stdout e devolvendo 0 (ok) ou 1 (falhou):
  - `sprint iniciar [--nova]` → retoma a sprint em andamento ou cria uma nova; `{"sprint", "pasta", "retomada": bool}`
  - `sprint preflight` → grava `preflight.json` e `ambiente.json`
  - `sprint preparar` → espelho, clones locais, alvos (`alvos.json`), worktrees, `prova-antes.json`, backup do `%APPDATA%\MAW` (caminho em `estado.dados("preparar")["backup"]`)
  - `sprint compilar [--alvo NOME]` → `builds/<alvo>-<Release|Debug>.json`; build que falha gera achado bruto `achados-brutos/build-<alvo>-<config>.json` e resultado `saude/build-<config minúsculo>`
  - `sprint suite [--alvo NOME]` → Debug `--run-tests` sob `CapturaDepuracao`, Release `--benchmark`; grava `suites/<alvo>.json` (com `assercoes`) e `benchmarks/<alvo>.json`; achados brutos por bloco que falhou, por incoerência e por jassert; resultados de `saude/suite-existente`, `saude/benchmark` e de cada item do catálogo cujo `cenarios` tem entradas `suite:<prefixo do nome do bloco>`; alvos com `compartilha_com` copiam os resultados da origem com o motivo "mesma árvore de código de X"
  - `achado validar ARQUIVO` / `achado registrar ARQUIVO` → valida pelo esquema; `registrar` copia para `achados-brutos/` da sprint em andamento
  - `sprint consolidar` → junta `achados-brutos/*.json` + `vereditos/*.json` (mesmo nome de arquivo) + `reverificacoes.json`; grava `achados.json`, `derrubados.json`, atualiza `privado/historico/achados.json`; registra `falhou` nas células com achado `confirmado`
  - `sprint encerrar` → restaura o `%APPDATA%\MAW`, grava `prova-depois.json` e `intocada.json`
  - `sprint relatorio` → monta o contexto, gera `MAW-Sprint-NN.pdf`, anexa `achados.json`
  - `sprint status` → imprime o estado
- Funções puras testáveis: `achados_da_suite(alvo: dict, resultado: dict) -> list[dict]`, `achado_de_build(alvo: dict, r: dict) -> dict`, `resultados_suite_por_item(itens: list[dict], blocos: list[dict]) -> dict[str, tuple[str, str | None]]`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_fases.py
from maw_agent import achados, fases

ALVO = {"nome": "main", "commit": "a" * 40}

def test_achado_de_build_e_valido_e_critico():
    a = fases.achado_de_build(ALVO, {"config": "Release", "erros": ["c.cpp(1): error C2065: x"], "log": "l.log"})
    assert achados.validar(a) == [] and a["severidade"] == "critica" and a["item_catalogo"] == "saude/build-release"

def test_achados_da_suite_por_bloco_incoerencia_e_jassert():
    r = {"blocos": [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0},
                    {"nome": "Bloco B", "sub": "y", "ok": 1, "falhas": 2}],
         "detalhes": ["- Bloco B / y", "!!! Test 1 failed: z"], "incoerencias": ["relatório diz TUDO PASSOU mas o código de saída foi 1"],
         "assercoes": ["JUCE Assertion failure in a.cpp:10"], "exit_code": 1, "declarado": "PASSOU"}
    lista = fases.achados_da_suite(ALVO, r)
    assert all(achados.validar(a) == [] for a in lista)
    tipos = sorted(a["tipo"] for a in lista)
    assert tipos == ["erro", "erro", "violacao"]

def test_resultados_suite_por_item():
    itens = [{"id": "g/grade", "cenarios": ["suite:Bloco A"]}, {"id": "g/b", "cenarios": ["suite:Bloco B"]},
             {"id": "g/c", "cenarios": ["suite:Inexistente"]}, {"id": "g/d", "cenarios": []}]
    blocos = [{"nome": "Bloco A", "sub": "x", "ok": 1, "falhas": 0},
              {"nome": "Bloco B", "sub": "y", "ok": 1, "falhas": 1}]
    r = fases.resultados_suite_por_item(itens, blocos)
    assert r["g/grade"] == ("passou", None)
    assert r["g/b"][0] == "falhou"
    assert r["g/c"][0] == "nao_testavel" and "não encontrado" in r["g/c"][1]
    assert "g/d" not in r
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/Scripts/python -m pytest tests/test_fases.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar**

```python
# maw_agent/fases.py
"""Fases da sprint expostas como `python -m maw_agent sprint <acao>` e `achado <acao>`."""
from __future__ import annotations
import argparse
import json
import sys
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
    return {"titulo": f"Build {cfg} de {alvo['nome']} não compila", "tipo": "erro", "severidade": "critica",
            "prioridade": None, "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
            "item_catalogo": f"saude/build-{cfg.lower()}", "principio": "P14",
            "passos": [f"msbuild Builds\\VisualStudio2022\\MAW_APP_App.vcxproj /p:Configuration={cfg} /p:Platform=x64"],
            "esperado": "compilação sem erros", "obtido": "; ".join(r["erros"][:5]),
            "evidencias": [{"arquivo": r["log"], "legenda": "log do MSBuild", "embutir": False}],
            "causa_provavel": None, "sugestao": "corrigir os erros de compilação listados",
            "criterio_aceite": f"o build {cfg} compila sem erros", "confianca": "confirmado",
            "assinatura": f"build:{cfg}:" + (r["erros"][0] if r["erros"] else "sem-erro"), "fonte": "build"}


def achados_da_suite(alvo: dict, r: dict) -> list[dict]:
    base = {"prioridade": None, "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
            "evidencias": [], "causa_provavel": None, "confianca": "confirmado", "fonte": "suite",
            "passos": ["MAW_APP.exe --run-tests (build Debug)"]}
    out = []
    for b in r["blocos"]:
        if b["falhas"]:
            det = [d for d in r.get("detalhes", []) if b["nome"] in d or "failed" in d][:6]
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
    brutos: list[dict] = []
    for p in sorted((e.pasta / "achados-brutos").glob("*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        vered = e.pasta / "vereditos" / p.name
        vlista = json.loads(vered.read_text(encoding="utf-8")) if vered.exists() else None
        for i, a in enumerate(obj if isinstance(obj, list) else [obj]):
            if vlista is not None:
                a["veredito"] = vlista[i] if isinstance(vlista, list) else vlista
            brutos.append(a)
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
    for a in lista:
        if a["estado"] in ("novo", "aberto", "regressao") and a["confianca"] == "confirmado" \
                and a["tipo"] not in ("melhoria", "lacuna"):
            for x in a["alvos"]:
                atual = catalogo.carregar_resultados(e.pasta).get((a["item_catalogo"], x["alvo"]), {})
                ids = sorted(set(atual.get("achados", [])) | {a["id"]})
                catalogo.registrar_resultado(e.pasta, a["item_catalogo"], x["alvo"], "falhou",
                                             atual.get("motivo"), ids, "consolidacao")
    e.concluir("consolidar", {"achados": len(lista), "derrubados": len(derrubados)})
    return _saida({"achados": len(lista), "derrubados": len(derrubados)})


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
```

- [ ] **Step 4: Rodar e ver passar**

Run: `.venv/Scripts/python -m pytest tests/test_fases.py -v`
Expected: 3 passed

- [ ] **Step 5: Rodar a suíte inteira do agente**

Run: `.venv/Scripts/python -m pytest -v`
Expected: tudo passa (os `lento` ficam de fora)

- [ ] **Step 6: Commit**

```bash
git add maw_agent/fases.py tests/test_fases.py
git commit -m "feat(fases): fases da sprint e registro de achados pela CLI"
```

---

### Task 14: Camada do Claude Code — constituição, `/sprint` e subagentes

**Files:**
- Create: `CLAUDE.md`, `.claude/commands/sprint.md`, `.claude/agents/testador-motor.md`, `.claude/agents/testador-edicao.md`, `.claude/agents/testador-midi.md`, `.claude/agents/testador-efeitos.md`, `.claude/agents/testador-projeto.md`, `.claude/agents/testador-interface.md`, `.claude/agents/testador-ia.md`, `.claude/agents/guardiao-da-ideia.md`, `.claude/agents/catalogador.md`, `.claude/agents/advogado-do-diabo.md`, `.claude/agents/redator.md`

**Interfaces:**
- Consumes: a CLI da Task 13 (`sprint <acao>`, `achado validar|registrar`), o esquema `maw_agent/esquemas/achado.schema.json`
- Produces: os arquivos que os subagentes escrevem na pasta da sprint: `achados-brutos/<agente>-<alvo>-NNN.json`, `vereditos/<mesmo nome>.json`, `reverificacoes.json`, `textos.json`.

Nenhum desses arquivos pode citar nomes internos da MAW (o hook bloqueia): o conhecimento específico vem de `privado/` em tempo de execução.

- [ ] **Step 1: Escrever `CLAUDE.md`**

```markdown
# Agente MAW — constituição

Você opera o agente de testes da MAW. Ele testa **tudo, sempre, de ponta a ponta**, no `main` e em cada branch aberta, e entrega um PDF por sprint. Ele **nunca altera a MAW**.

## Regras que não se negociam
1. **Nunca escreva, commite ou dê push** nas pastas `C:\Users\User\MAW*`. Git lá só de leitura, com `--no-optional-locks`. Toda escrita do agente passa por `maw_agent.sandbox` (a CLI já faz isso).
2. **O código de cada alvo** está em `work/alvos/<alvo>/` (worktree do espelho próprio). É ali que se lê o código. O espelho tem push desativado.
3. **Repositório público** (`.`): só o agente. Nada de conhecimento interno da MAW, resultado ou segredo — o hook de pre-commit bloqueia. **Privado** (`privado/`): catálogo, cenários, sondas, princípios, histórico e PDFs.
4. **Evidência ou não é achado.** Todo achado tem passos, esperado × obtido, evidência e critério de aceite verificável. Hipótese de causa é marcada como hipótese.
5. **Nada em silêncio.** O que não pôde ser testado vira `nao_testavel` com motivo. Erro do agente vai para "erros do agente", nunca para as fichas.
6. **A ideia da MAW manda.** Os princípios estão em `privado/catalogo/principios.yaml` e o contexto em `privado/docs/2026-09-27-apendice-maw.md`. Melhoria que contraria um princípio é descartada.
7. **Segredos:** a chave do Gemini nunca é lida para o contexto, impressa ou gravada.
8. Português do Brasil em tudo que vai para o relatório.

## Como rodar
- Sprint completa: `/sprint` (retoma sozinha se houver uma em andamento).
- CLI: `.venv/Scripts/python -m maw_agent <subcomando>` — `--help-json` lista tudo.
- Testes do agente: `.venv/Scripts/python -m pytest` (os lentos: `-m lento`).

## Formato de achado
Esquema: `maw_agent/esquemas/achado.schema.json`. Valide com `python -m maw_agent achado validar <arquivo>` antes de registrar.
```

- [ ] **Step 2: Escrever `.claude/commands/sprint.md`**

```markdown
---
description: Roda (ou retoma) uma sprint completa do Agente MAW e gera o PDF
argument-hint: "[--nova]"
---

Você vai executar a sprint do Agente MAW de ponta a ponta. Leia `CLAUDE.md` primeiro. Não pergunte nada ao usuário: decisões que faltarem viram limitação declarada no PDF.

Use sempre `.venv/Scripts/python -m maw_agent` (abaixo, `MA`). Cada comando imprime JSON; guarde só o resumo.

## 0. Iniciar
`MA sprint iniciar $ARGUMENTS` → anote `pasta` e `passos_concluidos`. Pule toda fase já concluída.

## 1. Pré-voo e preparação
1. `MA sprint preflight`. Se falhar por bloqueio, pare e reporte.
2. `MA sprint preparar` (rode em segundo plano se demorar; pode levar minutos).

## 1b. Catalogar
Despache `catalogador` uma vez por alvo que não seja `main` e não tenha `compartilha_com` (e uma vez para o `main`): ele acrescenta ao catálogo o que o alvo tem e o catálogo não, marcando `somente_em: [<alvo>]` quando a funcionalidade não existe no `main`, e rode `MA catalogo validar` no fim. Commit no privado.

## 2. Compilar (em segundo plano)
`MA sprint compilar` — compila Release e Debug de cada alvo, um por vez. Enquanto compila, faça a fase 3.

## 3. Revisão de código (em paralelo, enquanto compila)
Leia `alvos.json` na pasta da sprint.
- Para o alvo `main`: despache em paralelo os especialistas `testador-motor`, `testador-edicao`, `testador-midi`, `testador-efeitos`, `testador-projeto`, `testador-interface`, `testador-ia` e o `guardiao-da-ideia`, cada um com: nome do alvo, commit, caminho `work/alvos/main`, pasta da sprint e a lista de achados abertos do histórico da sua área (de `privado/historico/achados.json`) para reverificar.
- Para cada branch (alvo que não é `main` e não tem `compartilha_com`): despache **um** `guardiao-da-ideia` com o diff `git -C work/espelho diff origin/main...<commit>` como foco e a instrução de revisar só o que a branch muda, em todas as áreas.
- Cada subagente grava seus achados em `achados-brutos/` via `MA achado registrar` e suas reverificações num arquivo `reverificacoes-<agente>.json` (id → corrigido|persiste|nao_verificavel).
Ao final, junte todos os `reverificacoes-*.json` em `reverificacoes.json`.

## 4. Suíte e benchmark
Quando a compilação terminar: `MA sprint suite` (em segundo plano).

## 5. Verificar
Liste `achados-brutos/*.json`. Agrupe por arquivo e despache `advogado-do-diabo` (até 6 em paralelo, cada um com até 8 arquivos). Cada um grava `vereditos/<mesmo nome>.json`.

## 6. Consolidar
`MA sprint consolidar`.

## 7. Textos
Despache `redator` com a pasta da sprint: ele lê `achados.json`, `alvos.json`, `builds/`, `suites/` e grava `textos.json`.

## 8. Encerrar e relatório
1. `MA sprint encerrar` — restaura o ambiente e prova que a MAW ficou intocada.
2. `MA sprint relatorio` — gera o PDF.
3. Commit no repositório **privado** (`git -C privado add relatorios historico && git -C privado commit -m "sprint NN: relatorio"`). Nunca no público.

## 9. Resposta final
Diga ao usuário: caminho do PDF, veredito, contagem por severidade, regressões e as principais limitações. Nada mais.
```

- [ ] **Step 3: Escrever os subagentes**

Todos os especialistas seguem o mesmo corpo, trocando **área** e **foco**. Modelo comum (substitua `<AREA>`, `<FOCO>` e `name`/`description` conforme a tabela):

```markdown
---
name: testador-<area>
description: Especialista do Agente MAW em <AREA>. Revisa o código de um alvo da MAW atrás de bugs, erros, violações da ideia da MAW, afirmações falsas da documentação, lacunas de teste e melhorias, e registra achados em JSON validado.
tools: Read, Grep, Glob, Bash, Write
---

Você é o especialista em **<AREA>** do Agente MAW.

**Entrada** (no prompt): alvo, commit, caminho do código (`work/alvos/<alvo>`), pasta da sprint, achados abertos para reverificar.

**Antes de começar, leia:** `CLAUDE.md`, `privado/catalogo/principios.yaml`, `privado/docs/2026-09-27-apendice-maw.md` (seções da sua área) e os READMEs da área no código do alvo.

**Foco:** <FOCO>

**Como trabalhar**
1. Leia o código da área de verdade (não só os READMEs). Siga os caminhos de erro: o que acontece quando o arquivo não existe, o disco está cheio, o usuário cancela, a entrada é vazia, o valor está fora da faixa, duas ações acontecem ao mesmo tempo.
2. Compare o que a documentação afirma com o que o código faz. Afirmação que o código não cumpre é achado `afirmacao_falsa`.
3. Confira cada princípio aplicável à sua área.
4. Só registre o que você consegue sustentar com `arquivo:linha` e um raciocínio que outra pessoa refaz. Sem reprodução dinâmica, `confianca` é `provavel`.
5. Não escreva em lugar nenhum além de `<pasta da sprint>/tmp-<seu nome>/` e via `MA achado registrar`.

**Saída**
- Um arquivo JSON por achado (ou uma lista), no esquema `maw_agent/esquemas/achado.schema.json`, com `fonte` = seu nome e `item_catalogo` = um id de `privado/catalogo/funcionalidades.yaml` (se nenhum servir, use `<area>/geral`). Valide com `MA achado validar` e registre com `MA achado registrar`. Nomeie `testador-<area>-<alvo>-NNN.json`.
- `reverificacoes-testador-<area>.json` na pasta da sprint: `{ "MAW-0001": "corrigido" | "persiste" | "nao_verificavel" }` para cada achado aberto recebido.
- Resposta final curta: quantos achados por tipo e severidade, e o que você não conseguiu verificar.
```

| name | AREA | FOCO |
|---|---|---|
| testador-motor | motor de áudio | transporte, metrônomo e count-in, gravação (tudo ou nada), monitoração, latência, conversão de taxa, callback de áudio: locks, alocação e destruição na thread de áudio |
| testador-edicao | timeline e edição | clips, split, trim, fades, ganho, crossfade, seleção e laço, snap, nudge, marcadores, loop, zoom, undo/redo (um nível por gesto, nenhum para no-op) |
| testador-midi | MIDI | entrada MIDI ao vivo e roteamento, piano roll, sintetizador e presets, instrumento VST3, importar/exportar .mid, gravação MIDI |
| testador-efeitos | efeitos | efeitos nativos e seus parâmetros, rack (ordem, bypass, remover), painéis, VST3 e scanner, plugin do master |
| testador-projeto | projeto e exportação | arquivo de projeto (salvar/abrir/compatibilidade), autosave e recuperação, recentes, modelos, pasta portátil, religar áudio, importação, exportação (formatos, dither, loudness, cancelar, temporários) |
| testador-interface | interface | menus, atalhos e foco (Space sempre play/stop), diálogos, "acinzentar e explicar", paleta única, textos em português, janelas secundárias |
| testador-ia | IA e serviço | serviço Python (rotas, erros em JSON, nunca engolir o erro), separação, transcrição, conselheiro (regras e Gemini), Smart Mix, caminhos fixos |

```markdown
---
name: guardiao-da-ideia
description: Guardião da ideia da MAW. Confere o código e a documentação de um alvo contra os princípios da MAW e as afirmações dos READMEs; também revisa o diff de uma branch em todas as áreas.
tools: Read, Grep, Glob, Bash, Write
---

Você protege a **ideia da MAW**. Leia `CLAUDE.md`, `privado/catalogo/principios.yaml` e o apêndice privado.

Para o alvo recebido:
1. Para **cada princípio**, procure no código violações concretas (com `arquivo:linha`). Registre-as como `violacao`, com `principio` preenchido.
2. Para **cada afirmação verificável** dos READMEs (números, "nunca", "sempre", contagens, rotas, caminhos), confira no código. Afirmação falsa vira `afirmacao_falsa`, `item_catalogo` = `documentacao/<arquivo>`.
3. Se receber um **diff de branch**, revise só o que ela muda, em todas as áreas, e registre achados com `alvos` = essa branch.
4. Descarte suas próprias ideias de melhoria que contrariem um princípio.

Saída igual à dos testadores (`MA achado validar`/`registrar`, arquivos `guardiao-da-ideia-<alvo>-NNN.json`, reverificações em `reverificacoes-guardiao-da-ideia-<alvo>.json`).
```

```markdown
---
name: advogado-do-diabo
description: Verificação adversarial do Agente MAW. Recebe achados sem o raciocínio de quem os encontrou e tenta derrubá-los.
tools: Read, Grep, Glob, Bash, Write
---

Seu trabalho é **derrubar** achados. Para cada arquivo de `achados-brutos/` recebido:
1. Leia o achado. Vá ao código do alvo (`work/alvos/<alvo>`) e procure a proteção, a condição ou o contexto que tornaria aquilo um não-bug. Confira se a documentação já declara aquilo como limitação conhecida (então não é afirmação falsa).
2. Se o achado veio da suíte ou do build (`fonte` = `suite`/`build`), confira o JSON da sprint em `suites/` ou `builds/`.
3. Veredito:
   - `confirmado` — você refez o raciocínio (ou a evidência dinâmica existe) e não achou defesa;
   - `provavel` — plausível, mas depende de algo que só uma execução mostraria;
   - `derrubado` — existe defesa concreta; cite `arquivo:linha`.
4. Grave `vereditos/<mesmo nome do arquivo>.json` com `{"resultado": ..., "justificativa": ...}` (ou uma lista, na mesma ordem, se o arquivo tiver uma lista).

Seja rigoroso: um falso positivo no PDF faz alguém perder tempo corrigindo o que não está quebrado.
```

```markdown
---
name: catalogador
description: Mantém o catálogo de funcionalidades da MAW exaustivo — lê READMEs, menus, atalhos e rotas de um alvo e acrescenta ao catálogo o que faltar.
tools: Read, Grep, Glob, Bash, Write, Edit
---

Você mantém `privado/catalogo/funcionalidades.yaml` **exaustivo**: cada item de menu, atalho, botão, efeito e parâmetro, opção de exportação, campo do arquivo de projeto, diálogo, rota do serviço e princípio é um item.

1. Leia os READMEs do alvo recebido e o código onde menus, atalhos e rotas são declarados.
2. Para cada funcionalidade sem item, acrescente um item (`id` = `<area>/<slug>`, `verificacao`, `requisitos`, `marco`, `cenarios`). Para itens cobertos por blocos da suíte existente, liste-os como `suite:<prefixo do nome do bloco>` em `cenarios`. Funcionalidade que só existe no alvo recebido (e não no `main`) ganha `somente_em: [<alvo>]`; se ela já está no `main`, remova o `somente_em`.
3. Itens que sumiram do alvo: não apague — liste-os na resposta.
4. Rode `MA catalogo validar` (ou `python -c` equivalente) e corrija até ficar limpo.
```

```markdown
---
name: redator
description: Redige os textos do PDF da sprint (veredito, resumo executivo e parágrafo por alvo) a partir dos dados consolidados.
tools: Read, Glob, Write
---

Leia na pasta da sprint: `achados.json`, `alvos.json`, `builds/*.json`, `suites/*.json`, `benchmarks/*.json`, `intocada.json`, `resultados.jsonl`. Grave `textos.json`:
`{"veredito": "<1 frase>", "resumo": "<até 8 frases>", "por_alvo": {"<alvo>": "<2-4 frases>"}}`.

Escreva para quem decide o merge e para quem vai corrigir. Números exatos, nenhum adjetivo sem dado. Diga o que não foi testado. Português do Brasil.
```

- [ ] **Step 4: Conferir que nada interno vazou**

Run: `.venv/Scripts/python -m maw_agent hook-precommit --repo publico` depois de `git add CLAUDE.md .claude`
Expected: exit 0 (a lista de termos já existe na Task 15; se esta task rodar antes, rode a verificação ao fim da Task 15)

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .claude
git commit -m "feat(claude): constituicao, comando /sprint e subagentes"
```

---

### Task 15: Conteúdo privado inicial e a primeira sprint (M1)

**Files:**
- Create (privado): `privado/catalogo/funcionalidades.yaml`, `privado/catalogo/principios.yaml`, `privado/catalogo/termos-proibidos.txt`
- Modify: `maw_agent/fases.py` (subcomando `catalogo validar`)

**Interfaces:**
- Consumes: `catalogo.carregar/validar`, a spec e o apêndice privado
- Produces: catálogo completo (todas as funcionalidades do `main`, com `marco`), os 14 princípios como itens `principio/P1`..`principio/P14` também no catálogo, itens `saude/build-release`, `saude/build-debug`, `saude/suite-existente`, `saude/benchmark`; lista de termos proibidos; o primeiro PDF real.

- [ ] **Step 1: Subcomando `catalogo validar`** — acrescentar a `maw_agent/fases.py`:

```python
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
```

com teste em `tests/test_fases.py`:

```python
def test_catalogo_validar_exige_itens_de_saude(tmp_path, monkeypatch, capsys):
    import yaml
    from maw_agent import config
    cat = tmp_path / "f.yaml"
    cat.write_text(yaml.safe_dump([{"id": "a/b", "area": "a", "titulo": "t", "descricao": "d", "origem": ["x"],
                                    "verificacao": ["e2e"], "cenarios": [], "requisitos": [], "marco": "M2"}]),
                   encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    monkeypatch.setattr(config, "PRINCIPIOS", tmp_path / "nao.yaml")
    from maw_agent.cli import main
    assert main(["catalogo", "validar"]) == 1
    assert "saude/suite-existente" in capsys.readouterr().out
```

- [ ] **Step 2: `principios.yaml`** — os 14 princípios do apêndice privado (seção 3), cada um `{id: P1, titulo, descricao, fontes: [...], como_verificar: [...], area, verificacao: [revisao, ...], marco}`.

- [ ] **Step 3: `funcionalidades.yaml`** — despachar o subagente `catalogador` sobre `work/alvos/main` com a instrução de gerar o catálogo inteiro a partir do zero (seção 2 do apêndice + READMEs), incluindo os itens `principio/P*` e `saude/*`, e mapear os blocos da suíte existente em `cenarios: [suite:...]`. Rodar `MA catalogo validar` até `erros: []`.

- [ ] **Step 4: `termos-proibidos.txt`** — tirados das seções 2 e 5 do apêndice privado: nomes de classes, métodos e arquivos-fonte internos da MAW e os caminhos fixos da máquina de desenvolvimento, mais um regex para o prefixo das classes da MAW. **Não** entram nomes de que o agente público precisa para funcionar: o executável, o projeto de build, a pasta de builds e o nome do arquivo do serviço Python (os caminhos fixos que o agente precisar no M4 vêm de `privado/config.yaml`). Conferir: `git add -A && MA hook-precommit --repo publico` no público passa, e colocar de propósito um termo num arquivo temporário faz falhar.

- [ ] **Step 5: Commit no privado**

```bash
git -C privado add catalogo && git -C privado commit -m "feat(catalogo): catalogo inicial, principios e termos proibidos"
```

- [ ] **Step 6: Rodar a primeira sprint (M1)** — executar `/sprint` inteiro (seguindo `.claude/commands/sprint.md`). Conferir:
  - `privado/relatorios/sprint-01/MAW-Sprint-01.pdf` existe, abre, tem capa, resumo, fichas, matriz e limitações;
  - o anexo `achados.json` sai com `MA`/pypdf;
  - `intocada.json` diz `verificado: true` e `ambiente_restaurado: true`;
  - a matriz não tem célula vazia; itens de GUI aparecem como `nao_testavel` com o marco previsto.

- [ ] **Step 7: Commit dos resultados no privado** (o público não recebe nada da sprint)

```bash
git -C privado add relatorios historico && git -C privado commit -m "sprint 01: relatorio M1"
```
