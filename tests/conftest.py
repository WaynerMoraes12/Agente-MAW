import pytest

@pytest.fixture(autouse=True)
def _sem_pastas_reais(monkeypatch, tmp_path):
    """Nenhum teste enxerga as pastas reais da MAW: protegidas = uma pasta falsa."""
    falsa = tmp_path / "_maw_do_usuario"
    falsa.mkdir()
    monkeypatch.setenv("MAW_AGENTE_PROTEGIDAS", str(falsa))
    return falsa
