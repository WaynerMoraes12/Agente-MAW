from maw_agent import bancada
import pytest


def test_carregar_sem_arquivo_devolve_vazio(tmp_path):
    assert bancada.carregar(tmp_path) == {}


def test_registrar_grava_uma_entrada(tmp_path):
    bancada.registrar(tmp_path, "abrir", "passou", "abriu maximizada", "main")
    assert bancada.carregar(tmp_path) == {"abrir": {"estado": "passou", "nota": "abriu maximizada", "alvo": "main"}}


def test_registrar_duas_vezes_mantem_o_pior_estado(tmp_path):
    bancada.registrar(tmp_path, "abrir", "passou", "abriu ok", "main")
    bancada.registrar(tmp_path, "abrir", "problema", "travou ao abrir", "main")
    d = bancada.carregar(tmp_path)["abrir"]
    assert d["estado"] == "problema"
    assert "abriu ok" in d["nota"] and "travou ao abrir" in d["nota"]


def test_pulei_e_pior_que_passou_mas_melhor_que_problema(tmp_path):
    bancada.registrar(tmp_path, "x", "passou", "n1", "main")
    bancada.registrar(tmp_path, "x", "pulei", "sem equipamento", "main")
    assert bancada.carregar(tmp_path)["x"]["estado"] == "pulei"
    bancada.registrar(tmp_path, "x", "problema", "quebrou", "main")
    assert bancada.carregar(tmp_path)["x"]["estado"] == "problema"


def test_estado_melhor_nao_sobrescreve_o_pior_mas_junta_nota(tmp_path):
    bancada.registrar(tmp_path, "x", "problema", "quebrou", "main")
    bancada.registrar(tmp_path, "x", "passou", "n2", "main")
    d = bancada.carregar(tmp_path)["x"]
    assert d["estado"] == "problema" and "quebrou" in d["nota"] and "n2" in d["nota"]


def test_alvo_acompanha_o_pior_estado(tmp_path):
    bancada.registrar(tmp_path, "x", "passou", "n1", "main")
    bancada.registrar(tmp_path, "x", "problema", "quebrou", "ramo-b")
    assert bancada.carregar(tmp_path)["x"]["alvo"] == "ramo-b"


def test_nota_repetida_nao_duplica(tmp_path):
    bancada.registrar(tmp_path, "x", "problema", "mesma nota", "main")
    bancada.registrar(tmp_path, "x", "problema", "mesma nota", "main")
    assert bancada.carregar(tmp_path)["x"]["nota"] == "mesma nota"


def test_estado_invalido_levanta(tmp_path):
    with pytest.raises(ValueError):
        bancada.registrar(tmp_path, "x", "talvez", "n", "main")


def test_varios_codigos_independentes(tmp_path):
    bancada.registrar(tmp_path, "a", "passou", "", "main")
    bancada.registrar(tmp_path, "b", "pulei", "sem guitarra", "main")
    d = bancada.carregar(tmp_path)
    assert set(d) == {"a", "b"} and d["a"]["estado"] == "passou" and d["b"]["estado"] == "pulei"
