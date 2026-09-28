import json
import subprocess
from pathlib import Path

import pytest

from maw_agent import calibracao, sandbox


# ---------- defeitos ----------

def _defeito(**kw):
    base = {"id": "d1", "arquivo": "Source/a.cpp", "buscar": "x = 1;", "trocar": "x = 2;",
            "origem": "plantado", "descricao": "d", "esperado": ["Bloco A"]}
    base.update(kw)
    return calibracao.Defeito(**base)


def test_carregar_defeitos_valida_campos_e_ids(tmp_path):
    p = tmp_path / "defeitos.yaml"
    p.write_text("- id: a\n  arquivo: Source/a.cpp\n  buscar: 'x'\n  trocar: 'y'\n  esperado: [Bloco A]\n"
                 "- id: b\n  arquivo: Source/b.cpp\n  buscar: 'x'\n  trocar: 'y'\n  esperado: [Bloco B → sub]\n",
                 encoding="utf-8")
    ds = calibracao.carregar_defeitos(p)
    assert [d.id for d in ds] == ["a", "b"] and ds[0].esperado == ["Bloco A"] and ds[1].esperado == ["Bloco B → sub"]
    p.write_text("- id: a\n  arquivo: Source/a.cpp\n  buscar: 'x'\n  trocar: 'y'\n  esperado: [A]\n"
                 "- id: a\n  arquivo: Source/a.cpp\n  buscar: 'x'\n  trocar: 'z'\n  esperado: [A]\n", encoding="utf-8")
    with pytest.raises(calibracao.DefeitoInvalido, match="duplicado"):
        calibracao.carregar_defeitos(p)
    p.write_text("- id: a\n  arquivo: Source/a.cpp\n  buscar: 'x'\n  esperado: [A]\n", encoding="utf-8")
    with pytest.raises(calibracao.DefeitoInvalido, match="trocar"):
        calibracao.carregar_defeitos(p)
    p.write_text("- id: a\n  arquivo: Source/a.cpp\n  buscar: 'x'\n  trocar: 'x'\n  esperado: [A]\n", encoding="utf-8")
    with pytest.raises(calibracao.DefeitoInvalido, match="igual"):
        calibracao.carregar_defeitos(p)
    # sem bloco esperado não há como contar a detecção: recusado
    p.write_text("- id: a\n  arquivo: Source/a.cpp\n  buscar: 'x'\n  trocar: 'y'\n", encoding="utf-8")
    with pytest.raises(calibracao.DefeitoInvalido, match="esperado"):
        calibracao.carregar_defeitos(p)


def test_aplicar_troca_ocorrencia_unica(tmp_path):
    (tmp_path / "Source").mkdir()
    (tmp_path / "Source" / "a.cpp").write_bytes(b"int x = 1;\nint y = 3;\n")
    calibracao.aplicar_defeito(tmp_path, _defeito())
    assert (tmp_path / "Source" / "a.cpp").read_bytes() == b"int x = 2;\nint y = 3;\n"


def test_aplicar_recusa_texto_ausente_ou_repetido_sem_mexer_no_arquivo(tmp_path):
    (tmp_path / "Source").mkdir()
    arq = tmp_path / "Source" / "a.cpp"
    arq.write_bytes(b"int z = 1;\n")
    with pytest.raises(calibracao.DefeitoInvalido, match="não encontrado"):
        calibracao.aplicar_defeito(tmp_path, _defeito())
    arq.write_bytes(b"x = 1; x = 1;\n")
    with pytest.raises(calibracao.DefeitoInvalido, match="2 vezes"):
        calibracao.aplicar_defeito(tmp_path, _defeito())
    assert arq.read_bytes() == b"x = 1; x = 1;\n"
    with pytest.raises(calibracao.DefeitoInvalido, match="não existe"):
        calibracao.aplicar_defeito(tmp_path, _defeito(arquivo="Source/nada.cpp"))


