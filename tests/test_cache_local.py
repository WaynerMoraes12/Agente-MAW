from maw_agent import cache_local


def test_cache_criado_durante_a_fase_e_apagado(tmp_path):
    alvo = tmp_path / "Local" / "MAW"
    lims = []
    with cache_local.temporario(lims.append, onde=alvo):
        (alvo / "pretrained_models" / "2stems").mkdir(parents=True)
    assert not alvo.exists() and len(lims) == 1 and "apagado" in lims[0]


def test_cache_que_ja_existia_fica_intocado(tmp_path):
    alvo = tmp_path / "Local" / "MAW"
    (alvo / "x").mkdir(parents=True)
    lims = []
    with cache_local.temporario(lims.append, onde=alvo):
        (alvo / "y").mkdir()
    assert (alvo / "x").exists() and (alvo / "y").exists() and lims == []


def test_sem_cache_nada_acontece(tmp_path):
    lims = []
    with cache_local.temporario(lims.append, onde=tmp_path / "nao"):
        pass
    assert lims == []
