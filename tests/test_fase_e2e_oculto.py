"""`MA e2e --oculto`: a fase se relança no desktop oculto (sem abrir janela nenhuma aqui: o
lançamento é simulado, e o de verdade é coberto por tests/test_desktop_oculto.py)."""
import argparse
import sys

import pytest

from maw_agent import cli, config, desktop_oculto, fase_e2e


@pytest.fixture(autouse=True)
def _isolado(tmp_path, monkeypatch):
    """Nada daqui enxerga sprint, bandeira ou cenário de verdade, nem restaura nada de verdade."""
    from maw_agent import fases

    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")
    monkeypatch.setattr(config, "BACKUPS", tmp_path / "local" / "backups")
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    monkeypatch.setattr(config, "PRIVADO", tmp_path / "privado")
    monkeypatch.setattr(fases, "restaurar_pendencias_ambiente",
                        lambda motivo, atual=None: {"restauradas": [], "descartadas": [], "adiadas": [], "erros": []})


def _args(linha: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    fase_e2e._cfg(p)
    return p.parse_args(linha)


def test_linha_de_comando_reconstruida_sem_oculto():
    args = _args(["--avulso", "--oculto", "--alvo", "main", "--cenario", "gui/*", "--cenario", "x",
                  "--arquivo", "a.py", "--limite", "6:00"])
    assert fase_e2e._argv_sem_oculto(args) == ["e2e", "--alvo", "main", "--cenario", "gui/*", "--cenario", "x",
                                               "--arquivo", "a.py", "--avulso", "--limite", "06:00"]
    assert fase_e2e._argv_sem_oculto(_args(["--oculto"])) == ["e2e"]


def test_oculto_relanca_no_desktop_oculto_e_repassa_o_codigo(monkeypatch, capsys):
    chamadas = []

    def falso(argv, *, cwd, env, log, timeout, eco=None, nome=desktop_oculto.NOME_PADRAO, relatorio=None):
        chamadas.append({"argv": argv, "cwd": cwd, "env": env, "log": log, "timeout": timeout})
        eco('{"execucoes": []}')
        return 7

    monkeypatch.setattr(desktop_oculto, "no_desktop_oculto", lambda: False)
    monkeypatch.setattr(desktop_oculto, "rodar_oculto", falso)
    monkeypatch.setattr(fase_e2e.e2e, "descobrir", lambda raiz: [])  # o pai só descobre (para o teto de tempo)
    monkeypatch.setattr(fase_e2e.e2e, "rodar_cenario", lambda *a, **k: pytest.fail("o pai não roda cenário nenhum"))
    monkeypatch.setenv("MAW_AGENTE_ENTRADA_REAL", "1")
    codigo = cli.main(["e2e", "--avulso", "--oculto", "--alvo", "main", "--cenario", "gui/abre-menu"])
    assert codigo == 7
    (c,) = chamadas
    assert c["argv"] == [sys.executable, "-m", "maw_agent", "e2e", "--alvo", "main", "--cenario", "gui/abre-menu",
                         "--avulso"]
    assert c["cwd"] == config.RAIZ
    assert c["env"]["MAW_AGENTE_ENTRADA_REAL"] == "0"
    saida = capsys.readouterr()
    assert '{"execucoes": []}' in saida.out
    assert "código 7" in saida.err


def test_dentro_do_oculto_nao_se_relanca(monkeypatch):
    monkeypatch.setattr(desktop_oculto, "no_desktop_oculto", lambda: True)
    monkeypatch.setattr(desktop_oculto, "rodar_oculto", lambda *a, **k: pytest.fail("relançou dentro do oculto"))
    rodou = []
    monkeypatch.setattr(fase_e2e.e2e, "descobrir", lambda raiz: [])
    monkeypatch.setattr(fase_e2e, "_rodar_avulso", lambda args, cenarios: rodou.append(args.alvo) or 0)
    assert cli.main(["e2e", "--avulso", "--oculto", "--alvo", "main"]) == 0
    assert rodou == ["main"]


def test_desktop_oculto_indisponivel_vira_erro_sem_rodar(monkeypatch, capsys):
    def falha(*a, **k):
        raise desktop_oculto.DesktopOcultoIndisponivel("não existe")

    monkeypatch.setattr(desktop_oculto, "no_desktop_oculto", lambda: False)
    monkeypatch.setattr(desktop_oculto, "rodar_oculto", falha)
    assert cli.main(["e2e", "--avulso", "--oculto"]) == 1
    assert "desktop oculto indisponível" in capsys.readouterr().out


def test_janela_no_desktop_do_usuario_vira_codigo_de_violacao(monkeypatch, capsys):
    def viola(*a, **k):
        raise desktop_oculto.JanelaNoDesktopDoUsuario("pid 1 'X' 'y'")

    monkeypatch.setattr(desktop_oculto, "no_desktop_oculto", lambda: False)
    monkeypatch.setattr(desktop_oculto, "rodar_oculto", viola)
    assert cli.main(["e2e", "--avulso", "--oculto"]) == desktop_oculto.CODIGO_VIOLACAO
    assert "pid 1" in capsys.readouterr().out


def test_ajuda_anuncia_oculto(capsys):
    with pytest.raises(SystemExit):
        cli.main(["e2e", "--help"])
    assert "--oculto" in capsys.readouterr().out


def _cenarios_falsos(monkeypatch, timeouts):
    from maw_agent import e2e

    cs = [e2e.Cenario(id=f"c{i}", func=lambda ctx: None, timeout=t) for i, t in enumerate(timeouts)]
    monkeypatch.setattr(fase_e2e.e2e, "descobrir", lambda raiz: cs)


def test_de_dia_relanca_no_oculto_mesmo_sem_a_opcao(monkeypatch):
    chamadas = []
    monkeypatch.setattr(desktop_oculto, "no_desktop_oculto", lambda: False)
    monkeypatch.setattr(desktop_oculto, "rodar_oculto", lambda argv, **k: chamadas.append(argv) or 0)
    _cenarios_falsos(monkeypatch, [])
    monkeypatch.delenv("MAW_AGENTE_ENTRADA_REAL", raising=False)
    assert cli.main(["e2e", "--avulso", "--alvo", "main"]) == 0
    assert chamadas and "--oculto" not in chamadas[0] and chamadas[0][-3:] == ["--alvo", "main", "--avulso"]


def test_com_entrada_real_e_sem_a_opcao_roda_aqui(monkeypatch):
    monkeypatch.setattr(desktop_oculto, "no_desktop_oculto", lambda: False)
    monkeypatch.setattr(desktop_oculto, "rodar_oculto", lambda *a, **k: pytest.fail("relançou"))
    monkeypatch.setenv("MAW_AGENTE_ENTRADA_REAL", "1")
    rodou = []
    monkeypatch.setattr(fase_e2e.e2e, "descobrir", lambda raiz: [])
    monkeypatch.setattr(fase_e2e, "_rodar_avulso", lambda args, cenarios: rodou.append(True) or 0)
    assert cli.main(["e2e", "--avulso"]) == 0 and rodou == [True]


def test_microfone_ausente_de_dia(monkeypatch):
    monkeypatch.delenv("MAW_AGENTE_MICROFONE", raising=False)
    assert "microfone" in fase_e2e._requisitos_extra()
    monkeypatch.setenv("MAW_AGENTE_MICROFONE", "1")
    assert "microfone" not in fase_e2e._requisitos_extra()


def test_teto_do_oculto_e_a_soma_dos_timeouts(monkeypatch):
    _cenarios_falsos(monkeypatch, [100, 200])
    args = _args(["--avulso", "--alvo", "main"])
    assert fase_e2e._tempo_oculto(args) == 100 + 200 + 2 * fase_e2e.FOLGA_POR_CENARIO + fase_e2e.MARGEM_OCULTO
    monkeypatch.setattr(fase_e2e.e2e, "descobrir", lambda raiz: (_ for _ in ()).throw(RuntimeError("x")))
    assert fase_e2e._tempo_oculto(args) == fase_e2e.TEMPO_OCULTO_MAX


def test_fuga_vira_aviso_e_filho_morto_restaura_o_ambiente(monkeypatch, capsys):
    from maw_agent import fases

    restauradas = []
    monkeypatch.setattr(fases, "restaurar_pendencias_ambiente",
                        lambda motivo, atual=None: restauradas.append(motivo) or
                        {"restauradas": [{}], "descartadas": [], "adiadas": [], "erros": []})
    monkeypatch.setattr(desktop_oculto, "no_desktop_oculto", lambda: False)
    _cenarios_falsos(monkeypatch, [10])

    def falso(argv, *, relatorio=None, **k):
        relatorio["fugas"] = [{"classe": "CabinetWClass", "processo": "explorer.exe", "quando": "x"}]
        return desktop_oculto.CODIGO_ESTOUROU

    monkeypatch.setattr(desktop_oculto, "rodar_oculto", falso)
    assert cli.main(["e2e", "--avulso", "--oculto"]) == desktop_oculto.CODIGO_ESTOUROU
    err = capsys.readouterr().err
    assert "possível janela fora do desktop oculto: CabinetWClass (explorer.exe)" in err
    assert restauradas and "1 restaurada" in err
    restauradas.clear()
    monkeypatch.setattr(desktop_oculto, "rodar_oculto", lambda argv, **k: 0)
    assert cli.main(["e2e", "--avulso", "--oculto"]) == 0 and restauradas == []  # saída normal: nada a restaurar
