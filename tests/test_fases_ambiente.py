"""Bandeiras appdata-sujo.json de todos os lugares (sprints, sessão avulsa do driver de GUI e
calibração fora de sprint): descoberta, restauração da mais antiga, descarte das mais novas e
adiamento com a MAW aberta. Tudo em tmp_path; nada toca a MAW real."""
import json
from pathlib import Path

import pytest

from maw_agent import config, estado, fases, sandbox, suite
from maw_agent.cli import main


def _ler(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture
def amb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    monkeypatch.setattr(config, "APPDATA_MAW", tmp_path / "Roaming" / "MAW")
    monkeypatch.setattr(config, "BACKUPS", tmp_path / "Local" / "AgenteMAW" / "backups")
    monkeypatch.setattr(sandbox, "ESPERAS", (0, 0, 0, 0))
    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    config.APPDATA_MAW.mkdir(parents=True)
    (config.APPDATA_MAW / "MAW.settings").write_text("<original/>", encoding="utf-8")
    return tmp_path


def _appdata() -> str:
    return (config.APPDATA_MAW / "MAW.settings").read_text(encoding="utf-8")


def _bandeira(pasta: Path, desde: str, sujar: str | None = None, extra: dict | None = None) -> Path:
    """Backup do %APPDATA%\\MAW como está + bandeira em `pasta`; depois suja com `sujar`."""
    bk = sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
    sandbox.escrever_json(pasta / "appdata-sujo.json", {"backup": str(bk), "desde": desde, **(extra or {})})
    if sujar is not None:
        (config.APPDATA_MAW / "MAW.settings").write_text(sujar, encoding="utf-8")
    return bk


def _avulsa() -> Path:
    return config.BACKUPS.parent


def _calibracao() -> Path:
    return config.WORK / "calibracao"


def test_pendencias_de_todos_os_lugares_da_mais_antiga_para_a_mais_nova(amb):
    e = estado.nova_sprint(config.RELATORIOS)
    bk_s = _bandeira(e.pasta, "2026-09-28T03:00:00")
    bk_a = _bandeira(_avulsa(), "2026-09-28T01:00:00", extra={"quem": "gui"})
    bk_c = _bandeira(_calibracao(), "2026-09-28T02:00:00")
    p = fases.pendencias_ambiente()
    assert [x["origem"] for x in p] == ["avulsa", "calibracao", "sprint"]
    assert [x["backup"] for x in p] == [bk_a, bk_c, bk_s]
    assert [x["flag"] for x in p] == [_avulsa() / "appdata-sujo.json", _calibracao() / "appdata-sujo.json",
                                      e.pasta / "appdata-sujo.json"]
    assert p[0]["desde"] == "2026-09-28T01:00:00" and isinstance(p[0]["flag"], Path)


def test_sem_bandeira_nada_a_fazer(amb):
    assert fases.pendencias_ambiente() == []
    assert fases.restaurar_pendencias_ambiente("teste") == {"restauradas": [], "descartadas": [],
                                                            "adiadas": [], "erros": []}


def test_duas_bandeiras_de_lugares_diferentes_a_mais_antiga_vence(amb):
    e = estado.nova_sprint(config.RELATORIOS)
    bk_a = _bandeira(_avulsa(), "2026-09-28T01:00:00", sujar="<sujo pela sessão avulsa/>", extra={"quem": "gui"})
    bk_s = _bandeira(e.pasta, "2026-09-28T03:00:00", sujar="<sujo pela suíte/>")  # backup de um estado já sujo
    r = fases.restaurar_pendencias_ambiente("sprint iniciar")
    assert _appdata() == "<original/>"  # o estado de antes do agente, não o do backup mais novo
    assert [x["origem"] for x in r["restauradas"]] == ["avulsa"] and r["restauradas"][0]["verificado"] is True
    assert [x["origem"] for x in r["descartadas"]] == ["sprint"]
    assert r["adiadas"] == [] and r["erros"] == []
    assert "avulsa" in r["descartadas"][0]["descartado"]
    assert not bk_a.exists() and not bk_s.exists()
    assert fases.pendencias_ambiente() == []
    reg = estado.carregar(e.pasta).restauracoes[-1]
    assert reg["verificado"] is True and reg["backup_apagado"] is True and "avulsa" in reg["descartado"]
    assert reg["motivo"] == "sprint iniciar"


def test_bandeira_da_calibracao_restaurada_e_registrada_na_sprint_atual(amb):
    e = estado.nova_sprint(config.RELATORIOS)
    bk = _bandeira(_calibracao(), "2026-09-28T02:00:00", sujar="<sujo pela calibração/>")
    r = fases.restaurar_pendencias_ambiente("sprint suite", atual=e)
    assert _appdata() == "<original/>" and not bk.exists()
    assert not (_calibracao() / "appdata-sujo.json").exists()
    [x] = r["restauradas"]
    assert x["origem"] == "calibracao" and x["backup_apagado"] is True
    regs = estado.carregar(e.pasta).restauracoes
    assert regs[-1]["origem"] == "calibracao" and regs[-1]["verificado"] is True
    assert fases._situacao_do_ambiente(estado.carregar(e.pasta)) == (True, None)


def test_maw_aberta_adia_tudo_sem_tocar_em_nada(amb, monkeypatch):
    e = estado.nova_sprint(config.RELATORIOS)
    bk_s = _bandeira(e.pasta, "2026-09-28T03:00:00")
    bk_a = _bandeira(_avulsa(), "2026-09-28T01:00:00")
    bk_c = _bandeira(_calibracao(), "2026-09-28T02:00:00", sujar="<sujo/>")
    monkeypatch.setattr(suite, "maw_aberta", lambda: True)
    r = fases.restaurar_pendencias_ambiente("sprint iniciar", atual=e)
    assert r["restauradas"] == [] and r["descartadas"] == [] and r["erros"] == []
    assert sorted(x["origem"] for x in r["adiadas"]) == ["avulsa", "calibracao", "sprint"]
    assert all(x["adiada"] and not x["verificado"] for x in r["adiadas"])
    assert _appdata() == "<sujo/>" and bk_s.exists() and bk_a.exists() and bk_c.exists()
    assert len(fases.pendencias_ambiente()) == 3
    s = estado.carregar(e.pasta)
    assert all(x.get("adiada") for x in s.restauracoes)
    assert fases._MOTIVO_ADIADA in _ler(e.pasta / "limitacoes-ambiente.json")
    # nada foi escrito fora da sprint (nem na pasta dos backups nem na da calibração)
    assert not (_avulsa() / "limitacoes-ambiente.json").exists()
    assert not (_calibracao() / "estado.json").exists()


def test_bandeira_mais_antiga_ilegivel_nao_deixa_restaurar_nem_descartar_as_outras(amb):
    e = estado.nova_sprint(config.RELATORIOS)
    sandbox.criar_pasta(_avulsa())
    (_avulsa() / "appdata-sujo.json").write_text("{quebrado", encoding="utf-8")
    bk = _bandeira(e.pasta, "2026-09-28T03:00:00", sujar="<sujo/>")
    r = fases.restaurar_pendencias_ambiente("sprint iniciar")
    assert r["restauradas"] == [] and r["descartadas"] == []
    [err] = r["erros"]
    assert err["origem"] == "avulsa" and "ilegível" in err["erro"]
    assert _appdata() == "<sujo/>" and bk.exists() and (e.pasta / "appdata-sujo.json").exists()


def test_restauracao_que_falha_mantem_bandeiras_e_backups(amb, monkeypatch):
    e = estado.nova_sprint(config.RELATORIOS)
    bk_a = _bandeira(_avulsa(), "2026-09-28T01:00:00")
    bk_s = _bandeira(e.pasta, "2026-09-28T03:00:00", sujar="<sujo/>")

    def falha(backup):
        raise sandbox.RestauracaoFalhou("arquivo em uso (simulado)")

    monkeypatch.setattr(sandbox, "restaurar_pasta", falha)
    r = fases.restaurar_pendencias_ambiente("sprint suite", atual=e)
    [err] = r["erros"]
    assert err["origem"] == "avulsa" and "em uso" in err["erro"]
    assert bk_a.exists() and bk_s.exists() and len(fases.pendencias_ambiente()) == 2
    ok, msg = fases._situacao_do_ambiente(estado.carregar(e.pasta))
    assert ok is False and "appdata-sujo.json" in msg and "em uso" in msg


def test_compativel_restaurar_pendentes_devolve_lista_por_ordem(amb):
    e1 = estado.nova_sprint(config.RELATORIOS)
    _bandeira(e1.pasta, "2026-09-28T01:00:00", sujar="<sujo 1/>")
    _bandeira(_calibracao(), "2026-09-28T02:00:00", sujar="<sujo 2/>")
    regs = fases._restaurar_pendentes()
    assert [(r["sprint"], r["origem"]) for r in regs] == [("sprint-01", "sprint"), (None, "calibracao")]
    assert regs[0]["verificado"] and "descartado" in regs[1]
    assert _appdata() == "<original/>"


# ---------- nas fases: iniciar, suíte e encerrar ----------

def test_iniciar_restaura_a_bandeira_avulsa(amb, monkeypatch, capsys):
    e = estado.nova_sprint(config.RELATORIOS)
    monkeypatch.setattr(estado, "em_andamento", lambda raiz=config.RELATORIOS: estado.carregar(e.pasta))
    bk = _bandeira(_avulsa(), "2026-09-28T01:00:00", sujar="<sujo pela sessão avulsa/>")
    assert main(["sprint", "iniciar", "--retomar"]) == 0
    saida = json.loads(capsys.readouterr().out)
    assert saida["restauracoes"][0]["origem"] == "avulsa" and saida["restauracoes"][0]["verificado"] is True
    assert _appdata() == "<original/>" and not bk.exists()
    assert estado.carregar(e.pasta).restauracoes[-1]["origem"] == "avulsa"  # aparece no PDF da sprint


def test_iniciar_com_a_maw_aberta_adia_a_bandeira_avulsa(amb, monkeypatch, capsys):
    e = estado.nova_sprint(config.RELATORIOS)
    monkeypatch.setattr(estado, "em_andamento", lambda raiz=config.RELATORIOS: estado.carregar(e.pasta))
    bk = _bandeira(_avulsa(), "2026-09-28T01:00:00", sujar="<sujo/>")
    monkeypatch.setattr(suite, "maw_aberta", lambda: True)
    assert main(["sprint", "iniciar"]) == 1
    saida = json.loads(capsys.readouterr().out)
    assert saida["restauracoes"][0]["adiada"] is True
    assert _appdata() == "<sujo/>" and bk.exists()


def test_suite_recusa_com_bandeira_avulsa_que_nao_restaura(amb, monkeypatch):
    e = estado.nova_sprint(config.RELATORIOS)
    sandbox.escrever_json(e.pasta / "alvos.json", {"alvos": [
        {"nome": "main", "branch": "main", "ref": "origin/main", "commit": "a" * 40, "origem": "github",
         "assinatura": "s", "compartilha_com": None}], "avisos": []})
    monkeypatch.setattr(fases, "_sprint_atual", lambda: estado.carregar(e.pasta))
    monkeypatch.setattr(config, "CATALOGO", amb / "nao-existe.yaml")
    sandbox.criar_pasta(_avulsa())
    sandbox.escrever_json(_avulsa() / "appdata-sujo.json", {"backup": str(config.BACKUPS / "sumiu"),
                                                             "desde": "2026-09-28T01:00:00"})
    chamou = []
    monkeypatch.setattr(sandbox, "backup_pasta", lambda *a: chamou.append(a))
    assert fases._suite(type("A", (), {"alvo": None})()) == 1
    assert chamou == []  # nunca faz backup de um ambiente sujo
    r = {k: v for k, v in __import__("maw_agent.catalogo").catalogo.carregar_resultados(e.pasta).items()}
    assert "pendente" in r[("saude/suite-existente", "main")]["motivo"]
    assert "appdata-sujo.json" in r[("saude/suite-existente", "main")]["motivo"]


def test_encerrar_nao_diz_ambiente_restaurado_com_bandeira_pendente_em_outro_lugar(amb, monkeypatch):
    e = estado.nova_sprint(config.RELATORIOS)
    monkeypatch.setattr(fases, "_sprint_atual", lambda: estado.carregar(e.pasta))
    sandbox.escrever_json(e.pasta / "prova-antes.json", {})
    monkeypatch.setattr(fases.alvos, "prova_intocada", lambda pastas: {})
    _bandeira(_calibracao(), "2026-09-28T02:00:00", sujar="<sujo/>")
    monkeypatch.setattr(suite, "maw_aberta", lambda: True)  # não dá para restaurar agora
    fases._encerrar(None)
    info = _ler(e.pasta / "intocada.json")
    assert info["ambiente_restaurado"] is False
    assert "calibracao" in info["erro_restauracao"] and "appdata-sujo.json" in info["erro_restauracao"]


def test_encerrar_restaura_bandeira_de_outro_lugar(amb, monkeypatch):
    e = estado.nova_sprint(config.RELATORIOS)
    monkeypatch.setattr(fases, "_sprint_atual", lambda: estado.carregar(e.pasta))
    sandbox.escrever_json(e.pasta / "prova-antes.json", {"MAW": {"tipo": "git", "head": "h"}})
    monkeypatch.setattr(fases.alvos, "prova_intocada", lambda pastas: {"MAW": {"tipo": "git", "head": "h"}})
    _bandeira(_avulsa(), "2026-09-28T01:00:00", sujar="<sujo/>")
    assert fases._encerrar(None) == 0
    assert _appdata() == "<original/>" and _ler(e.pasta / "intocada.json")["ambiente_restaurado"] is True


def test_sem_atual_o_registro_de_fora_de_sprint_vai_para_a_sprint_em_andamento(amb):
    e = estado.nova_sprint(config.RELATORIOS)
    _bandeira(_avulsa(), "2026-09-28T01:00:00", sujar="<sujo/>")
    r = fases.restaurar_pendencias_ambiente("gui")  # quem chama (driver de GUI) não tem a sprint em mãos
    assert r["restauradas"][0]["origem"] == "avulsa" and _appdata() == "<original/>"
    reg = estado.carregar(e.pasta).restauracoes[-1]
    assert reg["origem"] == "avulsa" and reg["motivo"] == "gui" and reg["verificado"] is True


def test_sem_sprint_em_andamento_nada_e_gravado_fora_do_lugar(amb):
    _bandeira(_calibracao(), "2026-09-28T02:00:00", sujar="<sujo/>")
    r = fases.restaurar_pendencias_ambiente("calibracao")
    assert r["restauradas"][0]["origem"] == "calibracao" and _appdata() == "<original/>"
    assert not config.RELATORIOS.exists() or not list(config.RELATORIOS.glob("sprint-*"))
    assert not (_calibracao() / "estado.json").exists()


class _OutraSessao:
    """Segura a trava do %APPDATA% da MAW numa thread à parte, como outra sessão do agente."""

    def __enter__(self):
        import threading

        from maw_agent import trava_appdata

        self._pegou, self._soltar = threading.Event(), threading.Event()

        def segura():
            posse = trava_appdata.adquirir(0)
            self._pegou.set()
            self._soltar.wait(30)
            if posse is not None:
                posse.liberar()

        self._t = threading.Thread(target=segura)
        self._t.start()
        assert self._pegou.wait(10)
        return self

    def __exit__(self, *_):
        self._soltar.set()
        self._t.join(10)


def test_trava_ocupada_adia_tudo_sem_tocar_em_nada(amb, monkeypatch):
    """Outra sessão do agente com a trava do %APPDATA% da MAW: nada é restaurado nem apagado."""
    monkeypatch.setattr(fases, "ESPERA_TRAVA_APPDATA", 0.2)
    e = estado.nova_sprint(config.RELATORIOS)
    bk = _bandeira(_avulsa(), "2026-09-28T01:00:00", sujar="<sujo/>")
    with _OutraSessao():
        r = fases.restaurar_pendencias_ambiente("teste", e)
    assert [x["adiada"] for x in r["adiadas"]] == [True] and not r["restauradas"]
    assert "outra sessão do agente" in r["adiadas"][0]["erro"]
    assert _appdata() == "<sujo/>" and bk.exists() and (_avulsa() / "appdata-sujo.json").exists()
    r = fases.restaurar_pendencias_ambiente("teste", e)  # livre: restaura
    assert r["restauradas"] and _appdata() == "<original/>" and not bk.exists()


def test_quem_ja_tem_a_trava_restaura_na_hora(amb):
    """Reentrante: a sessão de GUI restaura pendências com a trava na mão (mesma thread)."""
    from maw_agent import trava_appdata

    _bandeira(_avulsa(), "2026-09-28T01:00:00", sujar="<sujo/>")
    with trava_appdata.segurar(0):
        r = fases.restaurar_pendencias_ambiente("teste")
    assert r["restauradas"] and _appdata() == "<original/>"


def test_suite_com_a_trava_ocupada_nao_roda(amb, monkeypatch):
    monkeypatch.setattr(fases, "ESPERA_TRAVA_APPDATA", 0.2)
    e = estado.nova_sprint(config.RELATORIOS)
    sandbox.escrever_json(e.pasta / "alvos.json", {"alvos": [
        {"nome": "main", "branch": "main", "ref": "origin/main", "commit": "a" * 40, "origem": "github",
         "assinatura": "s", "compartilha_com": None}], "avisos": []})
    monkeypatch.setattr(fases, "_sprint_atual", lambda: estado.carregar(e.pasta))
    monkeypatch.setattr(config, "CATALOGO", amb / "nao-existe.yaml")
    chamou = []
    monkeypatch.setattr(sandbox, "backup_pasta", lambda *a: chamou.append(a))
    with _OutraSessao():
        assert fases._suite(type("A", (), {"alvo": None})()) == 1
    assert chamou == []
    r = __import__("maw_agent.catalogo").catalogo.carregar_resultados(e.pasta)
    assert "trava" in r[("saude/suite-existente", "main")]["motivo"]


def test_restaurar_ambiente_adia_com_uma_maw_aberta(tmp_path, monkeypatch):
    """A MAW do usuário aberta no fim da suíte/calibração: nada é restaurado nem apagado (a bandeira e o
    único backup ficam para a próxima retomada), e isso vira limitação."""
    from maw_agent import estado, fases, sandbox, suite
    pasta = tmp_path / "sprint-99"
    pasta.mkdir()
    e = estado.Estado(pasta, 99)
    backup = tmp_path / "backup-x"
    backup.mkdir()
    (pasta / "appdata-sujo.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(suite, "maw_aberta", lambda: True)
    restaurou = []
    monkeypatch.setattr(sandbox, "restaurar_pasta", lambda b: restaurou.append(b))
    reg = fases._restaurar_ambiente(e, backup, "suite")
    assert reg["adiada"] is True and reg["verificado"] is False and restaurou == []
    assert (pasta / "appdata-sujo.json").exists() and backup.exists()