def test_aplicar_multilinha_em_arquivo_crlf_mantem_crlf(tmp_path):
    (tmp_path / "Source").mkdir()
    arq = tmp_path / "Source" / "a.cpp"
    arq.write_bytes(b"if (p == nullptr)\r\n    return false;\r\nfim();\r\n")
    calibracao.aplicar_defeito(tmp_path, _defeito(buscar="if (p == nullptr)\n    return false;",
                                                  trocar="if (p == nullptr)\n    return true;"))
    assert arq.read_bytes() == b"if (p == nullptr)\r\n    return true;\r\nfim();\r\n"


def test_aplicar_respeita_a_sandbox(_sem_pastas_reais):
    (_sem_pastas_reais / "Source").mkdir()
    (_sem_pastas_reais / "Source" / "a.cpp").write_bytes(b"x = 1;\n")
    with pytest.raises(sandbox.EscritaProibida):
        calibracao.aplicar_defeito(_sem_pastas_reais, _defeito())


# ---------- avaliação ----------

BASE = [("Bloco A", "x"), ("Bloco A", "novo"), ("Bloco B", "y"), ("Bloco V", "velho"), ("Bloco Z", "z")]


def _suite(*falhas, incoerencias=()):
    """Suíte com os blocos de BASE passando e os de `falhas` falhando (um de fora de BASE entra só aqui)."""
    nomes = BASE + [f for f in falhas if f not in BASE]
    blocos = [{"nome": n, "sub": s, "ok": 1, "falhas": int((n, s) in falhas)} for n, s in nomes]
    return {"blocos": blocos, "incoerencias": list(incoerencias), "passou": not falhas and not incoerencias}


def test_avaliar_conta_so_bloco_esperado_que_passava_no_controle():
    controle = _suite(("Bloco V", "velho"))
    r = calibracao.avaliar(_suite(("Bloco V", "velho"), ("Bloco A", "novo")), controle, ["Bloco A"])
    assert r["detectado"] is True and r["detectado_so_por_outro_bloco"] is False
    assert r["blocos_esperados_que_falharam"] == ["Bloco A → novo"] and r["outros_blocos_que_falharam"] == []
    assert r["blocos_que_falharam"] == ["Bloco A → novo"]
    # o esperado já falhava no controle: não prova nada
    r = calibracao.avaliar(_suite(("Bloco V", "velho")), controle, ["Bloco V"])
    assert r["detectado"] is False and r["detectado_so_por_outro_bloco"] is False and r["blocos_que_falharam"] == []


def test_avaliar_quebrar_so_outro_bloco_nao_e_deteccao():
    r = calibracao.avaliar(_suite(("Bloco B", "y")), _suite(), ["Bloco A"])
    assert r["detectado"] is False and r["detectado_so_por_outro_bloco"] is True
    assert r["outros_blocos_que_falharam"] == ["Bloco B → y"] and r["blocos_esperados_que_falharam"] == []
    # esperado por "nome → sub": outra sub do mesmo bloco não conta
    r = calibracao.avaliar(_suite(("Bloco A", "x")), _suite(), ["Bloco A → novo"])
    assert r["detectado"] is False and r["detectado_so_por_outro_bloco"] is True
    r = calibracao.avaliar(_suite(("Bloco A", "novo")), _suite(), ["Bloco A → novo"])
    assert r["detectado"] is True


def test_avaliar_bloco_esperado_ausente_do_controle_nao_conta():
    # o bloco esperado só existe na rodada com defeito: não passou no controle, então não prova
    r = calibracao.avaliar(_suite(("Bloco N", "so no defeito")), _suite(), ["Bloco N"])
    assert r["detectado"] is False and r["detectado_so_por_outro_bloco"] is True


def test_avaliar_incoerencia_nova_e_so_por_outro_bloco():
    r = calibracao.avaliar(_suite(incoerencias=["totais ausentes: a execução pode ter morrido antes do fim"]),
                           _suite(), ["Bloco A"])
    assert r["detectado"] is False and r["detectado_so_por_outro_bloco"] is True and r["incoerencias"]


