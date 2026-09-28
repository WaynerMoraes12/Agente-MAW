"""Fases da sprint com o disco em tmp_path: suíte (C2, I3, I4, I7), consolidação (I2a, I5, I6),
encerrar (I2b), iniciar/marcar (I2d) e o registro de achados (I9). Nada toca a MAW real."""
import json
import os
import time
from pathlib import Path

import pytest
import yaml

from maw_agent import build, catalogo, config, estado, fases, sandbox, sondas, suite
from maw_agent.cli import main

COMMIT = "a" * 40
SAIDA_OK = ("  aviso antes do cabeçalho\r\n\r\nNOME - suite de testes automatizados (juce::UnitTest)\r\n"
            "[ok]     Bloco A  ->  faz x   (5 ok, 0 falha(s))\r\n"
            "Blocos de teste ........ 1\r\nVerificacoes que deram ok 5\r\nVerificacoes que falharam 0\r\n"
            "Tempo total ............ 10 ms\r\nRESULTADO: TUDO PASSOU.\r\n")
ITENS = [{"id": "saude/suite-existente", "area": "saude", "titulo": "t", "descricao": "d", "origem": ["x"],
          "verificacao": ["suite"], "cenarios": [], "requisitos": [], "marco": "M1"},
         {"id": "area/so-suite", "area": "area", "titulo": "t", "descricao": "d", "origem": ["x"],
          "verificacao": ["suite"], "cenarios": ["suite:Bloco A"], "requisitos": [], "marco": "M1"},
         {"id": "area/com-e2e", "area": "area", "titulo": "t", "descricao": "d", "origem": ["x"],
          "verificacao": ["suite", "e2e"], "cenarios": ["suite:Bloco A"], "requisitos": [], "marco": "M2"}]


def _ler(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _agora_menos(segundos: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - segundos))


