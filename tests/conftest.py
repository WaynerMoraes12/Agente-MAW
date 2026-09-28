import json
import os
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def _sem_pastas_reais(monkeypatch, tmp_path):
    """Nenhum teste enxerga as pastas reais da MAW: protegidas = uma pasta falsa."""
    falsa = tmp_path / "_maw_do_usuario"
    falsa.mkdir()
    monkeypatch.setenv("MAW_AGENTE_PROTEGIDAS", str(falsa))
    return falsa


@pytest.fixture(autouse=True)
def _trava_do_appdata_em_tmp(monkeypatch, tmp_path):
    """Nenhum teste pega (nem espera) a trava real do %APPDATA% da MAW, que as sessões de verdade usam.
    O teste da MAW real (tests/test_gui_maw.py) volta para a trava real."""
    from maw_agent import trava_appdata

    monkeypatch.setattr(trava_appdata, "caminho_padrao", lambda: tmp_path / "_trava" / trava_appdata.ARQUIVO)


@pytest.fixture(autouse=True)
def _nunca_restaura_o_appdata_real(monkeypatch):
    """Rede de segurança: nenhum teste restaura um backup cuja origem é a pasta de configuração real da
    MAW (a restauração escreve na origem gravada no manifesto do backup, não em config.APPDATA_MAW).
    O teste da MAW real volta para a função original (`restaurar_pasta.__wrapped__`)."""
    from maw_agent import sandbox

    real = (Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "MAW").resolve()
    original = getattr(sandbox.restaurar_pasta, "__wrapped__", sandbox.restaurar_pasta)

    def guardada(backup):
        info = json.loads((Path(backup) / "manifesto.json").read_text(encoding="utf-8"))
        if Path(info["origem"]).resolve() == real:
            raise AssertionError(f"um teste tentou restaurar a pasta de configuração real da MAW ({real})")
        return original(backup)

    guardada.__wrapped__ = original
    monkeypatch.setattr(sandbox, "restaurar_pasta", guardada)