def test_taxa_usa_so_a_deteccao_pelo_bloco_esperado():
    res = {"a": {"detectado": True, "detectado_so_por_outro_bloco": False, "erro": None},
           "b": {"detectado": False, "detectado_so_por_outro_bloco": True, "erro": None},
           "c": {"detectado": False, "detectado_so_por_outro_bloco": False, "erro": None},
           "d": {"detectado": False, "detectado_so_por_outro_bloco": False, "erro": "não compilou"}}
    t = calibracao.taxa(res)
    assert t == {"total": 4, "validos": 3, "detectados": 1, "detectados_so_por_outro_bloco": 1, "erros": 1,
                 "taxa": round(1 / 3, 4)}
    assert calibracao.taxa({})["taxa"] is None


# ---------- orquestração ----------

class OpsFalsas:
    """Worktree = pasta com Source/a.cpp; compilar olha o texto; a suíte falha o Bloco A → x se
    x = 2 e o Bloco B → y se y = 3."""

    def __init__(self, tmp: Path, controle_compila=True, abortar_em=None, preparar_falha_em=None):
        self.tmp, self.controle_compila, self.abortar_em = tmp, controle_compila, abortar_em
        self.preparar_falha_em = preparar_falha_em
        self.preparadas, self.limpas = [], []

    def preparar(self, nome):
        wt = self.tmp / nome
        (wt / "Source").mkdir(parents=True, exist_ok=True)
        if nome == self.preparar_falha_em:  # worktree pela metade: pasta criada, checkout caiu
            raise RuntimeError("git worktree add falhou no meio")
        (wt / "Source" / "a.cpp").write_text("x = 1;\ny = 1;\n", encoding="utf-8")
        self.preparadas.append(nome)
        return wt

    def compilar(self, nome, wt):
        texto = (wt / "Source" / "a.cpp").read_text(encoding="utf-8")
        if (nome == calibracao.CONTROLE and not self.controle_compila) or "erro de sintaxe" in texto:
            return {"ok": False, "exe": None, "erros": ["a.cpp(1): error C2143: sintaxe"]}
        return {"ok": True, "exe": str(wt / "app.exe"), "erros": []}

    def rodar(self, nome, exe):
        if nome == self.abortar_em:
            raise calibracao.Abortar("ambiente não restaurado")
        texto = (Path(exe).parent / "Source" / "a.cpp").read_text(encoding="utf-8")
        falhas = [f for f, marca in ((("Bloco A", "x"), "x = 2"), (("Bloco B", "y"), "y = 3")) if marca in texto]
        return _suite(*falhas)

    def limpar(self, nome):
        self.limpas.append(nome)
        wt = self.tmp / nome
        if wt.exists():
            sandbox.remover(wt)


def _defeitos():
    return [_defeito(id="pega"), _defeito(id="escapa", buscar="y = 1;", trocar="y = 3;"),
            _defeito(id="sumiu", buscar="w = 1;", trocar="w = 2;"),
            _defeito(id="quebra", buscar="y = 1;", trocar="erro de sintaxe")]


def test_calibrar_controle_depois_cada_defeito_e_sempre_limpa(tmp_path):
    ops = OpsFalsas(tmp_path)
    res, controle = calibracao.calibrar(_defeitos(), ops)
    assert controle["erro"] is None
    assert res["pega"]["detectado"] is True and res["pega"]["blocos_esperados_que_falharam"] == ["Bloco A → x"]
    # "escapa" só derruba o Bloco B, que ele não espera: fora da taxa, contado à parte
    assert res["escapa"]["detectado"] is False and res["escapa"]["detectado_so_por_outro_bloco"] is True
    assert res["escapa"]["outros_blocos_que_falharam"] == ["Bloco B → y"] and res["escapa"]["erro"] is None
    assert res["sumiu"]["erro"] and "não encontrado" in res["sumiu"]["erro"]
    assert res["quebra"]["erro"] and "não compilou" in res["quebra"]["erro"]
    assert all(isinstance(r["segundos"], (int, float)) for r in res.values())
    assert ops.preparadas[0] == calibracao.CONTROLE
    assert sorted(ops.limpas) == sorted(ops.preparadas)
    t = calibracao.taxa(res)
    assert t["detectados"] == 1 and t["detectados_so_por_outro_bloco"] == 1 and t["taxa"] == 0.5


