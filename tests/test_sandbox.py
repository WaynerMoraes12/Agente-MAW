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

def test_remover_junction_sem_apagar_destino(tmp_path):
    alvo = tmp_path / "alvo"
    alvo.mkdir()
    (alvo / "arquivo.txt").write_text("conteudo")

    ponte = tmp_path / "ponte"
    os.system(f'mklink /J "{ponte}" "{alvo}" >NUL')
    if not ponte.exists():
        pytest.skip("mklink /J indisponível")

    sandbox.remover(ponte)
    assert not ponte.exists(), "junction deve ser removida"
    assert alvo.exists(), "alvo deve continuar existindo"
    assert (alvo / "arquivo.txt").read_text() == "conteudo", "conteudo do alvo deve estar intacto"

def test_restauracao_falha_se_manifesto_corrompido(tmp_path):
    origem = tmp_path / "AppData" / "MAW"
    (origem / "config").mkdir(parents=True)
    (origem / "config" / "settings.json").write_text('{"a": 1}')
    bk = sandbox.backup_pasta(origem, tmp_path / "bk")

    # Corrompe o arquivo no backup depois do manifesto ser escrito
    (bk / "dados" / "config" / "settings.json").write_text('{"b": 2}')

    # Prepara uma nova origem para restauracao (remove o conteudo antigo)
    import shutil
    shutil.rmtree(origem)

    with pytest.raises(sandbox.RestauracaoFalhou):
        sandbox.restaurar_pasta(bk)


# ---------- I3: restauração segura, com tentativas ----------

@pytest.fixture
def sem_espera(monkeypatch):
    monkeypatch.setattr(sandbox, "ESPERAS", (0, 0, 0, 0))


def _origem_com_backup(tmp_path):
    origem = tmp_path / "AppData" / "MAW"
    (origem / "autosave").mkdir(parents=True)
    (origem / "MAW.settings").write_text("<a/>")
    (origem / "autosave" / "x.maw").write_bytes(b"\x00\x01")
    bk = sandbox.backup_pasta(origem, tmp_path / "bk")
    return origem, bk


def test_restauracao_nao_apaga_a_pasta_inteira(tmp_path, monkeypatch):
    origem, bk = _origem_com_backup(tmp_path)
    (origem / "MAW.settings").write_text("<mudou/>")
    (origem / "sobra").mkdir()
    (origem / "sobra" / "lixo.txt").write_text("lixo")
    (origem / "autosave" / "x.maw").unlink()
    apagadas = []
    real = sandbox.shutil.rmtree
    monkeypatch.setattr(sandbox.shutil, "rmtree", lambda p, *a, **k: (apagadas.append(Path(p)), real(p, *a, **k)))
    sandbox.restaurar_pasta(bk)
    assert Path(origem) not in apagadas  # a pasta nunca some inteira
    assert sandbox.manifesto(origem) == sandbox.manifesto(bk / "dados")
    assert not (origem / "sobra").exists()


def test_restauracao_so_regrava_o_que_mudou(tmp_path, monkeypatch):
    origem, bk = _origem_com_backup(tmp_path)
    (origem / "MAW.settings").write_text("<mudou/>")
    trocados = []
    real = sandbox.os.replace
    monkeypatch.setattr(sandbox.os, "replace", lambda a, b: (trocados.append(Path(b).name), real(a, b)))
    sandbox.restaurar_pasta(bk)
    assert trocados == ["MAW.settings"]


def test_restauracao_tenta_de_novo_contra_permissionerror(tmp_path, monkeypatch, sem_espera):
    origem, bk = _origem_com_backup(tmp_path)
    (origem / "MAW.settings").write_text("<mudou/>")
    real = sandbox.os.replace
    falhas = {"n": 0}

    def replace_instavel(a, b):
        if falhas["n"] < 2:
            falhas["n"] += 1
            raise PermissionError("arquivo em uso (simulado)")
        return real(a, b)

    monkeypatch.setattr(sandbox.os, "replace", replace_instavel)
    sandbox.restaurar_pasta(bk)
    assert falhas["n"] == 2
    assert (origem / "MAW.settings").read_text() == "<a/>"


