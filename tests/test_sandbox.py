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