def test_calibrar_preparar_que_falha_limpa_a_sobra_e_segue(tmp_path):
    ops = OpsFalsas(tmp_path, preparar_falha_em="escapa")
    res, _ = calibracao.calibrar(_defeitos(), ops)
    assert "worktree add falhou" in res["escapa"]["erro"] and res["escapa"]["detectado"] is False
    assert "escapa" in ops.limpas and not (tmp_path / "escapa").exists()
    assert res["pega"]["detectado"] is True and "não compilou" in res["quebra"]["erro"]  # os outros seguiram


def test_calibrar_limpeza_que_falha_fica_registrada(tmp_path):
    ops = OpsFalsas(tmp_path)

    def limpar_preso(nome):
        ops.limpas.append(nome)
        if nome == "pega":
            raise OSError("pdb preso")

    ops.limpar = limpar_preso
    res, _ = calibracao.calibrar(_defeitos()[:1], ops)
    assert "pdb preso" in res["pega"]["limpeza"] and res["pega"]["detectado"] is True


def test_calibrar_sem_controle_valido_nao_mede_defeito(tmp_path):
    ops = OpsFalsas(tmp_path, controle_compila=False)
    res, controle = calibracao.calibrar(_defeitos(), ops)
    assert controle["erro"] and all(r["erro"] and not r["detectado"] for r in res.values())
    assert ops.preparadas == [calibracao.CONTROLE] and ops.limpas == [calibracao.CONTROLE]


def test_calibrar_controle_que_nao_prepara_limpa_a_sobra(tmp_path):
    ops = OpsFalsas(tmp_path, preparar_falha_em=calibracao.CONTROLE)
    res, controle = calibracao.calibrar(_defeitos(), ops)
    assert "worktree add falhou" in controle["erro"] and all(r["erro"] for r in res.values())
    assert ops.limpas == [calibracao.CONTROLE] and not (tmp_path / calibracao.CONTROLE).exists()


def test_calibrar_abortar_interrompe_o_resto(tmp_path):
    ops = OpsFalsas(tmp_path, abortar_em="escapa")
    res, _ = calibracao.calibrar(_defeitos(), ops)
    assert res["pega"]["erro"] is None
    assert "interrompida" in res["escapa"]["erro"] and "interrompida" in res["sumiu"]["erro"]
    assert "escapa" in ops.limpas and "sumiu" not in ops.preparadas


# ---------- worktrees de verdade (git) ----------

def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_worktree_de_calibracao_cria_e_remove(tmp_path):
    repo = tmp_path / "espelho"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo); _git("config", "user.name", "t", cwd=repo)
    (repo / "a.cpp").write_text("x = 1;\n")
    _git("add", "-A", cwd=repo); _git("commit", "-qm", "c1", cwd=repo)
    commit = _git("rev-parse", "HEAD", cwd=repo)
    destino = tmp_path / "calibracao" / "d1"
    wt = calibracao.criar_worktree(repo, commit, destino)
    assert (wt / "a.cpp").read_text() == "x = 1;\n"
    assert _git("rev-parse", "HEAD", cwd=wt) == commit
    (wt / "lixo.obj").write_text("build")  # sobras de compilação não impedem a remoção
    (wt / "a.cpp").write_text("x = 2;\n")
    # recriar por cima de uma sobra (execução que caiu) começa limpo
    wt = calibracao.criar_worktree(repo, commit, destino)
    assert (wt / "a.cpp").read_text() == "x = 1;\n" and not (wt / "lixo.obj").exists()
    calibracao.remover_worktree(repo, wt)
    assert not destino.exists()
    assert str(destino).replace("\\", "/").lower() not in _git("worktree", "list", cwd=repo).replace("\\", "/").lower()


