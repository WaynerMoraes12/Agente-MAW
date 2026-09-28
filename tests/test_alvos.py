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

def test_worktree_reaproveitada_troca_de_commit_limpa_sobras(mundo, tmp_path):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    lista, _ = alvos.descobrir_alvos(esp, [])
    x = next(a for a in lista if a.nome == "feature-x")
    raiz = tmp_path / "alvos"
    wt = alvos.criar_worktree(esp, x, raiz)

    # feature/x anda no "GitHub": novo commit acrescenta .gitignore com build/
    sh("checkout", "-q", "feature/x", cwd=mundo["origem"])
    (mundo["origem"] / ".gitignore").write_text("build/\n")
    sh("add", "-A", cwd=mundo["origem"]); sh("commit", "-qm", "gitignore", cwd=mundo["origem"])
    novo_commit = sh("rev-parse", "HEAD", cwd=mundo["origem"])
    sh("push", "-q", str(mundo["bare"]), "feature/x", cwd=mundo["origem"])
    alvos.garantir_espelho(esp, str(mundo["bare"]))

    lista2, _ = alvos.descobrir_alvos(esp, [])
    x2 = next(a for a in lista2 if a.nome == "feature-x")
    assert x2.commit == novo_commit

    # sobras no worktree existente: um arquivo não rastreado e um "build" ignorado
    (wt / "lixo.txt").write_text("lixo")
    (wt / "build").mkdir(exist_ok=True)
    (wt / "build" / "obj.o").write_text("obj")

    wt2 = alvos.criar_worktree(esp, x2, raiz)

    assert wt2 == wt
    assert sh("rev-parse", "HEAD", cwd=wt2) == novo_commit
    assert (wt2 / "Source" / "a.cpp").read_text() == "2"
    assert (wt2 / ".gitignore").exists()
    assert not (wt2 / "lixo.txt").exists()
    assert (wt2 / "build" / "obj.o").exists()


# ---------- git sem prompt e com timeout ----------

def test_git_sem_prompt_e_com_timeout(monkeypatch, tmp_path):
    vistos = []

    def run_falso(args, **kw):
        vistos.append((args, kw))
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(alvos.subprocess, "run", run_falso)
    alvos.git(["fetch", "-q", "origin"], tmp_path)
    alvos.git(["rev-parse", "HEAD"], tmp_path, leitura=True)
    (fetch_args, fetch_kw), (rp_args, rp_kw) = vistos
    for kw in (fetch_kw, rp_kw):
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0" and kw["env"]["GCM_INTERACTIVE"] == "Never"
    assert fetch_kw["timeout"] == 600 and rp_kw["timeout"] == 60


def test_git_timeout_vira_runtimeerror_claro(monkeypatch, tmp_path):
    def run_lento(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(alvos.subprocess, "run", run_lento)
    with pytest.raises(RuntimeError, match="não respondeu em 60 s"):
        alvos.git(["status"], tmp_path)


def test_clone_do_espelho_sem_prompt_e_com_timeout(monkeypatch, tmp_path):
    vistos = []

    def run_falso(args, **kw):
        vistos.append((args, kw))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(alvos.subprocess, "run", run_falso)
    alvos.garantir_espelho(tmp_path / "espelho", "https://exemplo.invalido/repo.git", buscar=False)
    clone_args, clone_kw = vistos[0]
    assert clone_args[:2] == ["git", "clone"] and clone_kw["timeout"] == 600
    assert clone_kw["env"]["GIT_TERMINAL_PROMPT"] == "0"


# ---------- worktree protegido ----------

def test_worktree_existente_numa_pasta_protegida_e_recusado(mundo, _sem_pastas_reais, monkeypatch):
    from maw_agent import sandbox
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    lista, _ = alvos.descobrir_alvos(esp, [])
    x = next(a for a in lista if a.nome == "feature-x")
    destino = _sem_pastas_reais / x.nome
    (destino / ".git").mkdir(parents=True)
    chamadas = []
    monkeypatch.setattr(alvos, "git", lambda *a, **k: chamadas.append(a) or "outro-commit")
    with pytest.raises(sandbox.EscritaProibida):
        alvos.criar_worktree(esp, x, _sem_pastas_reais)
    assert not any(args[0][0] in ("checkout", "clean") for args in chamadas)


# ---------- dois clones da mesma branch ----------

def test_dois_clones_divergentes_da_mesma_branch_nao_colidem(mundo):
    esp = alvos.garantir_espelho(mundo["tmp"] / "espelho", str(mundo["bare"]))
    a1 = _clone_do_usuario(mundo, "MAW_a1", "feature/x"); commit(a1, "Source/a.cpp", "a1", "a1")
    a2 = _clone_do_usuario(mundo, "MAW_A2", "feature/x"); commit(a2, "Source/a.cpp", "a2", "a2")
    sh("checkout", "-q", "feature/x", cwd=mundo["origem"]); commit(mundo["origem"], "Source/c.cpp", "gh", "gh")
    sh("push", "-q", str(mundo["bare"]), "feature/x", cwd=mundo["origem"])
    alvos.garantir_espelho(esp, str(mundo["bare"]))
    lista, _ = alvos.descobrir_alvos(esp, alvos.importar_clones_locais(esp, [a1, a2]))
    nomes = sorted(a.nome for a in lista if a.branch == "feature/x")
    assert nomes == ["feature-x", "feature-x-local-maw_a1", "feature-x-local-maw_a2"]


# ---------- prova de intocada ----------

def test_prova_de_pasta_sem_git_por_lista_de_arquivos(tmp_path):
    pasta = tmp_path / "MAW_sem_git"
    (pasta / "sub").mkdir(parents=True)
    (pasta / "sub" / "a.txt").write_text("1")
    antes = alvos.prova_intocada([pasta])
    assert antes["MAW_sem_git"]["tipo"] == "arquivos"
    assert antes["MAW_sem_git"]["arquivos"][0][:2] == ["sub/a.txt", 1]
    assert alvos.comparar_provas(antes, alvos.prova_intocada([pasta])) == []
    (pasta / "sub" / "b.txt").write_text("novo")
    difs = alvos.comparar_provas(antes, alvos.prova_intocada([pasta]))
    assert difs and "MAW_sem_git" in difs[0] and "sub/b.txt" in difs[0]


def test_prova_vazia_nao_e_verificada():
    assert alvos.comparar_provas({}, {}) == ["nenhuma pasta da MAW encontrada para provar"]