def test_restauracao_oserror_persistente_vira_restauracaofalhou(tmp_path, monkeypatch, sem_espera):
    origem, bk = _origem_com_backup(tmp_path)
    (origem / "MAW.settings").write_text("<mudou/>")
    tentativas = {"n": 0}

    def sempre_em_uso(a, b):
        tentativas["n"] += 1
        raise PermissionError("arquivo em uso (simulado)")

    monkeypatch.setattr(sandbox.os, "replace", sempre_em_uso)
    with pytest.raises(sandbox.RestauracaoFalhou):
        sandbox.restaurar_pasta(bk)
    assert tentativas["n"] == 5
    assert not list(origem.rglob("*.tmp"))


def test_restauracao_outro_oserror_vira_restauracaofalhou(tmp_path, monkeypatch, sem_espera):
    origem, bk = _origem_com_backup(tmp_path)
    (origem / "novo.txt").write_text("sobra")

    def disco(*a, **k):
        raise OSError("erro de E/S (simulado)")

    monkeypatch.setattr(sandbox.os, "unlink", disco)
    monkeypatch.setattr(Path, "unlink", disco)
    with pytest.raises(sandbox.RestauracaoFalhou):
        sandbox.restaurar_pasta(bk)


def test_restauracao_com_arquivo_realmente_aberto_falha_como_restauracaofalhou(tmp_path, sem_espera):
    origem, bk = _origem_com_backup(tmp_path)
    (origem / "MAW.settings").write_text("<mudou/>")
    with open(origem / "MAW.settings", "rb"):  # no Windows, segura o arquivo contra troca
        with pytest.raises(sandbox.RestauracaoFalhou):
            sandbox.restaurar_pasta(bk)
    sandbox.restaurar_pasta(bk)  # solto o arquivo, a restauração passa
    assert (origem / "MAW.settings").read_text() == "<a/>"


def test_escrever_bytes_tenta_de_novo_e_desiste_limpando_o_temporario(tmp_path, monkeypatch, sem_espera):
    real = sandbox.os.replace
    n = {"n": 0}

    def instavel(a, b):
        n["n"] += 1
        if n["n"] < 3:
            raise PermissionError("em uso")
        return real(a, b)

    monkeypatch.setattr(sandbox.os, "replace", instavel)
    sandbox.escrever_texto(tmp_path / "a.txt", "ok")
    assert (tmp_path / "a.txt").read_text() == "ok" and n["n"] == 3

    n["n"] = -100
    with pytest.raises(PermissionError):
        sandbox.escrever_texto(tmp_path / "b.txt", "x")
    assert not list(tmp_path.glob("*.tmp"))


# ---------- I4: backup fora do repositório e apagado depois ----------

def test_backups_ficam_fora_da_arvore_do_repositorio():
    from maw_agent import config
    assert config.RAIZ not in config.BACKUPS.parents
    assert config.BACKUPS.parts[-2:] == ("AgenteMAW", "backups")


def test_ciclo_backup_restaurar_apagar_nao_deixa_copia(tmp_path):
    origem = tmp_path / "AppData" / "MAW"
    origem.mkdir(parents=True)
    (origem / "MAW.settings").write_text("<chave-ficticia/>")
    raiz = tmp_path / "backups"
    bk = sandbox.backup_pasta(origem, raiz)
    (origem / "MAW.settings").write_text("<sujo/>")
    sandbox.restaurar_e_descartar(bk)
    assert (origem / "MAW.settings").read_text() == "<chave-ficticia/>"
    assert not bk.exists()
    assert not [p for p in raiz.rglob("*") if p.is_file()]


def test_restaurar_e_descartar_mantem_o_backup_se_a_restauracao_falha(tmp_path, monkeypatch, sem_espera):
    origem, bk = _origem_com_backup(tmp_path)
    (origem / "MAW.settings").write_text("<mudou/>")
    monkeypatch.setattr(sandbox.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("em uso")))
    with pytest.raises(sandbox.RestauracaoFalhou):
        sandbox.restaurar_e_descartar(bk)
    assert (bk / "manifesto.json").exists()