# ---------- a suíte da calibração com o %APPDATA% protegido ----------

from maw_agent import config, estado, fase_calibrar, suite  # noqa: E402

SAIDA_OK = ("NOME - suite de testes automatizados (juce::UnitTest)\n"
            "[ok]     Bloco A  ->  faz x   (1 ok, 0 falha(s))\n"
            "Blocos de teste ........ 1\nVerificacoes que deram ok 1\nVerificacoes que falharam 0\n"
            "Tempo total ............ 5 ms\nRESULTADO: TUDO PASSOU.\n")


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    appdata = tmp_path / "Roaming" / "APP"
    appdata.mkdir(parents=True)
    (appdata / "config.txt").write_text("original", encoding="utf-8")
    monkeypatch.setattr(config, "APPDATA_MAW", appdata)
    monkeypatch.setattr(config, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")
    monkeypatch.setattr(config, "MUSICA", tmp_path / "Music")
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    # a pasta da calibração fora de sprint: é onde o fases procura a bandeira dela
    e = estado.Estado(tmp_path / "work" / "calibracao", 0)
    sandbox.criar_pasta(e.pasta)
    ops = fase_calibrar.OpsReais(e, False, "c" * 40, [], tmp_path / "evid", 60, 0, raiz=tmp_path / "raiz")
    ops.cwd = tmp_path / "exec"
    return {"appdata": appdata, "e": e, "ops": ops, "tmp": tmp_path}


def test_rodar_faz_backup_poe_bandeira_e_restaura(ambiente, monkeypatch):
    appdata, e, ops = ambiente["appdata"], ambiente["e"], ambiente["ops"]
    visto = {}

    def suite_falsa(exe, cwd, timeout, env):
        visto["bandeira"] = (e.pasta / "appdata-sujo.json").exists()
        (appdata / "config.txt").write_text("sujo", encoding="utf-8")
        (appdata / "novo.txt").write_text("x", encoding="utf-8")
        return suite.interpretar_suite(SAIDA_OK, 0, 1.0)

    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    monkeypatch.setattr(suite, "rodar_suite", suite_falsa)
    d = ops.rodar("d1", ambiente["tmp"] / "app.exe")
    assert visto["bandeira"] is True
    assert (appdata / "config.txt").read_text(encoding="utf-8") == "original" and not (appdata / "novo.txt").exists()
    assert not (e.pasta / "appdata-sujo.json").exists()
    assert ops.restauracoes[-1]["verificado"] and ops.restauracoes[-1]["backup_apagado"]
    assert not any((ambiente["tmp"] / "backups").iterdir())
    assert "bruto" not in d and d["passou"] is True
    assert (ambiente["tmp"] / "evid" / "suite-d1.json").exists()


def test_rodar_restaura_mesmo_se_a_suite_explode(ambiente, monkeypatch):
    appdata, ops = ambiente["appdata"], ambiente["ops"]

    def explode(exe, cwd, timeout, env):
        (appdata / "config.txt").write_text("sujo", encoding="utf-8")
        raise RuntimeError("caiu")

    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    monkeypatch.setattr(suite, "rodar_suite", explode)
    with pytest.raises(RuntimeError):
        ops.rodar("d1", ambiente["tmp"] / "app.exe")
    assert (appdata / "config.txt").read_text(encoding="utf-8") == "original"
    assert not (ambiente["e"].pasta / "appdata-sujo.json").exists()


def test_rodar_com_a_maw_aberta_aborta_sem_tocar_no_ambiente(ambiente, monkeypatch):
    monkeypatch.setattr(suite, "maw_aberta", lambda: True)
    monkeypatch.setattr(suite, "rodar_suite", lambda *a, **k: pytest.fail("a suíte não podia rodar"))
    with pytest.raises(calibracao.Abortar):
        ambiente["ops"].rodar("d1", ambiente["tmp"] / "app.exe")
    assert not (ambiente["tmp"] / "backups").exists()
    assert not (ambiente["e"].pasta / "appdata-sujo.json").exists()


def test_rodar_recupera_bandeira_de_calibracao_que_caiu(ambiente, monkeypatch):
    appdata, e, ops = ambiente["appdata"], ambiente["e"], ambiente["ops"]
    backup = sandbox.backup_pasta(appdata, config.BACKUPS)
    sandbox.escrever_json(e.pasta / "appdata-sujo.json", {"backup": str(backup), "desde": "2026-09-28T01:00:00"})
    (appdata / "config.txt").write_text("sujo de antes", encoding="utf-8")
    vistos = []
    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    monkeypatch.setattr(suite, "rodar_suite",
                        lambda *a, **k: vistos.append((appdata / "config.txt").read_text(encoding="utf-8"))
                        or suite.interpretar_suite(SAIDA_OK, 0, 1.0))
    ops.rodar("d1", ambiente["tmp"] / "app.exe")
    assert vistos == ["original"]  # o backup novo foi tirado do ambiente já restaurado
    assert (appdata / "config.txt").read_text(encoding="utf-8") == "original"


def test_rodar_restaura_pendencia_de_outro_lugar_antes_do_backup(ambiente, monkeypatch):
    # a sessão avulsa do driver de GUI caiu e deixou a bandeira dela ao lado da pasta dos backups
    appdata, ops = ambiente["appdata"], ambiente["ops"]
    backup = sandbox.backup_pasta(appdata, config.BACKUPS)
    sandbox.escrever_json(config.BACKUPS.parent / "appdata-sujo.json",
                          {"backup": str(backup), "desde": "2026-09-28T01:00:00"})
    (appdata / "config.txt").write_text("sujo da gui", encoding="utf-8")
    vistos = []
    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    monkeypatch.setattr(suite, "rodar_suite",
                        lambda *a, **k: vistos.append((appdata / "config.txt").read_text(encoding="utf-8"))
                        or suite.interpretar_suite(SAIDA_OK, 0, 1.0))
    ops.rodar("d1", ambiente["tmp"] / "app.exe")
    assert vistos == ["original"]
    assert not (config.BACKUPS.parent / "appdata-sujo.json").exists()
    assert any(r.get("origem") == "avulsa" and r["verificado"] for r in ops.restauracoes)


def test_rodar_recusa_backup_com_pendencia_que_nao_restaura(ambiente, monkeypatch):
    appdata, tmp = ambiente["appdata"], ambiente["tmp"]
    e = estado.nova_sprint(config.RELATORIOS)
    ops = fase_calibrar.OpsReais(e, True, "c" * 40, [], tmp / "evid", 60, 0, raiz=tmp / "raiz")
    ops.cwd = tmp / "exec"
    # bandeira de uma sessão que caiu, apontando para um backup que sumiu: não há como restaurar
    sandbox.escrever_json(config.BACKUPS.parent / "appdata-sujo.json",
                          {"backup": str(tmp / "sumiu"), "desde": "2026-09-28T01:00:00"})
    (appdata / "config.txt").write_text("sujo da gui", encoding="utf-8")
    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    monkeypatch.setattr(suite, "rodar_suite", lambda *a, **k: pytest.fail("a suíte não podia rodar"))
    with pytest.raises(calibracao.Abortar, match="pendente"):
        ops.rodar("d1", tmp / "app.exe")
    # nenhum backup novo do ambiente sujo foi tirado como se fosse o original
    assert not config.BACKUPS.exists() or not any(config.BACKUPS.iterdir())
    assert not (e.pasta / "appdata-sujo.json").exists()
    assert (config.BACKUPS.parent / "appdata-sujo.json").exists()  # a pendência continua à vista
    lim = json.loads((e.pasta / "limitacoes-calibracao.json").read_text(encoding="utf-8"))
    assert any("pendente" in l for l in lim)


def test_saida_nao_quebra_com_stdout_cp1252(monkeypatch):
    # o script noturno redireciona a saída para arquivo: no Windows o stdout vira cp1252,
    # que não tem "→" (usado nos nomes de bloco da calibração)
    import io
    import json as _json
    import sys
    bruto = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(bruto, encoding="cp1252"))
    obj = {"blocos": ["Bloco A → sub"], "nota": "calibração"}
    assert fase_calibrar._saida(obj, True) == 0
    sys.stdout.flush()
    assert _json.loads(bruto.getvalue().decode("cp1252")) == obj