@pytest.fixture
def sprint(tmp_path, monkeypatch):
    """Uma sprint falsa com um alvo `main` compilado nesta sprint e a suíte simulada."""
    raiz = tmp_path / "relatorios"
    monkeypatch.setattr(config, "RELATORIOS", raiz)
    monkeypatch.setattr(config, "HISTORICO", tmp_path / "historico" / "achados.json")
    monkeypatch.setattr(config, "ALVOS_DIR", tmp_path / "alvos")
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    monkeypatch.setattr(config, "FERRAMENTAS", tmp_path / "ferramentas")
    monkeypatch.setattr(config, "APPDATA_MAW", tmp_path / "Roaming" / "MAW")
    monkeypatch.setattr(config, "BACKUPS", tmp_path / "Local" / "AgenteMAW" / "backups")
    monkeypatch.setattr(config, "MUSICA", tmp_path / "Music")
    cat = tmp_path / "funcionalidades.yaml"
    cat.write_text(yaml.safe_dump(ITENS, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    # sondas do agente: nenhuma, a não ser que o teste ponha (nunca as do privado de verdade)
    monkeypatch.setattr(sondas, "PASTA_PRIVADA", tmp_path / "privado" / "sondas")
    monkeypatch.setattr(sondas, "MAPA", tmp_path / "privado" / "sondas" / "itens.yaml")
    monkeypatch.setattr(sandbox, "ESPERAS", (0, 0, 0, 0))
    e = estado.nova_sprint(raiz)
    monkeypatch.setattr(fases, "_sprint_atual", lambda: estado.carregar(e.pasta))
    sandbox.escrever_json(e.pasta / "alvos.json", {"alvos": [
        {"nome": "main", "branch": "main", "ref": "origin/main", "commit": COMMIT, "origem": "github",
         "assinatura": "s", "compartilha_com": None},
        {"nome": "docs-z", "branch": "docs/z", "ref": "origin/docs/z", "commit": "d" * 40, "origem": "github",
         "assinatura": "s", "compartilha_com": "main"}], "avisos": []})
    (config.APPDATA_MAW).mkdir(parents=True)
    (config.APPDATA_MAW / "MAW.settings").write_text("<original/>", encoding="utf-8")
    (config.MUSICA / "MAW Teste").mkdir(parents=True)
    (config.MUSICA / "MAW Teste" / "antigo.wav").write_bytes(b"RIFF")
    (config.FERRAMENTAS / "ffmpeg" / "bin").mkdir(parents=True)
    ctx = {"estado": e, "tmp": tmp_path, "chamadas": [], "durante": {}}

    def compilado(cfg: str, ok: bool = True, antigo: bool = False):
        exe = build.caminho_exe(config.ALVOS_DIR / "main", cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ")
        s = estado.carregar(e.pasta)
        s.passos[f"compilar:main:{cfg}"] = {"status": "concluido", "inicio": _agora_menos(60),
                                            "fim": _agora_menos(1), "detalhe": {"ok": ok}}
        s.salvar()
        if antigo:  # exe de uma sprint anterior
            os.utime(exe, (time.time() - 86400, time.time() - 86400))
        sandbox.escrever_json(e.pasta / "builds" / f"main-{cfg}.json", {"ok": ok, "config": cfg})

    ctx["compilado"] = compilado

    def rodar_suite(exe, cwd, timeout=1800, env=None):
        ctx["chamadas"].append(("suite", exe, env))
        ctx["durante"]["sujo"] = (e.pasta / "appdata-sujo.json").exists()
        ctx["durante"]["backups"] = [p for p in config.BACKUPS.iterdir()] if config.BACKUPS.exists() else []
        (config.APPDATA_MAW / "MAW.settings").write_text("<sujo pela suite/>", encoding="utf-8")
        (config.APPDATA_MAW / "novo.txt").write_text("lixo", encoding="utf-8")
        (config.MUSICA / "MAW Teste" / "gravacao-da-suite.wav").write_bytes(b"RIFF")
        return suite.interpretar_suite(SAIDA_OK, 0, 1.0)

    def rodar_benchmark(exe, cwd, timeout=1800, env=None):
        ctx["chamadas"].append(("benchmark", exe, env))
        return {"exit_code": 0, "segundos": 1.0, "cabecalho": [], "bruto": "",
                "linhas": [{"trilhas": 32, "medio_ms": 0.1, "max_ms": 0.2, "carga_media": 1.0, "carga_max": 2.0}]}

    class Captura:
        erro = None
        mensagens = [(1, "JUCE Assertion failure in a.cpp:1 " + "AIza" + "K" * 35)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(suite, "rodar_suite", rodar_suite)
    monkeypatch.setattr(suite, "rodar_benchmark", rodar_benchmark)
    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    monkeypatch.setattr(fases.saude, "CapturaDepuracao", Captura)
    return ctx


class Args:
    def __init__(self, **kw):
        self.alvo = None
        self.__dict__.update(kw)


def _resultados(e):
    return catalogo.carregar_resultados(e.pasta)


# ---------- C2: suíte e benchmark só sobre o binário desta sprint ----------

def test_suite_nao_roda_sobre_build_que_falhou_ou_nao_rodou(sprint):
    e = sprint["estado"]
    sprint["compilado"]("Debug", ok=False)
    sprint["compilado"]("Release", antigo=True)  # o exe existe, mas é de uma sprint anterior
    fases._suite(Args())
    assert sprint["chamadas"] == []
    r = _resultados(e)
    assert r[("saude/suite-existente", "main")]["resultado"] == "nao_testavel"
    assert r[("saude/suite-existente", "main")]["motivo"] == "build Debug falhou ou não rodou nesta sprint"
    assert r[("saude/benchmark", "main")]["motivo"] == "build Release falhou ou não rodou nesta sprint"
    assert r[("area/so-suite", "main")]["resultado"] == "nao_testavel"
    assert not (e.pasta / "achados-brutos").exists()  # nenhum achado de suíte
    # o alvo que compartilha a árvore herda a mesma situação
    assert r[("saude/suite-existente", "docs-z")]["resultado"] == "nao_testavel"


def test_suite_sem_build_registrado_nao_roda(sprint):
    fases._suite(Args())
    assert sprint["chamadas"] == []
    assert _resultados(sprint["estado"])[("saude/suite-existente", "main")]["resultado"] == "nao_testavel"


# ---------- I3/I4/I7: ciclo completo da suíte ----------

def test_suite_ciclo_completo(sprint):
    e = sprint["estado"]
    sprint["compilado"]("Debug")
    sprint["compilado"]("Release")
    assert fases._suite(Args()) == 0
    # rodou os dois, com o ffmpeg portátil na frente do PATH
    assert [c[0] for c in sprint["chamadas"]] == ["benchmark", "suite"]
    ffmpeg = str(config.FERRAMENTAS / "ffmpeg" / "bin")
    assert all(c[2]["PATH"].split(os.pathsep)[0] == ffmpeg for c in sprint["chamadas"])
    # durante a suíte: bandeira de ambiente sujo e exatamente um backup, fora do repositório
    assert sprint["durante"]["sujo"] is True and len(sprint["durante"]["backups"]) == 1
    # depois: %APPDATA%\MAW restaurado, bandeira e backup apagados
    assert (config.APPDATA_MAW / "MAW.settings").read_text(encoding="utf-8") == "<original/>"
    assert not (config.APPDATA_MAW / "novo.txt").exists()
    assert not (e.pasta / "appdata-sujo.json").exists()
    assert not [p for p in config.BACKUPS.rglob("*") if p.is_file()]
    s = estado.carregar(e.pasta)
    assert len(s.restauracoes) == 1 and s.restauracoes[0]["verificado"] and s.restauracoes[0]["backup_apagado"]
    # resultados: C1 aplicado com benchmark coberto
    r = _resultados(e)
    assert r[("saude/suite-existente", "main")]["resultado"] == "falhou"  # jassert disparou
    assert r[("area/so-suite", "main")]["resultado"] == "passou"
    assert r[("area/com-e2e", "main")]["resultado"] == "nao_testavel"
    assert r[("area/com-e2e", "docs-z")]["resultado"] == "nao_testavel"  # herdado
    # jassert redigido; preâmbulo guardado; sobras na pasta de música declaradas
    d = _ler(e.pasta / "suites" / "main.json")
    assert d["assercoes"] == ["JUCE Assertion failure in a.cpp:1 [REDACTED]"]
    assert d["preambulo"] == ["aviso antes do cabeçalho"]
    lim = _ler(e.pasta / "limitacoes-suite.json")
    assert any("1 arquivo(s)" in l and "MAW Teste" in l for l in lim)


def test_suite_recusa_com_a_maw_aberta_e_nao_mexe_no_appdata(sprint, monkeypatch):
    e = sprint["estado"]
    sprint["compilado"]("Debug")
    sprint["compilado"]("Release")
    monkeypatch.setattr(suite, "maw_aberta", lambda: True)
    assert fases._suite(Args()) == 1
    assert sprint["chamadas"] == []
    assert not config.BACKUPS.exists() and not (e.pasta / "appdata-sujo.json").exists()
    r = _resultados(e)
    assert r[("saude/suite-existente", "main")]["motivo"] == "MAW aberta: feche-a para a suíte rodar"
    assert r[("saude/benchmark", "main")]["resultado"] == "nao_testavel"
    assert not estado.carregar(e.pasta).feito("suite:main")  # fechar a MAW e rodar de novo funciona


def test_suite_restaura_mesmo_se_a_execucao_explodir(sprint, monkeypatch):
    e = sprint["estado"]
    sprint["compilado"]("Debug")

    def explode(*a, **k):
        (config.APPDATA_MAW / "MAW.settings").write_text("<sujo/>", encoding="utf-8")
        raise RuntimeError("queda simulada")

    monkeypatch.setattr(suite, "rodar_suite", explode)
    with pytest.raises(RuntimeError):
        fases._suite(Args())
    assert (config.APPDATA_MAW / "MAW.settings").read_text(encoding="utf-8") == "<original/>"
    assert not (e.pasta / "appdata-sujo.json").exists()


def test_restauracao_que_falha_mantem_bandeira_e_backup(sprint, monkeypatch):
    e = sprint["estado"]
    sprint["compilado"]("Debug")
    def restauracao_falha(backup):
        raise sandbox.RestauracaoFalhou("arquivo em uso (simulado)")

    monkeypatch.setattr(sandbox, "restaurar_pasta", restauracao_falha)
    assert fases._suite(Args()) == 1
    assert (e.pasta / "appdata-sujo.json").exists()
    info = _ler(e.pasta / "appdata-sujo.json")
    assert Path(info["backup"]).exists() and "desde" in info
    s = estado.carregar(e.pasta)
    assert s.restauracoes[-1]["verificado"] is False and "em uso" in s.restauracoes[-1]["erro"]


def test_iniciar_restaura_ambiente_sujo_antes_de_tudo(sprint, monkeypatch, capsys):
    e = sprint["estado"]
    bk = sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
    sandbox.escrever_json(e.pasta / "appdata-sujo.json", {"backup": str(bk), "desde": "2026-09-28T03:00:00"})
    (config.APPDATA_MAW / "MAW.settings").write_text("<sujo: a sprint caiu no meio da suíte/>", encoding="utf-8")
    monkeypatch.setattr(estado, "em_andamento", lambda raiz=config.RELATORIOS: estado.carregar(e.pasta))
    assert main(["sprint", "iniciar", "--retomar"]) == 0
    saida = json.loads(capsys.readouterr().out)
    assert saida["retomada"] is True and saida["restauracoes"][0]["verificado"] is True
    assert (config.APPDATA_MAW / "MAW.settings").read_text(encoding="utf-8") == "<original/>"
    assert not (e.pasta / "appdata-sujo.json").exists() and not bk.exists()
    assert estado.carregar(e.pasta).restauracoes[0]["quem"] == "retomada"


# ---------- I2b / intocada: encerrar ----------

def test_encerrar_com_guarda_e_sem_restaurar_o_preparar(sprint, monkeypatch, capsys):
    e = sprint["estado"]
    sandbox.escrever_json(e.pasta / "prova-antes.json", {})
    monkeypatch.setattr(fases.alvos, "prova_intocada", lambda pastas: {})
    fases._encerrar(None)
    info = _ler(e.pasta / "intocada.json")
    assert info["verificado"] is False and info["diferencas"] == ["nenhuma pasta da MAW encontrada para provar"]
    assert info["ambiente_restaurado"] is True  # nada foi sujo nesta sprint
    capsys.readouterr()
    # segunda chamada: devolve o guardado e não restaura nada de novo
    chamou = []
    monkeypatch.setattr(fases, "_restaurar_ambiente", lambda *a, **k: chamou.append(a))
    (e.pasta / "appdata-sujo.json").write_text(json.dumps({"backup": "x", "desde": "y"}), encoding="utf-8")
    fases._encerrar(None)
    saida = json.loads(capsys.readouterr().out)
    assert saida["retomado"] is True and chamou == []


def test_encerrar_restauracao_pendente_ou_falha_vira_ambiente_nao_restaurado(sprint, monkeypatch):
    e = sprint["estado"]
    sandbox.escrever_json(e.pasta / "prova-antes.json", {"MAW": {"tipo": "git", "head": "h"}})
    monkeypatch.setattr(fases.alvos, "prova_intocada", lambda pastas: {"MAW": {"tipo": "git", "head": "h"}})
    s = estado.carregar(e.pasta)
    s.registrar_restauracao({"quem": "suite", "backup": "b1", "verificado": False, "erro": "em uso"})
    fases._encerrar(None)
    info = _ler(e.pasta / "intocada.json")
    assert info["verificado"] is True and info["ambiente_restaurado"] is False and "em uso" in info["erro_restauracao"]


# ---------- I2a / I5 / I6: consolidar ----------

def _bruto_de_suite():
    return fases.achados_da_suite({"nome": "main", "commit": COMMIT},
                                  {"blocos": [{"nome": "Bloco B", "sub": "y", "ok": 0, "falhas": 1}],
                                   "detalhes": [], "incoerencias": [], "assercoes": []})[0]


def test_consolidar_duas_vezes_da_o_mesmo_resultado(sprint):
    e = sprint["estado"]
    sandbox.escrever_json(e.pasta / "achados-brutos" / "suite-main-000.json", _bruto_de_suite())
    sandbox.escrever_json(e.pasta / "vereditos" / "suite-main-000.json", {"resultado": "confirmado"})
    sandbox.escrever_json(e.pasta / "achados-brutos" / "x-main-001.json",
                          dict(_bruto_de_suite(), assinatura="outra-assinatura", fonte="testador-motor"))
    (e.pasta / "vereditos" / "x-main-001.json").write_text("{ilegivel", encoding="utf-8")
    fases._consolidar(None)
    primeiro = _ler(e.pasta / "achados.json")
    erros1 = _ler(e.pasta / "erros_agente.json")
    hist1 = _ler(config.HISTORICO)
    fases._consolidar(None)
    assert _ler(e.pasta / "achados.json") == primeiro
    assert all(a["estado"] == "novo" for a in primeiro)
    assert _ler(e.pasta / "erros_agente.json") == erros1 and len(erros1) == 1  # reconstruído, não acumulado
    hist2 = _ler(config.HISTORICO)
    assert hist2 == hist1
    assert all(r["historico"] == ["01"] for r in hist2["itens"].values())
    assert (e.pasta / "historico-antes.json").exists()
    detalhe = estado.carregar(e.pasta).dados("consolidar")
    assert detalhe["sem_verificacao_adversarial"] == 1
    por_assin = {a["assinatura"]: a for a in primeiro}
    assert por_assin["outra-assinatura"]["confianca"] == "provavel"


def test_consolidar_nao_acumula_falhou_de_consolidacao_anterior(sprint):
    e = sprint["estado"]
    sandbox.escrever_json(e.pasta / "achados-brutos" / "suite-main-000.json", _bruto_de_suite())
    sandbox.escrever_json(e.pasta / "vereditos" / "suite-main-000.json", {"resultado": "confirmado"})
    fases._consolidar(None)
    assert _resultados(e)[("saude/suite-existente", "main")]["resultado"] == "falhou"
    # o advogado muda de ideia e a consolidação roda de novo: a célula não fica presa em 'falhou'
    sandbox.escrever_json(e.pasta / "vereditos" / "suite-main-000.json", {"resultado": "derrubado",
                                                                          "justificativa": "x"})
    fases._consolidar(None)
    assert ("saude/suite-existente", "main") not in _resultados(e)


def test_consolidar_corrige_achado_automatico_quando_o_criterio_passa(sprint):
    e = sprint["estado"]
    # sprint anterior: bloco da suíte falhando
    hist = {"proximo": 2, "itens": {"imp": {"id": "MAW-0001", "estado": "novo", "historico": ["00"],
                                            "ultimo": _bruto_de_suite(), "sprint_do_estado": "00"}}}
    sandbox.escrever_json(config.HISTORICO, hist)
    # nesta sprint o bloco passa
    sandbox.escrever_json(e.pasta / "suites" / "main.json", {
        "blocos": [{"nome": "Bloco B", "sub": "y", "ok": 1, "falhas": 0}], "incoerencias": [], "assercoes": [],
        "captura_erro": None})
    fases._consolidar(None)
    [a] = _ler(e.pasta / "achados.json")
    assert a["id"] == "MAW-0001" and a["estado"] == "corrigido"


# ---------- I2d: marcar e subcomandos ----------

def test_sprint_marcar(sprint, capsys):
    e = sprint["estado"]
    assert main(["sprint", "marcar", "revisao:main:testador-motor", "--detalhe", '{"achados": 3}']) == 0
    s = estado.carregar(e.pasta)
    assert s.feito("revisao:main:testador-motor") and s.dados("revisao:main:testador-motor") == {"achados": 3}
    capsys.readouterr()
    assert main(["sprint", "marcar", "verificar", "--detalhe", "{nao json"]) == 1
    assert "JSON" in capsys.readouterr().out
    assert main(["sprint", "marcar", "consolidar"]) == 1  # passo da própria CLI
    assert not estado.carregar(e.pasta).feito("consolidar")


def test_alvo_so_onde_e_usado():
    with pytest.raises(SystemExit):
        main(["sprint", "iniciar", "--alvo", "main"])
    with pytest.raises(SystemExit):
        main(["sprint", "iniciar", "--nova", "--retomar"])


# ---------- I9: achado registrar ----------

def _achado_valido():
    a = _bruto_de_suite()
    a["fonte"] = "testador-motor"
    return a


def test_registrar_confere_referencias_redige_e_nao_sobrescreve(sprint, tmp_path, capsys):
    e = sprint["estado"]
    arq = tmp_path / "testador-motor-main-001.json"
    chave = "AIza" + "J" * 35
    arq.write_text(json.dumps(dict(_achado_valido(), obtido=f"vazou {chave}")), encoding="utf-8")
    assert main(["achado", "registrar", str(arq)]) == 0
    gravado = (e.pasta / "achados-brutos" / arq.name).read_text(encoding="utf-8")
    assert chave not in gravado and "[REDACTED]" in gravado
    capsys.readouterr()
    assert main(["achado", "registrar", str(arq)]) == 1  # já existe
    assert "já existe" in capsys.readouterr().out
    ruim = tmp_path / "testador-motor-main-002.json"
    ruim.write_text(json.dumps(dict(_achado_valido(), item_catalogo="area/inexistente",
                                    alvos=[{"alvo": "feature/x", "commit": COMMIT}])), encoding="utf-8")
    assert main(["achado", "registrar", str(ruim)]) == 1
    saida = capsys.readouterr().out
    assert "area/inexistente" in saida and "feature/x" in saida
    assert not (e.pasta / "achados-brutos" / ruim.name).exists()


def test_backup_que_falha_recusa_a_suite_sem_deixar_copia(sprint, monkeypatch):
    e = sprint["estado"]
    sprint["compilado"]("Debug")
    real = sandbox.copiar

    def copia_quebra(origem, destino):
        real(origem, destino)
        raise OSError("disco cheio (simulado)")

    monkeypatch.setattr(sandbox, "copiar", copia_quebra)
    assert fases._suite(Args()) == 1
    assert sprint["chamadas"] == [] and not (e.pasta / "appdata-sujo.json").exists()
    assert not [p for p in config.BACKUPS.rglob("*") if p.is_file()]
    assert "backup" in _resultados(e)[("saude/suite-existente", "main")]["motivo"]


def test_erro_de_disco_durante_a_suite_nao_vira_recusa(sprint, monkeypatch):
    sprint["compilado"]("Debug")

    def disco(*a, **k):
        raise OSError("falha de E/S no meio da suíte (simulada)")

    monkeypatch.setattr(suite, "rodar_suite", disco)
    with pytest.raises(OSError):
        fases._suite(Args())
    assert (config.APPDATA_MAW / "MAW.settings").read_text(encoding="utf-8") == "<original/>"


def test_compilar_conclui_a_fase_quando_todos_os_builds_rodaram(sprint, monkeypatch, capsys):
    e = sprint["estado"]
    monkeypatch.setattr(build, "compilar", lambda alvo, wt, cfg, logs: build.ResultadoBuild(
        alvo, cfg, True, 1.0, [], [], "x.exe", "l.log"))
    fases._compilar(Args())
    s = estado.carregar(e.pasta)
    assert s.feito("compilar:main:Release") and s.feito("compilar:main:Debug") and s.feito("compilar")


def test_reverificacoes_escritas_a_mao_antes_da_consolidacao_valem_e_nao_realimentam(sprint):
    e = sprint["estado"]
    ultimo = dict(_achado_valido(), assinatura="Source/a.cpp::f::condicao")
    sandbox.escrever_json(config.HISTORICO, {"proximo": 2, "itens": {"imp": {
        "id": "MAW-0001", "estado": "aberto", "historico": ["00"], "ultimo": ultimo, "sprint_do_estado": "00"}}})
    sandbox.escrever_json(e.pasta / "reverificacoes.json", {"MAW-0001": "corrigido"})
    fases._consolidar(None)
    [a] = _ler(e.pasta / "achados.json")
    assert a["estado"] == "corrigido" and (e.pasta / "reverificacoes-manual.json").exists()
    fases._consolidar(None)
    [b] = _ler(e.pasta / "achados.json")
    assert b == a and _ler(e.pasta / "erros_agente.json") == []


def test_bloco_da_suite_falhando_sem_veredito_bloqueia_o_alvo(sprint):
    from maw_agent.relatorio import contexto
    e = sprint["estado"]
    sandbox.escrever_json(e.pasta / "builds" / "main-Release.json", {"ok": True, "segundos": 1, "avisos": []})
    sandbox.escrever_json(e.pasta / "achados-brutos" / "suite-main-000.json", _bruto_de_suite())  # sem veredito
    fases._consolidar(None)
    [a] = _ler(e.pasta / "achados.json")
    assert a["confianca"] == "confirmado"
    assert estado.carregar(e.pasta).dados("consolidar")["sem_verificacao_adversarial"] == 0
    ctx = contexto.montar(e.pasta, ITENS, [], None, None)
    assert {x["nome"]: x["semaforo"] for x in ctx["alvos"]}["main"] == "bloqueado"
    assert not any("sem verificação adversarial" in l for l in ctx["limitacoes"])


# ---------- correção pontual pós-re-revisão ----------

def _pendencia(e, estado_appdata: str | None = None) -> Path:
    """Backup do %APPDATA%\\MAW como está agora + bandeira appdata-sujo.json na sprint `e`;
    depois suja o %APPDATA%\\MAW com `estado_appdata`."""
    bk = sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
    sandbox.escrever_json(e.pasta / "appdata-sujo.json", {"backup": str(bk), "desde": "2026-09-28T03:00:00"})
    if estado_appdata is not None:
        (config.APPDATA_MAW / "MAW.settings").write_text(estado_appdata, encoding="utf-8")
    return bk


def _appdata():
    return (config.APPDATA_MAW / "MAW.settings").read_text(encoding="utf-8")


def test_iniciar_com_a_maw_aberta_adia_a_restauracao(sprint, monkeypatch, capsys):
    e = sprint["estado"]
    bk = _pendencia(e, "<sujo/>")
    monkeypatch.setattr(estado, "em_andamento", lambda raiz=config.RELATORIOS: estado.carregar(e.pasta))
    monkeypatch.setattr(suite, "maw_aberta", lambda: True)
    assert main(["sprint", "iniciar"]) == 1
    saida = json.loads(capsys.readouterr().out)
    assert saida["restauracoes"][0]["adiada"] is True
    # nada foi restaurado nem apagado: a bandeira e o único backup bom continuam lá
    assert _appdata() == "<sujo/>" and (e.pasta / "appdata-sujo.json").exists() and bk.exists()
    reg = estado.carregar(e.pasta).restauracoes[-1]
    assert reg["adiada"] is True and reg["verificado"] is False
    motivo = "restauração do %APPDATA%\\MAW adiada: MAW aberta — feche a MAW e rode `sprint iniciar` de novo"
    assert reg["erro"] == motivo
    assert motivo in _ler(e.pasta / "limitacoes-ambiente.json")
    # a MAW foi fechada: a retomada restaura e a limitação de adiamento sai
    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    assert main(["sprint", "iniciar"]) == 0
    assert _appdata() == "<original/>" and not (e.pasta / "appdata-sujo.json").exists() and not bk.exists()
    assert motivo not in _ler(e.pasta / "limitacoes-ambiente.json", )


def test_encerrar_com_a_maw_aberta_nao_restaura_e_declara(sprint, monkeypatch):
    e = sprint["estado"]
    bk = _pendencia(e, "<sujo/>")
    sandbox.escrever_json(e.pasta / "prova-antes.json", {"MAW": {"tipo": "git", "head": "h"}})
    monkeypatch.setattr(fases.alvos, "prova_intocada", lambda pastas: {"MAW": {"tipo": "git", "head": "h"}})
    monkeypatch.setattr(suite, "maw_aberta", lambda: True)
    assert fases._encerrar(None) == 1
    info = _ler(e.pasta / "intocada.json")
    assert info["ambiente_restaurado"] is False and "adiada: MAW aberta" in info["erro_restauracao"]
    assert _appdata() == "<sujo/>" and bk.exists() and (e.pasta / "appdata-sujo.json").exists()


def test_encerrar_com_a_maw_fechada_restaura_a_pendencia(sprint, monkeypatch):
    e = sprint["estado"]
    _pendencia(e, "<sujo/>")
    sandbox.escrever_json(e.pasta / "prova-antes.json", {"MAW": {"tipo": "git", "head": "h"}})
    monkeypatch.setattr(fases.alvos, "prova_intocada", lambda pastas: {"MAW": {"tipo": "git", "head": "h"}})
    assert fases._encerrar(None) == 0
    assert _appdata() == "<original/>" and _ler(e.pasta / "intocada.json")["ambiente_restaurado"] is True


def test_varias_pendencias_restaura_a_mais_antiga_e_descarta_as_novas(sprint):
    e1 = sprint["estado"]
    bk1 = _pendencia(e1, "<sujo pela sprint-01/>")        # backup do estado original
    e2 = estado.nova_sprint(config.RELATORIOS)
    bk2 = _pendencia(e2, "<sujo pela sprint-02/>")        # backup de um estado já sujo
    regs = fases._restaurar_pendentes()
    assert _appdata() == "<original/>"                    # o estado de antes do agente
    assert not bk1.exists() and not bk2.exists()
    assert not (e1.pasta / "appdata-sujo.json").exists() and not (e2.pasta / "appdata-sujo.json").exists()
    assert [r["sprint"] for r in regs] == ["sprint-01", "sprint-02"]
    r2 = estado.carregar(e2.pasta).restauracoes[-1]
    assert r2["verificado"] is True and r2["backup_apagado"] is True and "sprint-01" in r2["descartado"]


def test_pendencia_mais_antiga_que_falha_nao_deixa_restaurar_a_mais_nova(sprint):
    e1 = sprint["estado"]
    sandbox.escrever_json(e1.pasta / "appdata-sujo.json", {"backup": str(config.BACKUPS / "sumiu"), "desde": "x"})
    e2 = estado.nova_sprint(config.RELATORIOS)
    bk2 = _pendencia(e2, "<sujo/>")
    fases._restaurar_pendentes()
    assert _appdata() == "<sujo/>" and bk2.exists() and (e2.pasta / "appdata-sujo.json").exists()


def test_suite_recusa_com_pendencia_em_qualquer_sprint(sprint):
    e1 = sprint["estado"]
    sprint["compilado"]("Debug")
    e2 = estado.nova_sprint(config.RELATORIOS)  # outra sprint, com pendência que não restaura
    sandbox.escrever_json(e2.pasta / "appdata-sujo.json", {"backup": str(config.BACKUPS / "sumiu"), "desde": "x"})
    assert fases._suite(Args()) == 1
    assert sprint["chamadas"] == []
    assert "pendente" in _resultados(e1)[("saude/suite-existente", "main")]["motivo"]


def test_registrar_recusa_fonte_mecanica(sprint, tmp_path, capsys):
    for fonte in ("build", "suite"):
        arq = tmp_path / f"falso-{fonte}.json"
        arq.write_text(json.dumps(dict(_achado_valido(), fonte=fonte)), encoding="utf-8")
        assert main(["achado", "registrar", str(arq)]) == 1
        assert "reservada" in capsys.readouterr().out
        assert not (sprint["estado"].pasta / "achados-brutos" / arq.name).exists()


def test_restauracao_pendente_da_propria_sprint_nao_se_perde_quando_a_suite_salva_o_estado(sprint):
    e = sprint["estado"]
    sprint["compilado"]("Debug")
    _pendencia(e, "<sujo por uma suíte que caiu/>")
    assert fases._suite(Args()) == 0
    quem = [r["quem"] for r in estado.carregar(e.pasta).restauracoes]
    assert quem == ["retomada", "suite"]


# ---------- sondas do agente: suíte e build ----------

SAIDA_COM_SONDA = ("NOME - suite de testes automatizados (juce::UnitTest)\r\n"
                   "[ok]     Bloco A  ->  faz x   (5 ok, 0 falha(s))\r\n"
                   "[FALHOU] SONDA Andamento  ->  clipe dividido   (1 ok, 2 falha(s))\r\n"
                   "[ok]     SONDA Andamento  ->  120 BPM   (3 ok, 0 falha(s))\r\n"
                   "Blocos de teste ........ 3\r\nVerificacoes que deram ok 9\r\nVerificacoes que falharam 2\r\n"
                   "Tempo total ............ 10 ms\r\nRESULTADO: FALHOU.\r\n"
                   "Detalhe das falhas\r\n- SONDA Andamento / clipe dividido\r\n"
                   "!!! Test 1 failed: a metade da direita tem 140 BPM e T mediu 115.58 BPM\r\n")
ITEM_SONDA = {"id": "area/andamento", "area": "area", "titulo": "t", "descricao": "d", "origem": ["x"],
              "verificacao": ["suite", "sonda"], "cenarios": [], "requisitos": [], "marco": "M5"}
SONDA_A = 'struct SondaA : juce::UnitTest { SondaA() : juce::UnitTest ("SONDA Andamento", "MAW") {} };\n'
SONDA_B = 'struct SondaB : juce::UnitTest { SondaB() : juce::UnitTest ("SONDA Audio", "MAW") {} };\n'
PROJETO = ('<?xml version="1.0" encoding="utf-8"?>\r\n'
           '<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\r\n'
           '  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets" />\r\n</Project>\r\n')


def _com_sondas(tmp: Path, projeto: bool = True) -> None:
    """Duas sondas no privado falso, o mapa e (opcional) o projeto da worktree do main."""
    sondas.PASTA_PRIVADA.mkdir(parents=True, exist_ok=True)
    (sondas.PASTA_PRIVADA / "SondaA.cpp").write_text(SONDA_A, encoding="utf-8")
    (sondas.PASTA_PRIVADA / "SondaB.cpp").write_text(SONDA_B, encoding="utf-8")
    sondas.MAPA.write_text('"SONDA Andamento": [area/andamento]\n"SONDA Audio": [area/audio, principio/P4]\n',
                           encoding="utf-8")
    cat = yaml.safe_load(config.CATALOGO.read_text(encoding="utf-8")) + [ITEM_SONDA]
    config.CATALOGO.write_text(yaml.safe_dump(cat, allow_unicode=True), encoding="utf-8")
    if projeto:
        p = config.ALVOS_DIR / "main" / sondas.PROJETO
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PROJETO.encode("utf-8"))


def _sem_jassert(monkeypatch):
    class Captura:
        erro = None
        mensagens = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(fases.saude, "CapturaDepuracao", Captura)


def test_sonda_que_falha_nao_quebra_a_suite_existente_e_vira_achado_no_item_dela(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    _sem_jassert(monkeypatch)
    monkeypatch.setattr(suite, "rodar_suite", lambda exe, cwd, timeout=1800, env=None:
                        suite.interpretar_suite(SAIDA_COM_SONDA, 1, 1.0))
    sprint["compilado"]("Debug")
    sprint["compilado"]("Release")
    assert fases._suite(Args()) == 0
    r = _resultados(e)
    assert r[("saude/suite-existente", "main")]["resultado"] == "passou"  # a suíte da MAW passou
    assert r[("area/so-suite", "main")]["resultado"] == "passou"
    assert r[("area/andamento", "main")]["resultado"] == "falhou"
    assert r[("area/andamento", "docs-z")]["resultado"] == "falhou"  # herdado da mesma árvore
    brutos = sorted(p.name for p in (e.pasta / "achados-brutos").glob("*.json"))
    assert brutos == ["sonda-main-000.json"]  # nenhum "Teste da suíte falha"
    a = _ler(e.pasta / "achados-brutos" / "sonda-main-000.json")
    assert a["fonte"] == "sonda" and a["item_catalogo"] == "area/andamento"
    assert a["causa_provavel"]["arquivo_linha"] == "privado/sondas/SondaA.cpp"
    assert "115.58" in a["obtido"] and "SondaA.cpp" in " ".join(a["passos"])
    d = _ler(e.pasta / "suites" / "main.json")
    assert d["passou"] is True and d["total_falhas"] == 0 and [b["nome"] for b in d["blocos"]] == ["Bloco A"]
    assert d["sondas"]["total_falhas"] == 2 and len(d["sondas"]["blocos"]) == 2
    assert any("115.58" in l for l in d["sondas"]["detalhes"]) and d["detalhes"] == []


def test_sonda_que_nao_rodou_diz_o_motivo_no_item(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    sandbox.escrever_json(e.pasta / "sondas.json", {"main": {
        "injetadas": [], "ausentes": {"SONDA Andamento": "sonda não compila em main: SondaA.cpp(3): error C2065"}}})
    sprint["compilado"]("Debug")
    fases._suite(Args())
    motivo = _resultados(e)[("area/andamento", "main")]["motivo"]
    assert "SONDA Andamento: sonda não compila em main" in motivo


def test_suite_que_nao_rodou_marca_os_itens_das_sondas(sprint):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    fases._suite(Args())  # sem build nesta sprint
    r = _resultados(e)
    assert r[("area/andamento", "main")]["resultado"] == "nao_testavel"
    assert "build Debug" in r[("area/andamento", "main")]["motivo"]


class _BuildFalso:
    """build.compilar falso: devolve os resultados da fila (Debug) e registra as sondas presentes
    na worktree a cada chamada."""

    def __init__(self, erros_debug: list[list[str]]):
        self.fila = list(erros_debug)
        self.chamadas: list[tuple[str, list[str]]] = []

    def __call__(self, alvo, wt, cfg, logs):
        pasta = Path(wt) / sondas.PASTA_NA_WORKTREE
        self.chamadas.append((cfg, sorted(p.name for p in pasta.glob("*.cpp")) if pasta.is_dir() else []))
        erros = self.fila.pop(0) if cfg == "Debug" and self.fila else []
        log = Path(logs) / f"build-{alvo}-{cfg}.log"
        sandbox.escrever_texto(log, "\n".join(erros) or "ok")
        return build.ResultadoBuild(alvo, cfg, not erros, 1.0, [], erros, None if erros else "x.exe", str(log))


ERRO_SONDA = r"C:\w\alvos\main\Source\Tests\Sondas\SondaA.cpp(3,1): error C2065: 'x': undeclared identifier"
ERRO_MAW = r"C:\w\alvos\main\Source\Audio\Motor.cpp(9,2): error C2143: syntax error: missing ';'"


def test_compilar_injeta_as_sondas_so_antes_do_debug(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    falso = _BuildFalso([])
    monkeypatch.setattr(build, "compilar", falso)
    assert fases._compilar(Args()) == 0
    assert falso.chamadas == [("Release", []), ("Debug", ["SondaA.cpp", "SondaB.cpp"])]
    projeto = (config.ALVOS_DIR / "main" / sondas.PROJETO).read_text(encoding="utf-8")
    assert "SondasDoAgente" in projeto and "Debug" in projeto
    info = _ler(e.pasta / "sondas.json")["main"]
    assert info["injetadas"] == ["SondaA.cpp", "SondaB.cpp"] and info["ausentes"] == {}
    assert _resultados(e)[("saude/build-debug", "main")]["resultado"] == "passou"


def test_sonda_que_nao_compila_vira_limitacao_e_o_debug_recompila_sem_ela(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    falso = _BuildFalso([[ERRO_SONDA]])
    monkeypatch.setattr(build, "compilar", falso)
    assert fases._compilar(Args()) == 0
    assert falso.chamadas == [("Release", []), ("Debug", ["SondaA.cpp", "SondaB.cpp"]), ("Debug", ["SondaB.cpp"])]
    assert _ler(e.pasta / "builds" / "main-Debug.json")["ok"] is True
    assert not (e.pasta / "achados-brutos").exists()  # não é falha de build da MAW
    lim = _ler(e.pasta / "limitacoes-sondas.json")
    assert any(l.startswith("sonda não compila em main: SondaA.cpp") and "C2065" in l for l in lim)
    info = _ler(e.pasta / "sondas.json")["main"]
    assert info["injetadas"] == ["SondaB.cpp"] and "SondaA.cpp" in info["excluidas"]
    assert "não compila" in info["ausentes"]["SONDA Andamento"]
    r = _resultados(e)
    assert r[("saude/build-debug", "main")]["resultado"] == "passou"
    assert r[("area/andamento", "main")]["resultado"] == "nao_testavel"
    assert "não compila" in r[("area/andamento", "main")]["motivo"]
    # o log da tentativa com a sonda fica como evidência
    assert (e.pasta / "evidencias" / "builds" / "build-main-Debug-com-sondas-1.log").read_text(
        encoding="utf-8").startswith(ERRO_SONDA)


def test_erro_da_maw_com_sondas_injetadas_vira_achado_de_build(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    falso = _BuildFalso([[ERRO_MAW], [ERRO_MAW]])  # com e sem as sondas: a falha é da MAW
    monkeypatch.setattr(build, "compilar", falso)
    assert fases._compilar(Args()) == 1
    assert falso.chamadas[1:] == [("Debug", ["SondaA.cpp", "SondaB.cpp"]), ("Debug", [])]
    a = _ler(e.pasta / "achados-brutos" / "build-main-Debug.json")
    assert "Motor.cpp" in a["obtido"] and "Sondas" not in a["obtido"]
    lim = _ler(e.pasta / "limitacoes-sondas.json")
    assert not any(l.startswith("sonda não compila") for l in lim)
    assert any("falha é da MAW" in l for l in lim)


def test_erro_misto_so_a_parte_da_maw_vira_achado(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    falso = _BuildFalso([[ERRO_SONDA, ERRO_MAW], [ERRO_MAW], [ERRO_MAW]])
    monkeypatch.setattr(build, "compilar", falso)
    assert fases._compilar(Args()) == 1
    assert [c[1] for c in falso.chamadas[1:]] == [["SondaA.cpp", "SondaB.cpp"], ["SondaB.cpp"], []]
    a = _ler(e.pasta / "achados-brutos" / "build-main-Debug.json")
    assert "Motor.cpp" in a["obtido"] and "SondaA" not in a["obtido"]
    lim = _ler(e.pasta / "limitacoes-sondas.json")
    assert any(l.startswith("sonda não compila em main: SondaA.cpp") for l in lim)


def test_injecao_que_falha_vira_limitacao_e_o_build_segue(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"], projeto=False)  # sem .vcxproj: a injeção não tem onde pôr as sondas
    falso = _BuildFalso([])
    monkeypatch.setattr(build, "compilar", falso)
    assert fases._compilar(Args()) == 0
    assert [c[0] for c in falso.chamadas] == ["Release", "Debug"]
    lim = _ler(e.pasta / "limitacoes-sondas.json")
    assert any(l.startswith("sondas não injetadas em main:") for l in lim)
    assert "não injetadas" in _ler(e.pasta / "sondas.json")["main"]["ausentes"]["SONDA Audio"]


def test_sem_sondas_no_privado_nao_injeta_nada(sprint, monkeypatch):
    e = sprint["estado"]
    p = config.ALVOS_DIR / "main" / sondas.PROJETO
    p.parent.mkdir(parents=True)
    p.write_bytes(PROJETO.encode("utf-8"))
    falso = _BuildFalso([])
    monkeypatch.setattr(build, "compilar", falso)
    fases._compilar(Args())
    assert p.read_bytes() == PROJETO.encode("utf-8") and not (e.pasta / "sondas.json").exists()


def test_mapa_das_sondas_ilegivel_vira_limitacao_e_a_suite_roda(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    sondas.MAPA.write_text("{isto: [nao fecha\n", encoding="utf-8")
    _sem_jassert(monkeypatch)
    monkeypatch.setattr(suite, "rodar_suite", lambda exe, cwd, timeout=1800, env=None:
                        suite.interpretar_suite(SAIDA_COM_SONDA, 1, 1.0))
    sprint["compilado"]("Debug")
    assert fases._suite(Args()) == 0
    assert any("mapa de itens das sondas ilegível" in l for l in _ler(e.pasta / "limitacoes-sondas.json"))
    a = _ler(e.pasta / "achados-brutos" / "sonda-main-000.json")
    assert a["item_catalogo"] == "saude/geral"


def test_suite_incoerente_com_sondas_declara_que_nao_da_para_separar(sprint, monkeypatch):
    e = sprint["estado"]
    _com_sondas(sprint["tmp"])
    _sem_jassert(monkeypatch)
    morreu = SAIDA_COM_SONDA.split("Blocos de teste")[0]  # a execução morreu antes dos totais
    monkeypatch.setattr(suite, "rodar_suite", lambda exe, cwd, timeout=1800, env=None:
                        suite.interpretar_suite(morreu, -1073741819, 1.0))
    sprint["compilado"]("Debug")
    fases._suite(Args())
    assert any("incoerente com as sondas" in l for l in _ler(e.pasta / "limitacoes-sondas.json"))
    assert _resultados(e)[("saude/suite-existente", "main")]["resultado"] == "falhou"
