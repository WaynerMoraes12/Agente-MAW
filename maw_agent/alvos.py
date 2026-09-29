"""Espelho próprio da MAW, descoberta de alvos, worktrees e prova de intocada."""
from __future__ import annotations
import hashlib
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config, sandbox

# o que da raiz é código (a documentação fica de fora); todo script .py da raiz também conta
CAMINHOS_DE_CODIGO = ("Source", "Builds", "JuceLibraryCode", "MAW_APP.jucer",
                      "requirements.txt", "pretrained_models")


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


# git nunca pede credencial na madrugada: sem prompt no terminal nem janela do gerenciador de credenciais
_ENV_GIT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
_LENTOS = ("fetch", "clone")


def _rodar_git(cmd: list[str], cwd: Path | None, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Roda git sem prompt e com limite de tempo (600 s para fetch/clone, 60 s para o resto)."""
    sub = next((c for c in cmd[1:] if not c.startswith("-")), "")
    timeout = timeout or (600 if sub in _LENTOS else 60)
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env={**os.environ, **_ENV_GIT}, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{' '.join(cmd[:4])} não respondeu em {timeout} s (em {cwd}); "
                           "rede ou credencial travada?") from None


def git(args: list[str], cwd: Path, leitura: bool = False) -> str:
    base = ["git", "--no-optional-locks"] if leitura else ["git"]
    p = _rodar_git(base + args, cwd)
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
        p = _rodar_git(["git", "clone", "-q", "--no-checkout", url, str(espelho)], None)
        if p.returncode != 0:
            raise RuntimeError(f"git clone falhou para {espelho}: {p.stderr.strip()}")
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
    return _rodar_git(["git", "merge-base", "--is-ancestor", a, b], espelho).returncode == 0


def _caminhos_de_codigo(espelho: Path, commit: str) -> list[str]:
    try:
        raiz = git(["ls-tree", "--name-only", commit], espelho).splitlines()
    except RuntimeError:
        raiz = []
    return list(CAMINHOS_DE_CODIGO) + sorted(n for n in raiz if n.endswith(".py") and n not in CAMINHOS_DE_CODIGO)


def _assinatura(espelho: Path, commit: str) -> str:
    h = hashlib.sha256()
    for c in _caminhos_de_codigo(espelho, commit):
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
                "origem": f"local:{nome_pasta}", "pasta": nome_pasta}
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
            # dois clones locais da mesma branch não podem colidir: o nome leva a pasta
            nome = (slug(branch) if len(grupo) == 1 or c["origem"] == "github"
                    else f"{slug(branch)}-local-{slug(c['pasta'])}")
            assin = _assinatura(espelho, c["commit"])
            lista.append(Alvo(nome, branch, c["ref"], c["commit"], c["origem"], assin, vistos.get(assin)))
            vistos.setdefault(assin, nome)
    return lista, avisos


def criar_worktree(espelho: Path, alvo: Alvo, raiz: Path = config.ALVOS_DIR) -> Path:
    destino = Path(raiz) / alvo.nome
    if (destino / ".git").exists():
        if git(["rev-parse", "HEAD"], destino) == alvo.commit:
            return destino
        sandbox.garantir_escrita(destino)  # checkout e clean escrevem: só fora das pastas da MAW
        git(["checkout", "-q", "--detach", "--force", alvo.commit], destino)
        git(["clean", "-fdq"], destino)  # sem -x: mantém o que o .gitignore ignora (ex.: build/)
        return destino
    sandbox.criar_pasta(Path(raiz))
    git(["worktree", "prune"], espelho)
    git(["worktree", "add", "-q", "--force", "--detach", str(destino), alvo.commit], espelho)
    return destino


def _lista_de_arquivos(pasta: Path) -> list[list]:
    """(caminho relativo, tamanho, mtime em ns) de cada arquivo: a prova de uma pasta sem git."""
    out = []
    for arq in sorted(pasta.rglob("*")):
        if arq.is_file():
            st = arq.stat()
            out.append([arq.relative_to(pasta).as_posix(), st.st_size, st.st_mtime_ns])
    return out


def prova_intocada(pastas: list[Path]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for pasta in pastas:
        pasta = Path(pasta)
        if not pasta.is_dir():
            continue
        if not (pasta / ".git").exists():
            out[pasta.name] = {"tipo": "arquivos", "arquivos": _lista_de_arquivos(pasta)}
            continue
        status = git(["status", "--porcelain=v1", "--untracked-files=all"], pasta, leitura=True)
        diff = subprocess.run(["git", "--no-optional-locks", "diff", "HEAD", "--binary"], cwd=pasta,
                              capture_output=True, env={**os.environ, **_ENV_GIT}, timeout=60).stdout
        out[pasta.name] = {
            "tipo": "git",
            "head": git(["rev-parse", "HEAD"], pasta, leitura=True),
            "branch": git(["branch", "--show-current"], pasta, leitura=True),
            "hash_status": hashlib.sha256(status.encode()).hexdigest(),
            "hash_diff": hashlib.sha256(diff).hexdigest(),
        }
    return out


def _diferenca_de_arquivos(a: list[list], d: list[list]) -> str:
    antes = {x[0]: tuple(x[1:]) for x in a}
    depois = {x[0]: tuple(x[1:]) for x in d}
    novos = sorted(set(depois) - set(antes))
    removidos = sorted(set(antes) - set(depois))
    alterados = sorted(k for k in set(antes) & set(depois) if antes[k] != depois[k])
    partes = []
    for rotulo, lista in (("novos", novos), ("alterados", alterados), ("removidos", removidos)):
        if lista:
            exemplos = ", ".join(lista[:5]) + (" …" if len(lista) > 5 else "")
            partes.append(f"{len(lista)} {rotulo}: {exemplos}")
    return "; ".join(partes)


def comparar_provas(antes: dict, depois: dict) -> list[str]:
    if not antes and not depois:
        return ["nenhuma pasta da MAW encontrada para provar"]
    difs = []
    for nome in sorted(set(antes) | set(depois)):
        a, d = antes.get(nome), depois.get(nome)
        if a == d:
            continue
        if a and d and a.get("tipo") == d.get("tipo") == "arquivos":
            difs.append(f"{nome}: arquivos mudaram ({_diferenca_de_arquivos(a['arquivos'], d['arquivos'])})")
        else:
            campos = sorted(k for k in set((a or {})) | set((d or {})) if (a or {}).get(k) != (d or {}).get(k))
            difs.append(f"{nome}: mudou ({', '.join(campos)})")
    return difs