def test_ops_reais_limpar_por_id_remove_sobra_de_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "espelho"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    monkeypatch.setattr(config, "ESPELHO", repo)
    e = estado.Estado(tmp_path / "cal", 0)
    ops = fase_calibrar.OpsReais(e, False, "c" * 40, [], tmp_path / "evid", 60, 0, raiz=tmp_path / "raiz")
    sobra = tmp_path / "raiz" / "d1" / "Source"
    sobra.mkdir(parents=True)  # add que caiu no meio: pasta sem registro no git
    ops.limpar("d1")
    assert not (tmp_path / "raiz" / "d1").exists()
    ops.limpar("nunca-existiu")  # sem sobra: não levanta


def test_cli_calibrar_relata_taxa_estrita_e_so_por_outro_bloco(tmp_path, monkeypatch, capsys):
    import json as _json
    from maw_agent import cli, fases
    yaml_defeitos = tmp_path / "defeitos.yaml"
    yaml_defeitos.write_text(
        "- id: pega\n  arquivo: Source/a.cpp\n  buscar: 'x = 1;'\n  trocar: 'x = 2;'\n  esperado: [Bloco A]\n"
        "- id: escapa\n  arquivo: Source/a.cpp\n  buscar: 'y = 1;'\n  trocar: 'y = 3;'\n  esperado: [Bloco A]\n",
        encoding="utf-8")
    raiz = tmp_path / "calibracao"

    def ops_falsas(e, em_sprint, commit, arquivos, evidencias, timeout, esperar):
        ops = OpsFalsas(tmp_path / "wts")
        ops.commit, ops.restauracoes, ops.sondas = commit, [], {}
        return ops

    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")
    monkeypatch.setattr(fase_calibrar, "DEFEITOS", yaml_defeitos)
    monkeypatch.setattr(fase_calibrar, "RAIZ", raiz)
    monkeypatch.setattr(fase_calibrar, "OpsReais", ops_falsas)
    monkeypatch.setattr(fase_calibrar, "_commit_do_main", lambda e: "c" * 40)
    monkeypatch.setattr(fases, "_arquivos_musica", lambda: {})
    assert cli.main(["calibrar"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["taxa"]["detectados"] == 1 and out["taxa"]["detectados_so_por_outro_bloco"] == 1
    assert out["taxa"]["taxa"] == 0.5
    assert out["defeitos"]["escapa"]["detectado_so_por_outro_bloco"] is True
    salvo = _json.loads((raiz / "ultimo.json").read_text(encoding="utf-8"))
    assert salvo["pega"]["detectado"] is True and salvo["escapa"]["detectado"] is False
    assert salvo["escapa"]["detectado_so_por_outro_bloco"] is True
    assert salvo["escapa"]["outros_blocos_que_falharam"] == ["Bloco B → y"]


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


def test_rodar_com_a_trava_ocupada_aborta_sem_tocar_no_ambiente(ambiente, monkeypatch):
    from maw_agent import fases

    monkeypatch.setattr(fases, "ESPERA_TRAVA_APPDATA", 0.2)
    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    monkeypatch.setattr(suite, "rodar_suite", lambda *a, **k: pytest.fail("a suíte não podia rodar"))
    with _OutraSessao():
        with pytest.raises(calibracao.Abortar, match="trava"):
            ambiente["ops"].rodar("d1", ambiente["tmp"] / "app.exe")
    assert not (ambiente["tmp"] / "backups").exists()
