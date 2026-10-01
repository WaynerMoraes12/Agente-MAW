import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from maw_agent import estado, fase_noturno as N
from maw_agent.preflight import Verificacao
from maw_agent.cli import main

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
SEGREDO = "AIza" + "B" * 35
SEGUNDA = date(2026, 9, 28)
TERCA = date(2026, 9, 29)
TODOS = {"sprint", "achado", "catalogo", "e2e", "servico", "calibrar", "sondas", "noturno"}


@pytest.fixture(autouse=True)
def _bancada_sem_config_do_privado(tmp_path, monkeypatch):
    """Nenhum teste lê `privado/bancada/config.yaml` de verdade (ele pode desativar a bancada); quem
    precisa de uma configuração põe a sua."""
    monkeypatch.setattr(N, "CONFIG_BANCADA", tmp_path / "sem-config-da-bancada.yaml")


# ---------- argumentos ----------

@pytest.mark.parametrize("texto,esperado", [("22:00", "22:00"), ("7:05", "07:05"), (" 00:00 ", "00:00"),
                                            ("23:59", "23:59")])
def test_hora_valida_normaliza(texto, esperado):
    assert N.hora_valida(texto) == esperado


@pytest.mark.parametrize("texto", ["24:00", "22:60", "abc", "22", "22:0", "-1:00", ""])
def test_hora_valida_recusa(texto):
    with pytest.raises(argparse.ArgumentTypeError):
        N.hora_valida(texto)


def _parser():
    p = argparse.ArgumentParser()
    N.configurar(p)
    return p


def test_parser_rodar_tem_padroes_da_noite():
    a = _parser().parse_args(["rodar"])
    assert (a.ensaio, a.calibrar, a.so_alvo, a.sem_julgamento, a.retomar) == (False, False, None, False, False)
    assert a.limite == "07:00" and a.entrada_real == "auto"


def test_parser_rodar_le_todas_as_opcoes():
    a = _parser().parse_args(["rodar", "--ensaio", "--calibrar", "--so-alvo", "main", "--sem-julgamento",
                              "--retomar", "--limite", "6:30", "--entrada-real", "nao"])
    op = N.opcoes_de(a)
    assert op == N.Opcoes(ensaio=True, calibrar=True, so_alvo="main", sem_julgamento=True, retomar=True,
                          limite="06:30", entrada_real="nao")


def test_parser_recusa_alvo_estranho_e_hora_invalida():
    with pytest.raises(SystemExit):
        _parser().parse_args(["rodar", "--so-alvo", "main; rm"])
    with pytest.raises(SystemExit):
        _parser().parse_args(["instalar", "--hora", "25:00"])


def test_cli_instalar_recusa_hora_invalida_sem_registrar(monkeypatch):
    chamadas = []
    monkeypatch.setattr(N.subprocess, "run", lambda *a, **k: chamadas.append(a))
    with pytest.raises(SystemExit) as ex:
        main(["noturno", "instalar", "--hora", "25:00"])
    assert ex.value.code == 2 and chamadas == []


def test_instalar_hora_padrao_22():
    assert _parser().parse_args(["instalar"]).hora == "22:00"


# ---------- tarefa agendada ----------

def _xml(hora="22:00"):
    return N.xml_tarefa(hora, Path(r"C:\Agente X\ferramentas\noturno.ps1"), Path(r"C:\Agente X"),
                        r"PC\Fulano", SEGUNDA)


def test_xml_da_tarefa_so_com_usuario_conectado_sem_elevacao():
    raiz = ET.fromstring(_xml().split("?>", 1)[1])
    assert raiz.find("t:Principals/t:Principal/t:LogonType", NS).text == "InteractiveToken"
    assert raiz.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"
    assert raiz.find("t:Principals/t:Principal/t:UserId", NS).text == r"PC\Fulano"


def test_xml_da_tarefa_diaria_na_hora_pedida():
    raiz = ET.fromstring(_xml("21:30").split("?>", 1)[1])
    gatilho = raiz.find("t:Triggers/t:CalendarTrigger", NS)
    assert gatilho.find("t:StartBoundary", NS).text == "2026-09-28T21:30:00"
    assert gatilho.find("t:ScheduleByDay/t:DaysInterval", NS).text == "1"


def test_xml_da_tarefa_chama_o_script_pelo_powershell():
    raiz = ET.fromstring(_xml().split("?>", 1)[1])
    exe = raiz.find("t:Actions/t:Exec", NS)
    assert exe.find("t:Command", NS).text == "powershell.exe"
    args = exe.find("t:Arguments", NS).text
    assert '-File "C:\\Agente X\\ferramentas\\noturno.ps1"' in args
    assert "-NoProfile" in args and "-ExecutionPolicy Bypass" in args
    # janela oculta: uma janela minimizada fechada por engano matou a noite de 28/09
    assert "-WindowStyle Hidden" in args and "Minimized" not in args
    assert exe.find("t:WorkingDirectory", NS).text == r"C:\Agente X"


def test_xml_da_tarefa_roda_na_bateria_e_nao_duplica():
    raiz = ET.fromstring(_xml().split("?>", 1)[1])
    s = raiz.find("t:Settings", NS)
    assert s.find("t:DisallowStartIfOnBatteries", NS).text == "false"
    assert s.find("t:StopIfGoingOnBatteries", NS).text == "false"
    assert s.find("t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert s.find("t:ExecutionTimeLimit", NS).text.startswith("PT")


def test_xml_escapa_caracteres_especiais():
    xml = N.xml_tarefa("22:00", Path(r"C:\A & B\noturno.ps1"), Path(r"C:\A & B"), "PC\\X", SEGUNDA)
    raiz = ET.fromstring(xml.split("?>", 1)[1])
    assert raiz.find("t:Actions/t:Exec/t:WorkingDirectory", NS).text == r"C:\A & B"


def test_linhas_do_schtasks():
    xml = Path(r"C:\t\tarefa.xml")
    assert N.comando_instalar(xml) == ["schtasks", "/Create", "/TN", "Agente MAW - sprint noturna",
                                       "/XML", str(xml), "/F"]
    assert N.comando_remover() == ["schtasks", "/Delete", "/TN", "Agente MAW - sprint noturna", "/F"]
    assert N.comando_consultar() == ["schtasks", "/Query", "/TN", "Agente MAW - sprint noturna", "/XML"]


def test_ler_xml_da_tarefa_ida_e_volta():
    info = N.ler_xml_tarefa(_xml("22:00"))
    assert info["hora"] == "22:00" and info["diaria"] is True
    assert info["somente_conectado"] is True and info["elevada"] is False
    assert info["script"] == r"C:\Agente X\ferramentas\noturno.ps1"
    assert info["roda_na_bateria"] is True


def test_decodificar_saida_do_schtasks():
    assert N.decodificar(b"ERRO: O sistema n\xc6o pode") == "ERRO: O sistema não pode"
    assert N.decodificar("ação".encode("utf-8")) == "ação"


# ---------- log redigido ----------

def test_registro_redige_segredos(tmp_path):
    reg = N.Registro(tmp_path / "noite.log", eco=False)
    reg.linha(f"chave {SEGREDO} no meio", "etapa")
    texto = (tmp_path / "noite.log").read_text(encoding="utf-8")
    assert SEGREDO not in texto and "[REDACTED]" in texto and "[etapa]" in texto


def test_executar_etapa_redige_a_saida_e_devolve_o_codigo(tmp_path):
    reg = N.Registro(tmp_path / "noite.log", eco=False)
    cmd = [sys.executable, "-c", f"print('chave={SEGREDO}'); print('ação'); import sys; sys.exit(3)"]
    r = N.executar_etapa(N.Etapa("falsa", cmd, 60), reg, N.ambiente_filhos(dict(os.environ), False), tmp_path)
    texto = (tmp_path / "noite.log").read_text(encoding="utf-8")
    assert r.codigo == 3 and not r.estourou
    assert SEGREDO not in texto and "chave=[REDACTED]" in texto and "ação" in texto
    assert SEGREDO not in r.saida


def test_executar_etapa_mata_a_arvore_quando_passa_do_tempo(tmp_path):
    reg = N.Registro(tmp_path / "noite.log", eco=False)
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    t0 = time.monotonic()
    r = N.executar_etapa(N.Etapa("lenta", cmd, 1), reg, N.ambiente_filhos(dict(os.environ), False), tmp_path)
    assert r.estourou and time.monotonic() - t0 < 30


def test_executar_etapa_que_nem_comeca(tmp_path):
    reg = N.Registro(tmp_path / "noite.log", eco=False)
    r = N.executar_etapa(N.Etapa("x", [str(tmp_path / "nao-existe.exe")], 5), reg, {}, tmp_path)
    assert r.codigo is None and r.erro


# ---------- ambiente e janela da noite ----------

def test_ambiente_dos_filhos():
    env = N.ambiente_filhos({"PATH": "x"}, True)
    assert env["MAW_AGENTE_ENTRADA_REAL"] == "1"
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    assert env["PYTHONUTF8"] == "1" and env["PATH"] == "x"
    assert N.ambiente_filhos({}, False)["MAW_AGENTE_ENTRADA_REAL"] == "0"


@pytest.mark.parametrize("hora,esperado", [(22, True), (3, True), (6, True), (7, False), (9, False),
                                           (15, False), (20, True)])
def test_entrada_real_automatica_so_de_noite(hora, esperado):
    assert N.decidir_entrada_real("auto", datetime(2026, 9, 28, hora, 0), "07:00") is esperado


def test_entrada_real_forcada():
    assert N.decidir_entrada_real("sim", datetime(2026, 9, 28, 12, 0), "07:00") is True
    assert N.decidir_entrada_real("nao", datetime(2026, 9, 28, 23, 0), "07:00") is False


def test_prazo_da_manha():
    assert N.prazo_manha(datetime(2026, 9, 28, 22, 0), "07:00") == datetime(2026, 9, 29, 7, 0)
    assert N.prazo_manha(datetime(2026, 9, 29, 1, 0), "07:00") == datetime(2026, 9, 29, 7, 0)


# ---------- plano da noite ----------

def _nomes(acoes):
    return [(acao, et.nome) for acao, et in acoes]


def test_plano_completo_de_segunda():
    acoes, lims = N.planejar(N.Opcoes(), TODOS, SEGUNDA, "py", "claude.exe", tem_sondas=True)
    assert _nomes(acoes) == [("rodar", "iniciar"), ("rodar", "preflight"), ("rodar", "preparar"),
                             ("rodar", "sondas"), ("fundo", "julgamento-revisao"), ("rodar", "compilar"),
                             ("rodar", "suite"), ("rodar", "calibrar"), ("rodar", "e2e"), ("rodar", "servico"),
                             ("esperar", "julgamento-revisao"), ("rodar", "julgamento-final")]
    assert lims == []
    cmd = dict((et.nome, et.comando) for _, et in acoes)
    assert cmd["iniciar"] == ["py", "-m", "maw_agent", "sprint", "iniciar", "--nova"]
    assert cmd["sondas"] == ["py", "-m", "maw_agent", "sondas", "injetar"]
    assert cmd["e2e"] == ["py", "-m", "maw_agent", "e2e"]


def test_plano_chama_o_claude_sem_bare_e_sem_perguntar():
    acoes, _ = N.planejar(N.Opcoes(), TODOS, SEGUNDA, "py", "claude.exe", tem_sondas=False)
    cmd = dict((et.nome, et.comando) for _, et in acoes)
    rev, fim = cmd["julgamento-revisao"], cmd["julgamento-final"]
    assert rev[:3] == ["claude.exe", "-p", "/sprint --retomar --ate revisao --noturno"]
    assert fim[:3] == ["claude.exe", "-p", "/sprint --retomar --noturno"]
    for c in (rev, fim):
        assert c[c.index("--permission-mode") + 1] == "dontAsk"
        assert c[c.index("--output-format") + 1] == "stream-json" and "--verbose" in c
        assert "--bare" not in c and "--max-turns" not in c


def test_plano_sem_subcomandos_das_outras_trilhas_declara_limitacao():
    acoes, lims = N.planejar(N.Opcoes(), {"sprint", "noturno"}, SEGUNDA, "py", "c", tem_sondas=True)
    nomes = [et.nome for _, et in acoes]
    assert not {"e2e", "servico", "calibrar", "sondas"} & set(nomes)
    assert len(lims) == 4
    assert any("`e2e`" in l for l in lims) and any("`servico`" in l for l in lims)
    assert any("`calibrar`" in l for l in lims) and any("`sondas`" in l for l in lims)


def test_plano_sem_sondas_no_privado_nao_declara_limitacao_de_sondas():
    _, lims = N.planejar(N.Opcoes(), {"sprint", "e2e", "servico", "calibrar"}, SEGUNDA, "py", "c",
                         tem_sondas=False)
    assert lims == []


def test_calibrar_so_na_segunda_ou_forcado():
    nomes = lambda op, dia: [et.nome for _, et in N.planejar(op, TODOS, dia, "py", "c", False)[0]]
    assert "calibrar" in nomes(N.Opcoes(), SEGUNDA)
    assert "calibrar" not in nomes(N.Opcoes(), TERCA)
    assert "calibrar" in nomes(N.Opcoes(calibrar=True), TERCA)
    # fora do dia, a calibração ausente não é limitação
    assert N.planejar(N.Opcoes(), {"sprint"} | {"e2e", "servico"}, TERCA, "py", "c", False)[1] == []


def test_plano_so_um_alvo_sem_julgamento_e_retomando():
    op = N.Opcoes(so_alvo="main", sem_julgamento=True, retomar=True, calibrar=True)
    acoes, _ = N.planejar(op, TODOS, TERCA, "py", "c", False)
    cmd = dict((et.nome, et.comando) for _, et in acoes)
    assert not any(n.startswith("julgamento") for n in cmd)
    assert cmd["iniciar"][-1] == "--retomar"
    for fase in ("sondas", "compilar", "suite", "e2e", "servico"):
        assert cmd[fase][-2:] == ["--alvo", "main"]
    assert "--alvo" not in cmd["calibrar"]  # a calibração é sempre sobre o main


def test_plano_sem_claude_declara_limitacao():
    acoes, lims = N.planejar(N.Opcoes(), TODOS, TERCA, "py", None, False)
    assert not any(et.nome.startswith("julgamento") for _, et in acoes)
    assert any("claude" in l.lower() for l in lims)


def test_e2e_e_a_unica_etapa_com_entrada_real():
    acoes, _ = N.planejar(N.Opcoes(), TODOS, SEGUNDA, "py", "c", True)
    assert [et.nome for _, et in acoes if et.entrada_real] == ["e2e"]
    assert {et.nome for _, et in acoes if et.mexe_na_maw} == {"suite", "e2e", "calibrar"}


# ---------- bancada ----------

def test_lotes_da_bancada_de_ate_50():
    dados = {f"t{i:02d}": {"estado": "passou", "nota": "ok", "alvo": "main"} for i in range(74)}
    lotes, ignorados = N.lotes_bancada(dados, "sprint-02", "2026-09-29T01:02:03.000Z")
    assert [len(l) for l in lotes] == [50, 24] and ignorados == []
    e = lotes[0][0]
    assert e == {"op": "set", "collection": "resultados", "doc_id": "t00__windows__agente",
                 "data": {"teste": "t00", "plataforma": "windows", "quem": "agente", "estado": "passou",
                          "nota": "ok (sprint-02)", "em": "2026-09-29T01:02:03.000Z"}}


def test_lotes_da_bancada_nota_com_alvo_redigida_e_limitada():
    dados = {"ramo-b": {"estado": "problema", "nota": f"x {SEGREDO} " + "y" * 3000, "alvo": "feature-ramo-b"},
             "abrir": {"estado": "pulei", "nota": "", "alvo": "main"}}
    lotes, _ = N.lotes_bancada(dados, "sprint-02", "t")
    por_id = {e["doc_id"]: e["data"] for e in lotes[0]}
    nota = por_id["ramo-b__windows__agente"]["nota"]
    assert SEGREDO not in nota and "[REDACTED]" in nota and len(nota) <= 1000
    assert nota.endswith("(sprint-02, alvo feature-ramo-b)")
    assert por_id["abrir__windows__agente"]["nota"] == "(sprint-02)"


def test_lotes_da_bancada_ignora_estado_ou_codigo_invalido():
    dados = {"a": {"estado": "talvez", "nota": "", "alvo": "main"},
             "b/c": {"estado": "passou", "nota": "", "alvo": "main"},
             "d": {"estado": "passou", "nota": "", "alvo": "main"}}
    lotes, ignorados = N.lotes_bancada(dados, "s", "t")
    assert [e["doc_id"] for e in lotes[0]] == ["d__windows__agente"] and len(ignorados) == 2


def test_carimbo_de_hora_iso_utc():
    em = N.agora_iso()
    assert em.endswith("Z") and datetime.fromisoformat(em.replace("Z", "+00:00"))


def test_cli_bancada_imprime_lotes(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MAW_AGENTE_BANCADA_URL", "https://exemplo.invalid/bancada")
    (tmp_path / "bancada.json").write_text(json.dumps({"abrir": {"estado": "passou", "nota": "n",
                                                                  "alvo": "main"}}), encoding="utf-8")
    assert main(["noturno", "bancada", "--pasta", str(tmp_path)]) == 0
    saida = json.loads(capsys.readouterr().out)
    assert saida["ok"] and saida["url"] == "https://exemplo.invalid/bancada" and saida["total"] == 1
    assert saida["colecao"] == "resultados" and saida["lotes"][0][0]["doc_id"] == "abrir__windows__agente"


def test_cli_bancada_sem_arquivo_vira_limitacao(tmp_path, capsys):
    assert main(["noturno", "bancada", "--pasta", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    lims = json.loads((tmp_path / "limitacoes-bancada.json").read_text(encoding="utf-8"))
    assert len(lims) == 1 and "bancada" in lims[0]


def test_cli_limitacao_nao_repete(tmp_path):
    for _ in range(2):
        assert main(["noturno", "limitacao", "--pasta", str(tmp_path), "julgamento sem permissão"]) == 0
    assert json.loads((tmp_path / "limitacoes-noturno.json").read_text(encoding="utf-8")) == ["julgamento sem permissão"]


def test_cli_limitacao_com_origem_e_redigida(tmp_path):
    assert main(["noturno", "limitacao", "--pasta", str(tmp_path), "--origem", "bancada", f"erro {SEGREDO}"]) == 0
    assert json.loads((tmp_path / "limitacoes-bancada.json").read_text(encoding="utf-8")) == ["erro [REDACTED]"]
    with pytest.raises(SystemExit):
        main(["noturno", "limitacao", "--pasta", str(tmp_path), "--origem", "../x", "t"])


def test_ambiente_dos_filhos_tira_marcas_da_sessao_do_claude_mas_mantem_o_token():
    env = N.ambiente_filhos({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": "s",
                             "CLAUDE_CODE_OAUTH_TOKEN": "tok"}, True)
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


# ---------- bancada: URL fora do repositório público ----------

def test_url_da_bancada_vem_do_privado_e_a_variavel_manda(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("url: https://exemplo.invalid/b\n", encoding="utf-8")
    monkeypatch.setattr(N, "CONFIG_BANCADA", cfg)
    monkeypatch.delenv("MAW_AGENTE_BANCADA_URL", raising=False)
    assert N.url_bancada() == "https://exemplo.invalid/b"
    monkeypatch.setenv("MAW_AGENTE_BANCADA_URL", "https://outra.invalid/c")
    assert N.url_bancada() == "https://outra.invalid/c"
    monkeypatch.delenv("MAW_AGENTE_BANCADA_URL")
    monkeypatch.setattr(N, "CONFIG_BANCADA", tmp_path / "nao-existe.yaml")
    assert N.url_bancada() is None


def test_cli_bancada_sem_url_vira_limitacao(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(N, "CONFIG_BANCADA", tmp_path / "nao-existe.yaml")
    monkeypatch.delenv("MAW_AGENTE_BANCADA_URL", raising=False)
    (tmp_path / "bancada.json").write_text(json.dumps({"abrir": {"estado": "passou", "nota": "", "alvo": "main"}}),
                                           encoding="utf-8")
    assert main(["noturno", "bancada", "--pasta", str(tmp_path)]) == 1
    lims = json.loads((tmp_path / "limitacoes-bancada.json").read_text(encoding="utf-8"))
    assert any("URL" in l for l in lims)


def test_codigo_publico_nao_tem_a_url_da_bancada():
    assert "claude.ai/artifact/" not in Path(N.__file__).read_text(encoding="utf-8")


# ---------- permissões (settings.json e linha de comando da noite) ----------

RAIZ = Path(N.__file__).resolve().parent.parent
PREFIXOS = (".venv/Scripts/python -m maw_agent", ".venv/Scripts/python.exe -m maw_agent",
            "PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python -m maw_agent",
            "PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m maw_agent")
SO_DO_SCRIPT = ("sprint iniciar", "sprint preflight", "sprint preparar", "sprint compilar", "sprint suite",
                "e2e", "servico", "calibrar", "sondas injetar", "noturno instalar", "noturno remover",
                "noturno rodar")


def _regras(tipo):
    d = json.loads((RAIZ / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return d["permissions"][tipo]


def _bate(cmd, regras):
    import fnmatch
    return any(r.startswith("Bash(") and fnmatch.fnmatchcase(cmd, r[5:-1]) for r in regras)


def _permitido(cmd):
    return _bate(cmd, _regras("allow")) and not _bate(cmd, _regras("deny"))


def _comandos_ma():
    import re
    textos = [(RAIZ / ".claude" / "commands" / "sprint.md").read_text(encoding="utf-8")]
    textos += [p.read_text(encoding="utf-8") for p in (RAIZ / ".claude" / "agents").glob("*.md")]
    achados = set()
    for t in textos:
        for m in re.finditer(r"MA ([a-z-]+) ([a-z-]+)", t):
            achados.add(f"{m[1]} {m[2]}")
    return achados


def test_settings_libera_tudo_o_que_o_julgamento_noturno_roda():
    usados = _comandos_ma() - set(SO_DO_SCRIPT)
    assert {"sprint marcar", "sprint status", "achado registrar", "noturno bancada"} <= usados
    for c in sorted(usados):
        for p in PREFIXOS:
            assert _permitido(f"{p} {c}") and _permitido(f"{p} {c} --x 'y'"), f"{p} {c}"
    for g in ("git -C work/espelho diff origin/main...abc", "git -C work/espelho show abc:README.md",
              "git -C work/espelho ls-tree -r --name-only abc", "git -C privado add relatorios historico catalogo",
              'git -C privado commit -m "sprint 02: relatorio"', "git -C privado push"):
        assert _permitido(g), g


def test_settings_nao_libera_nada_do_que_o_script_roda():
    for c in SO_DO_SCRIPT:
        for p in PREFIXOS:
            assert not _permitido(f"{p} {c}") and not _permitido(f"{p} {c} --alvo main"), f"{p} {c}"
    assert not _permitido("git -C work/espelho diff --output=C:/x.txt origin/main...abc")
    assert not _permitido(".venv/Scripts/python -c print(1)")


def test_settings_nega_leitura_de_segredos():
    deny = _regras("deny")
    for r in ("Read(//c/Users/User/AppData/Roaming/MAW/**)", "Read(//c/Users/User/AppData/Local/AgenteMAW/**)",
              "Read(**/gemini_api_key.txt)", "Read(//c/Users/User/.claude.json)",
              "Read(//c/Users/User/.claude/.credentials.json)", "Edit(//c/Users/User/MAW*/**)"):
        assert r in deny
    d = json.loads((RAIZ / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert {"Read", "Grep", "Glob"} <= set(_regras("allow")) and "defaultMode" not in d["permissions"]


def test_julgamento_proibe_na_linha_de_comando_o_que_e_do_script():
    cmd = N.comando_claude("claude.exe", N.PROMPT_FINAL)
    proibidas = cmd[cmd.index("--disallowedTools") + 1:]
    assert proibidas and all(not r.startswith("-") for r in proibidas)
    for c in SO_DO_SCRIPT:
        for p in PREFIXOS:
            assert _bate(f"{p} {c} --alvo main", proibidas) and _bate(f"{p} {c}", proibidas), f"{p} {c}"
    assert not _bate(".venv/Scripts/python -m maw_agent sprint marcar x", proibidas)
    assert N.regra_home("/.claude/**").startswith("Read(//") and N.regra_home("/.claude/**") in proibidas


# ---------- confiança do workspace e sessão bloqueada ----------

def test_workspace_confiavel_le_so_a_marca_do_projeto(tmp_path):
    arq = tmp_path / ".claude.json"
    raiz = Path(r"C:\X\Agente MAW")
    for chave in ("C:/X/Agente MAW", "c:\\X\\Agente MAW", "C:\\x\\agente maw\\"):
        arq.write_text(json.dumps({"projects": {chave: {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        assert N.workspace_confiavel(raiz, arq) is True, chave
    arq.write_text(json.dumps({"projects": {"C:/X/Agente MAW": {"hasTrustDialogAccepted": False}}}), encoding="utf-8")
    assert N.workspace_confiavel(raiz, arq) is False
    arq.write_text(json.dumps({"projects": {"C:/Outro": {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    assert N.workspace_confiavel(raiz, arq) is False
    # uma pasta acima confiável NÃO basta: o claude -p exige a entrada do próprio projeto
    arq.write_text(json.dumps({"projects": {"C:/X": {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    assert N.workspace_confiavel(raiz, arq) is False
    assert N.workspace_confiavel(raiz, tmp_path / "nao-existe.json") is None
    arq.write_text("{quebrado", encoding="utf-8")
    assert N.workspace_confiavel(raiz, arq) is None


def test_sessao_bloqueada_responde_sem_erro():
    assert N.sessao_bloqueada() in (True, False, None)


# ---------- diagnóstico do claude -p ----------

def _res(codigo=0, alertas=(), final=None, erro=None, estourou=False):
    return N.Resultado(codigo, 1.0, estourou, erro, "", alertas=list(alertas), final=final)


def test_diagnostico_ok_sem_causas():
    assert N.diagnosticar_julgamento(_res(final={"subtype": "success", "is_error": False,
                                                  "permission_denials": []})) == []


def test_diagnostico_workspace_sem_confianca_e_regras_ignoradas():
    r = _res(alertas=["Workspace has not been trusted", "Ignoring 27 permissions.allow rules from project"],
             final={"subtype": "success", "is_error": False, "permission_denials": []})
    causas = " ".join(N.diagnosticar_julgamento(r))
    assert "confiança" in causas and "permissions.allow" in causas


def test_diagnostico_resultado_com_erro_e_permissoes_negadas():
    r = _res(codigo=1, final={"subtype": "error_during_execution", "is_error": True,
                              "permission_denials": [{"tool_name": "Bash"}, {"tool_name": "Write"}]})
    causas = " ".join(N.diagnosticar_julgamento(r))
    assert "error_during_execution" in causas and "2 permissão(ões) negada(s)" in causas
    assert "Bash" in causas and "Write" in causas and "código 1" in causas


def test_diagnostico_sem_linha_final():
    assert any("linha final" in c for c in N.diagnosticar_julgamento(_res(final=None)))


def test_executar_etapa_analisa_a_saida_do_claude(tmp_path):
    reg = N.Registro(tmp_path / "noite.log", eco=False)
    final = {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
             "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "ls"}}]}
    prog = ("import sys, json; print('Workspace has not been trusted', file=sys.stderr); "
            f"print(json.dumps({final!r}))")
    et = N.Etapa("julgamento-x", [sys.executable, "-c", prog], 60, analisar=True)
    r = N.executar_etapa(et, reg, N.ambiente_filhos(dict(os.environ), False), tmp_path)
    assert r.alertas and "trusted" in r.alertas[0]
    assert r.final["subtype"] == "success" and r.final["permission_denials"] == [{"tool_name": "Bash"}]


def test_executar_etapa_cancelada_mata_a_arvore(tmp_path):
    import threading
    reg = N.Registro(tmp_path / "noite.log", eco=False)
    et = N.Etapa("lenta", [sys.executable, "-c", "import time; time.sleep(60)"], 600)
    threading.Timer(0.5, et.parar.set).start()
    t0 = time.monotonic()
    r = N.executar_etapa(et, reg, N.ambiente_filhos(dict(os.environ), False), tmp_path)
    assert time.monotonic() - t0 < 30 and r.erro and "interrompida" in r.erro


# ---------- orquestração (executor falso) ----------

MARCAS_FINAIS = ("executar", "verificar", "consolidar", "encerrar", "bancada", "textos", "relatorio")
SUCESSO = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": []}


class Relogio:
    def __init__(self, t: datetime):
        self.t, self.quebrar = t, False

    def __call__(self):
        if self.quebrar:
            self.quebrar = False
            raise RuntimeError("relógio quebrado de propósito")
        return self.t


class Falso:
    """Executor falso: registra a ordem, o ambiente e o limite de cada etapa e devolve o que ela pediria."""

    def __init__(self, pasta: Path, codigos: dict | None = None, estouros: set | None = None,
                 ao_rodar: dict | None = None, disponiveis=TODOS, saidas: dict | None = None,
                 finais: dict | None = None):
        self.pasta, self.codigos, self.estouros = pasta, codigos or {}, estouros or set()
        self.ao_rodar, self.disponiveis = ao_rodar or {}, disponiveis
        self.saidas, self.finais = saidas or {}, finais or {}
        self.ordem: list[str] = []
        self.envs: dict[str, dict] = {}
        self.limites: dict[str, float | None] = {}
        self.comandos: dict[str, list[str]] = {}

    def __call__(self, etapa, registro, env, cwd, timeout=None):
        self.ordem.append(etapa.nome)
        self.envs[etapa.nome], self.limites[etapa.nome] = dict(env), timeout
        self.comandos[etapa.nome] = list(etapa.comando)
        if etapa.nome in self.ao_rodar:
            self.ao_rodar[etapa.nome](etapa)
        saida = self.saidas.get(etapa.nome, "")
        if etapa.nome == "subcomandos":
            saida = json.dumps({"subcomandos": {k: "" for k in self.disponiveis}})
        elif etapa.nome == "iniciar":
            saida = json.dumps({"sprint": self.pasta.name, "pasta": str(self.pasta)}, indent=2)
        final = self.finais.get(etapa.nome, dict(SUCESSO) if etapa.nome.startswith("julgamento") else None)
        return N.Resultado(self.codigos.get(etapa.nome, 0), 0.01, etapa.nome in self.estouros, None, saida,
                           final=final)


def _sprint(tmp_path) -> Path:
    e = estado.Estado(tmp_path / "relatorios" / "sprint-02", 2)
    e.pasta.mkdir(parents=True)
    e.salvar()
    return e.pasta


def _marcar(pasta, *passos):
    def f(_etapa):
        e = estado.carregar(pasta)
        for p in passos:
            e.concluir(p, {})
    return f


def _julgamento_ok(pasta, **extra):
    return {"julgamento-revisao": _marcar(pasta, "catalogar"),
            "julgamento-final": _marcar(pasta, *MARCAS_FINAIS), **extra}


class VolumeFalso:
    def __init__(self, mudo=True, nivel=0.55, falhar_ao_ajustar=False):
        self.mudo, self.nivel, self.falhar = mudo, nivel, falhar_ao_ajustar
        self.ajustes: list[tuple[bool, float]] = []

    def ler(self):
        return self.mudo, self.nivel

    def ajustar(self, mudo, nivel):
        if self.falhar:
            raise OSError("dispositivo sumiu")
        self.ajustes.append((mudo, nivel))
        self.mudo, self.nivel = mudo, nivel


def _rodar(tmp_path, falso, op=None, agora=datetime(2026, 9, 28, 22, 0), sessao=lambda: False, volume=None,
           ocioso=lambda: 3600.0, confianca=lambda: True, dormir=None):
    """`confianca` falsa por padrão: a noite dos testes nunca depende do ~/.claude.json de verdade."""
    relogio = agora if callable(agora) else (lambda: agora)
    return N.rodar(op or N.Opcoes(), executar=falso, agora=relogio, logs=tmp_path / "logs", python="py",
                   claude="claude.exe", tem_sondas=False, manter_acordado=lambda _: None, sessao=sessao,
                   volume=volume or (lambda: None), ocioso=ocioso, confianca=confianca,
                   dormir=dormir or (lambda _s: None))


def _lims(pasta, origem="noturno"):
    p = pasta / f"limitacoes-{origem}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _falhas(pasta):
    return {k: v for k, v in estado.carregar(pasta).passos.items() if v.get("status") == "falhou"}


def test_rodar_segue_a_ordem_da_noite(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta))
    assert _rodar(tmp_path, falso) == 0
    o = falso.ordem
    assert o[:4] == ["subcomandos", "iniciar", "preflight", "preparar"]
    assert o.index("compilar") < o.index("suite") < o.index("calibrar") < o.index("e2e") < o.index("servico")
    assert o.index("julgamento-revisao") < o.index("julgamento-final") and o[-1] == "julgamento-final"
    assert _falhas(pasta) == {} and _lims(pasta) == []
    assert falso.envs["e2e"]["MAW_AGENTE_ENTRADA_REAL"] == "1"
    assert (tmp_path / "logs" / "2026-09-28.log").exists()
    assert not (tmp_path / "logs" / "rodando.json").exists()


def test_rodar_falha_de_etapa_nao_para_a_noite(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, codigos={"compilar": 1, "e2e": 2}, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso)
    assert falso.ordem[-1] == "julgamento-final" and "servico" in falso.ordem
    assert "FALHOU" in (tmp_path / "logs" / "2026-09-28.log").read_text(encoding="utf-8")


def test_rodar_declara_subcomandos_ausentes_na_sprint(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, disponiveis={"sprint", "noturno"}, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso)
    assert "e2e" not in falso.ordem
    assert any("`e2e`" in l for l in _lims(pasta)) and any("`servico`" in l for l in _lims(pasta))


def test_rodar_rede_de_seguranca_gera_o_relatorio_se_o_julgamento_falhar(tmp_path):
    pasta = _sprint(tmp_path)
    (pasta / "bancada.json").write_text("{}", encoding="utf-8")
    vistas = {}
    falso = Falso(pasta, codigos={"julgamento-final": 1},
                  ao_rodar={"julgamento-revisao": _marcar(pasta, "catalogar"),
                            "relatorio": lambda et: vistas.update(lims=_lims(pasta))})
    assert _rodar(tmp_path, falso) == 1  # o relatório (falso) não concluiu o passo
    o = falso.ordem
    assert o[o.index("julgamento-final") + 1:] == ["consolidar", "encerrar", "relatorio", "privado-add",
                                                   "privado-commit", "privado-push"]
    assert any("julgamento" in l for l in vistas["lims"]) and any("bancada" in l for l in vistas["lims"])
    assert _falhas(pasta)["noturno:julgamento-final"]["status"] == "falhou"


def test_rede_de_seguranca_empurra_mesmo_sem_nada_para_commitar(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, codigos={"julgamento-final": 1, "privado-commit": 1})
    _rodar(tmp_path, falso)
    assert falso.ordem[-2:] == ["privado-commit", "privado-push"]


def test_rodar_sem_julgamento_faz_relatorio_parcial_sem_push(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta)
    _rodar(tmp_path, falso, N.Opcoes(sem_julgamento=True, so_alvo="main"))
    assert "julgamento-revisao" not in falso.ordem and "relatorio" in falso.ordem
    assert not any(n.startswith("privado") for n in falso.ordem)


def test_rodar_etapa_que_estoura_restaura_o_ambiente(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, estouros={"suite"}, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso)
    o = falso.ordem
    assert o[o.index("suite") + 1] == "restaurar"
    assert any("suite" in l for l in _lims(pasta))


def test_rodar_nada_que_abre_a_maw_comeca_depois_do_limite(tmp_path):
    pasta = _sprint(tmp_path)
    relogio = Relogio(datetime(2026, 9, 28, 22, 0))
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta, compilar=lambda et: setattr(
        relogio, "t", datetime(2026, 9, 29, 7, 30))))
    _rodar(tmp_path, falso, N.Opcoes(entrada_real="nao"), agora=relogio)
    assert not {"suite", "e2e", "calibrar"} & set(falso.ordem)
    assert "servico" in falso.ordem and "julgamento-final" in falso.ordem
    lims = " ".join(_lims(pasta))
    assert "`suite`" in lims and "`e2e`" in lims and "`calibrar`" in lims and "janela da noite" in lims


def test_rodar_e2e_cortada_no_fim_da_janela_diz_o_motivo(tmp_path):
    pasta = _sprint(tmp_path)
    relogio = Relogio(datetime(2026, 9, 28, 22, 0))
    falso = Falso(pasta, estouros={"e2e"}, ao_rodar=_julgamento_ok(pasta, preparar=lambda et: setattr(
        relogio, "t", datetime(2026, 9, 29, 6, 0))))
    _rodar(tmp_path, falso, agora=relogio)
    assert falso.limites["e2e"] == 3600
    o = falso.ordem
    assert o[o.index("e2e") + 1] == "restaurar"
    assert any("fim da janela da noite" in l for l in _lims(pasta))


def test_rodar_prazo_final_forca_a_rede_de_seguranca(tmp_path):
    pasta = _sprint(tmp_path)
    relogio = Relogio(datetime(2026, 9, 28, 22, 0))
    falso = Falso(pasta, ao_rodar={"julgamento-revisao": _marcar(pasta, "catalogar"),
                                   "compilar": lambda et: setattr(relogio, "t", datetime(2026, 9, 29, 10, 30))})
    _rodar(tmp_path, falso, N.Opcoes(prazo_final="11:00"), agora=relogio)
    o = falso.ordem
    assert o[o.index("compilar") + 1:] == ["consolidar", "encerrar", "relatorio", "privado-add",
                                           "privado-commit", "privado-push"]
    assert falso.limites["julgamento-revisao"] <= 12 * 3600  # até 10:00 (11:00 menos a reserva)
    assert any("prazo final" in l for l in _lims(pasta))


def test_rodar_preflight_bloqueado_pula_as_fases_mecanicas(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, codigos={"preflight": 1}, ao_rodar=_julgamento_ok(pasta),
                  saidas={"preflight": json.dumps({"ok": False, "bloqueios": ["MSBuild não encontrado"]})})
    _rodar(tmp_path, falso)
    assert not {"sondas", "compilar", "suite", "e2e", "servico", "calibrar"} & set(falso.ordem)
    assert "julgamento-final" in falso.ordem
    assert any("MSBuild não encontrado" in l for l in _lims(pasta))


def test_rodar_sessao_bloqueada_roda_e2e_sem_entrada_real(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso, sessao=lambda: True)
    assert "e2e" in falso.ordem and falso.envs["e2e"]["MAW_AGENTE_ENTRADA_REAL"] == "0"
    assert any("sessão bloqueada às 22:00" in l for l in _lims(pasta))


def test_rodar_julgamento_com_permissao_negada_vira_falha(tmp_path):
    pasta = _sprint(tmp_path)
    negado = {**SUCESSO, "permission_denials": [{"tool_name": "Bash"}]}
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta), finais={"julgamento-revisao": negado})
    _rodar(tmp_path, falso)
    assert "noturno:julgamento-revisao" in _falhas(pasta)
    assert any(l.startswith("julgamento") and "Bash" in l for l in _lims(pasta))


def test_rodar_julgamento_sem_confianca_vira_falha(tmp_path):
    pasta = _sprint(tmp_path)

    class SemConfianca(Falso):
        def __call__(self, etapa, *a, **k):
            r = super().__call__(etapa, *a, **k)
            if etapa.nome.startswith("julgamento"):
                r.alertas.append("Workspace has not been trusted: ignoring project settings")
            return r

    _rodar(tmp_path, SemConfianca(pasta, ao_rodar=_julgamento_ok(pasta)))
    assert {"noturno:julgamento-revisao", "noturno:julgamento-final"} <= set(_falhas(pasta))
    assert any("confiança" in l for l in _lims(pasta))


def test_julgamento_que_sai_0_sem_as_marcas_vira_falha(tmp_path):
    pasta = _sprint(tmp_path)
    (pasta / "alvos.json").write_text(json.dumps({"alvos": [
        {"nome": "main", "compartilha_com": None}, {"nome": "feature-x", "compartilha_com": None}]}),
        encoding="utf-8")
    falso = Falso(pasta, ao_rodar={"julgamento-revisao": _marcar(pasta, "catalogar", "revisao:main:testador-motor"),
                                   "julgamento-final": _marcar(pasta, "relatorio")})
    _rodar(tmp_path, falso)
    falhas = _falhas(pasta)
    assert "revisao:feature-x:guardiao-da-ideia" in falhas["noturno:julgamento-revisao"]["erro"]
    assert "revisao:main:testador-ia" in falhas["noturno:julgamento-revisao"]["erro"]
    assert "textos" in falhas["noturno:julgamento-final"]["erro"]


def test_revisao_em_paralelo_com_compilar_e_espera_de_verdade(tmp_path):
    import threading
    pasta = _sprint(tmp_path)
    compilando, liberar = threading.Event(), threading.Event()
    marcas = {}

    def revisao(etapa):
        marcas["viu_compilar"] = compilando.wait(5)   # a revisão está rodando quando o build começa
        liberar.wait(5)
        time.sleep(0.2)
        marcas["revisao_fim"] = time.monotonic()
        _marcar(pasta, "catalogar")(etapa)

    def calibrar(etapa):
        marcas["mecanicas_antes_da_revisao"] = "revisao_fim" not in marcas
        liberar.set()

    falso = Falso(pasta, ao_rodar={"julgamento-revisao": revisao, "compilar": lambda et: compilando.set(),
                                   "calibrar": calibrar,
                                   "julgamento-final": lambda et: marcas.update(final_inicio=time.monotonic())})
    _rodar(tmp_path, falso)
    assert marcas["viu_compilar"] and marcas["mecanicas_antes_da_revisao"]
    assert marcas["final_inicio"] >= marcas["revisao_fim"]


def test_rodar_erro_do_agente_mata_o_fundo_e_faz_a_rede(tmp_path):
    pasta = _sprint(tmp_path)
    relogio = Relogio(datetime(2026, 9, 28, 22, 0))
    cancelada = {}

    def revisao(etapa):
        cancelada["sim"] = etapa.parar.wait(10)

    falso = Falso(pasta, ao_rodar={"julgamento-revisao": revisao,
                                   "compilar": lambda et: setattr(relogio, "quebrar", True)})
    _rodar(tmp_path, falso, agora=relogio)
    assert cancelada.get("sim") is True
    o = falso.ordem
    assert o[o.index("compilar") + 1:] == ["consolidar", "encerrar", "relatorio", "privado-add",
                                           "privado-commit", "privado-push"]
    log = (tmp_path / "logs" / "2026-09-28.log").read_text(encoding="utf-8")
    assert "Traceback" in log and "relógio quebrado de propósito" in log
    assert "noturno:orquestracao" in _falhas(pasta)


def test_rodar_recusa_segunda_execucao_ao_mesmo_tempo(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "rodando.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    falso = Falso(_sprint(tmp_path))
    assert _rodar(tmp_path, falso) == 1
    assert "iniciar" not in falso.ordem


# ---------- ensaio ----------

def _ensaio(tmp_path, falso, confianca=lambda: True, sessao=lambda: False):
    verificacoes = [Verificacao("msbuild", True, "ok", True, []),
                    Verificacao("loopback", False, "sem", False, ["loopback"])]
    return N.rodar(N.Opcoes(ensaio=True), executar=falso, agora=lambda: datetime(2026, 9, 28, 10, 0),
                   logs=tmp_path / "logs", python="py", claude="claude.exe", tem_sondas=False,
                   manter_acordado=lambda _: None, verificar=lambda: verificacoes,
                   status=lambda: {"instalada": True, "hora": "22:00"}, confianca=confianca, sessao=sessao)


def test_ensaio_so_confere_sem_rodar_fases(tmp_path, capsys):
    falso = Falso(_sprint(tmp_path))
    assert _ensaio(tmp_path, falso) == 0
    assert falso.ordem == ["subcomandos", "claude-versao"]
    log = (tmp_path / "logs" / "2026-09-28-ensaio.log").read_text(encoding="utf-8")
    assert "julgamento-final" in log and "loopback" in log
    saida = json.loads(capsys.readouterr().out)
    assert saida["ensaio"] is True and saida["tarefa"]["instalada"] is True and saida["confianca"] is True


def test_ensaio_sem_confianca_falha_com_a_instrucao(tmp_path, capsys):
    assert _ensaio(tmp_path, Falso(_sprint(tmp_path)), confianca=lambda: False) == 1
    esperado = "aceite a confiança do workspace: rode `claude` uma vez nesta pasta"
    assert esperado in (tmp_path / "logs" / "2026-09-28-ensaio.log").read_text(encoding="utf-8")
    assert esperado in json.loads(capsys.readouterr().out)["pendencias"]


def test_ensaio_nao_registra_nada_do_claude_json_alem_da_marca(tmp_path, capsys):
    arq = tmp_path / ".claude.json"
    arq.write_text(json.dumps({"oauthAccount": {"email": "segredo-do-arquivo@x"},
                               "projects": {"C:/Outro": {"segredo": "segredo-do-arquivo"}}}), encoding="utf-8")
    codigo = _ensaio(tmp_path, Falso(_sprint(tmp_path)),
                     confianca=lambda: N.workspace_confiavel(Path(r"C:\X\Agente MAW"), arq))
    saida = capsys.readouterr().out
    log = (tmp_path / "logs" / "2026-09-28-ensaio.log").read_text(encoding="utf-8")
    assert codigo == 1 and "segredo-do-arquivo" not in log and "segredo-do-arquivo" not in saida


def test_ensaio_avisa_sessao_bloqueada_sem_falhar(tmp_path, capsys):
    assert _ensaio(tmp_path, Falso(_sprint(tmp_path)), sessao=lambda: True) == 0
    assert "sessão bloqueada" in (tmp_path / "logs" / "2026-09-28-ensaio.log").read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["sessao_bloqueada"] is True


# ---------- som, desktop oculto e limite da E2E ----------

AJUDA_E2E_NOVA = "usage: maw_agent e2e [-h] [--alvo ALVO] [--oculto] [--limite HH:MM]"


def test_e2e_com_som_e_volume_baixo_audivel_devolvido_depois(tmp_path):
    pasta = _sprint(tmp_path)
    vol = VolumeFalso(mudo=True, nivel=0.55)
    durante = {}
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta, e2e=lambda et: durante.update(estado=vol.ler())))
    _rodar(tmp_path, falso, volume=lambda: vol)
    assert falso.envs["e2e"]["MAW_AGENTE_SOM"] == "1" and "MAW_AGENTE_SOM" not in falso.envs["servico"]
    assert durante["estado"] == (False, N.NIVEL_SOM) and 0.1 <= N.NIVEL_SOM <= 0.3  # nunca mudo: zera o loopback
    assert vol.ler() == (True, 0.55) and vol.ajustes[-1] == (True, 0.55)
    log = (tmp_path / "logs" / "2026-09-28.log").read_text(encoding="utf-8")
    assert "som:" in log and "devolvido" in log


def test_volume_indisponivel_ou_com_erro_nao_para_a_noite(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso, volume=lambda: VolumeFalso(falhar_ao_ajustar=True))
    assert "e2e" in falso.ordem and falso.ordem[-1] == "julgamento-final"
    pasta2 = _sprint(tmp_path / "b")
    falso2 = Falso(pasta2, ao_rodar=_julgamento_ok(pasta2))
    _rodar(tmp_path / "b", falso2, volume=lambda: None)
    assert "e2e" in falso2.ordem


def test_e2e_recebe_o_limite_quando_aceita(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta), saidas={"e2e-opcoes": AJUDA_E2E_NOVA})
    _rodar(tmp_path, falso)
    cmd = falso.comandos["e2e"]
    assert cmd[cmd.index("--limite") + 1] == "07:00" and "--oculto" not in cmd
    assert falso.comandos["e2e-opcoes"][-2:] == ["e2e", "--help"]


def test_e2e_antiga_nao_recebe_opcoes_que_nao_conhece(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta), saidas={"e2e-opcoes": "usage: maw_agent e2e [--alvo ALVO]"})
    _rodar(tmp_path, falso, sessao=lambda: True)
    assert "--limite" not in falso.comandos["e2e"] and "--oculto" not in falso.comandos["e2e"]
    assert any("sessão bloqueada às 22:00: e2e sem teclado/mouse reais" == l for l in _lims(pasta))


def test_sessao_bloqueada_roda_a_e2e_no_desktop_oculto(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta), saidas={"e2e-opcoes": AJUDA_E2E_NOVA})
    _rodar(tmp_path, falso, sessao=lambda: True)
    assert "--oculto" in falso.comandos["e2e"] and falso.envs["e2e"]["MAW_AGENTE_ENTRADA_REAL"] == "0"
    assert "sessão bloqueada às 22:00: e2e no desktop oculto, sem teclado/mouse reais" in _lims(pasta)


def test_e2e_so_no_desktop_normal_com_entrada_real_e_sessao_desbloqueada(tmp_path):
    """Sessão em estado desconhecido (a consulta falhou): e2e no desktop oculto, sem entrada real."""
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta), saidas={"e2e-opcoes": AJUDA_E2E_NOVA})
    _rodar(tmp_path, falso, sessao=lambda: None)
    assert "--oculto" in falso.comandos["e2e"] and falso.envs["e2e"]["MAW_AGENTE_ENTRADA_REAL"] == "0"
    assert "sessão em estado desconhecido às 22:00: e2e no desktop oculto, sem teclado/mouse reais" in _lims(pasta)
    assert falso.envs["e2e"]["MAW_AGENTE_MICROFONE"] == "1"


def test_volume_padrao_so_le_sem_mudar_nada():
    v = N.volume_padrao()  # só leitura: nada é ajustado neste teste
    if v is not None:
        mudo, nivel = v.ler()
        assert isinstance(mudo, bool) and 0.0 <= nivel <= 1.0


def test_usuario_ativo_no_pc_manda_a_e2e_para_o_desktop_oculto(tmp_path):
    """Sessão desbloqueada mas alguém mexeu no PC há 1 min: nada de teclado/mouse reais."""
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso, ocioso=lambda: 60.0)
    assert falso.envs["e2e"]["MAW_AGENTE_ENTRADA_REAL"] == "0"
    assert any("usuário ativo no PC" in l for l in _lims(pasta))


def test_ociosidade_desconhecida_conta_como_usuario_ativo(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso, ocioso=lambda: None)
    assert falso.envs["e2e"]["MAW_AGENTE_ENTRADA_REAL"] == "0"


def test_e2e_oculta_tambem_e_cortada_no_limite_da_manha(tmp_path):
    """Às 06:30 com o limite em 07:00, a E2E (mesmo no desktop oculto) recebe no máximo ~30 min."""
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso, agora=datetime(2026, 9, 29, 6, 30), sessao=lambda: True)
    assert falso.limites["e2e"] <= 31 * 60



def test_lotes_bancada_item_so_de_linux_ou_mac_vai_para_o_sistema_dele():
    dados = {"abrir": {"estado": "passou", "nota": "ok", "alvo": "main"},
             "l-audio": {"estado": "pulei", "nota": "exige Linux", "alvo": "main"},
             "projeto-windows": {"estado": "pulei", "nota": "exige Linux/macOS", "alvo": "main"}}
    plats = {"abrir": ["windows", "linux", "macos"], "l-audio": ["linux"], "projeto-windows": ["linux", "macos"]}
    lotes, ignorados = N.lotes_bancada(dados, "s", "t", plataformas=plats)
    ids = sorted(w["doc_id"] for l in lotes for w in l)
    assert ids == ["abrir__windows__agente", "l-audio__linux__agente",
                   "projeto-windows__linux__agente", "projeto-windows__macos__agente"]
    assert all(w["data"]["plataforma"] == w["doc_id"].split("__")[1] for l in lotes for w in l)
    assert ignorados == []


def test_plataformas_da_bancada_le_a_copia_local(tmp_path):
    (tmp_path / "x.json").write_text('{"plataformas": ["linux"]}', encoding="utf-8")
    (tmp_path / "y.json").write_text("{quebrado", encoding="utf-8")
    assert N.plataformas_da_bancada(tmp_path) == {"x": ["linux"]}
    assert N.plataformas_da_bancada(tmp_path / "nao-existe") == {}



def test_bancada_desativada_nao_envia_nem_registra_limitacao(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("desativada: true", encoding="utf-8")
    monkeypatch.setattr(N, "CONFIG_BANCADA", cfg)
    pasta = tmp_path / "sprint-09"
    pasta.mkdir()
    (pasta / "bancada.json").write_text('{"abrir": {"estado": "passou", "nota": "ok", "alvo": "main"}}', encoding="utf-8")

    class A:
        pass
    a = A(); a.pasta = str(pasta)
    assert N._bancada(a) != 0
    saida = json.loads(capsys.readouterr().out)
    assert saida["desativada"] is True and "lotes" not in saida
    assert not (pasta / "limitacoes-bancada.json").exists()



# ---------- workspace sem confiança: a noite roda sem julgamento ----------

def test_workspace_sem_confianca_roda_sem_julgamento_e_diz_por_que(tmp_path):
    """O `claude -p` num workspace não confiável só pode falhar: nem começa."""
    pasta = _sprint(tmp_path)
    falso = Falso(pasta)
    _rodar(tmp_path, falso, confianca=lambda: False)
    o = falso.ordem
    assert not any(n.startswith("julgamento") for n in o)
    assert {"compilar", "suite", "e2e", "servico"} <= set(o)
    assert o[-3:] == ["consolidar", "encerrar", "relatorio"]  # a rede de segurança faz o PDF
    assert not any(n.startswith("privado") for n in o)  # como no -SemJulgamento: fica só no PC
    lims = _lims(pasta)
    assert N.MSG_SEM_CONFIANCA in lims
    assert "rode `claude` uma vez na pasta e aceite" in N.MSG_SEM_CONFIANCA
    assert not any("-SemJulgamento" in l for l in lims)  # a causa é a confiança, não a opção
    assert "rodando sem julgamento" in (tmp_path / "logs" / "2026-09-28.log").read_text(encoding="utf-8")


def test_confianca_desconhecida_nao_desliga_o_julgamento(tmp_path):
    """~/.claude.json ilegível por um instante (outra sessão gravando) não tira o julgamento da noite."""
    pasta = _sprint(tmp_path)
    falso = Falso(pasta, ao_rodar=_julgamento_ok(pasta))
    _rodar(tmp_path, falso, confianca=lambda: None)
    assert "julgamento-revisao" in falso.ordem and "julgamento-final" in falso.ordem
    assert N.MSG_SEM_CONFIANCA not in _lims(pasta)


def test_sem_julgamento_pedido_nao_ganha_a_limitacao_da_confianca(tmp_path):
    pasta = _sprint(tmp_path)
    falso = Falso(pasta)
    _rodar(tmp_path, falso, N.Opcoes(sem_julgamento=True), confianca=lambda: False)
    lims = _lims(pasta)
    assert N.MSG_SEM_CONFIANCA not in lims and any("-SemJulgamento" in l for l in lims)


def test_ensaio_sem_confianca_mostra_a_noite_sem_julgamento(tmp_path, capsys):
    assert _ensaio(tmp_path, Falso(_sprint(tmp_path)), confianca=lambda: False) == 1
    saida = json.loads(capsys.readouterr().out)
    assert not any("julgamento" in p for p in saida["plano"])
    assert N.MSG_SEM_CONFIANCA in saida["limitacoes"]


# ---------- julgamento pela sessão interativa: a rede de segurança espera ----------

def _marca_interativa(tmp_path):
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    m = tmp_path / "logs" / N.MARCA_INTERATIVA
    m.write_text('{"sprint": "sprint-02"}', encoding="utf-8")
    return m


def test_sessao_interativa_gera_o_pdf_e_a_rede_nao_roda(tmp_path):
    """Sem confiança do workspace, a sessão interativa julga; a noite espera ela terminar em vez de
    consolidar e gerar o PDF sem os vereditos."""
    pasta = _sprint(tmp_path)
    _marca_interativa(tmp_path)
    esperas = []

    def dormir(seg):
        esperas.append(seg)
        if len(esperas) == 3:
            _marcar(pasta, "consolidar", "encerrar", "relatorio")(None)

    falso = Falso(pasta)
    assert _rodar(tmp_path, falso, confianca=lambda: False, dormir=dormir) == 0
    assert not {"consolidar", "encerrar", "relatorio"} & set(falso.ordem)
    assert len(esperas) == 3
    lims = _lims(pasta)
    assert N.MSG_SEM_CONFIANCA_INTERATIVO in lims and N.MSG_SEM_CONFIANCA not in lims
    log = (tmp_path / "logs" / "2026-09-28.log").read_text(encoding="utf-8")
    assert "esperando a sessão interativa" in log and "PDF gerado" in log


def test_sessao_interativa_que_nao_termina_ate_o_prazo_cai_na_rede(tmp_path):
    pasta = _sprint(tmp_path)
    _marca_interativa(tmp_path)
    agora = [datetime(2026, 9, 28, 22, 0)]
    falso = Falso(pasta)

    def dormir(seg):
        agora[0] += timedelta(hours=2)

    _rodar(tmp_path, falso, confianca=lambda: False, dormir=dormir, agora=lambda: agora[0])
    assert falso.ordem[-3:] == ["consolidar", "encerrar", "relatorio"]
    assert any("sessão interativa não terminou o julgamento" in l for l in _lims(pasta))


def test_marca_interativa_retirada_libera_a_rede(tmp_path):
    pasta = _sprint(tmp_path)
    marca = _marca_interativa(tmp_path)
    falso = Falso(pasta)
    _rodar(tmp_path, falso, confianca=lambda: False, dormir=lambda s: marca.unlink())
    assert falso.ordem[-3:] == ["consolidar", "encerrar", "relatorio"]


def test_marca_interativa_velha_e_ignorada(tmp_path):
    pasta = _sprint(tmp_path)
    marca = _marca_interativa(tmp_path)
    velho = time.time() - 30 * 3600
    os.utime(marca, (velho, velho))
    falso = Falso(pasta)
    _rodar(tmp_path, falso, confianca=lambda: False,
           dormir=lambda s: (_ for _ in ()).throw(AssertionError("não devia esperar")))
    assert falso.ordem[-3:] == ["consolidar", "encerrar", "relatorio"]
    assert N.MSG_SEM_CONFIANCA in _lims(pasta)


# ---------- pacote de correção no fim da noite ----------

def _sprint_com_pdf(tmp_path, achados):
    e = estado.Estado(tmp_path / "relatorios" / "sprint-07", 7)
    e.pasta.mkdir(parents=True)
    e.salvar()
    e.concluir("relatorio", {})
    (e.pasta / "MAW-Sprint-07.pdf").write_bytes(b"%PDF-1.4 falso")
    (e.pasta / "achados.json").write_text(json.dumps(achados), encoding="utf-8")
    return e.pasta


def test_pacote_leva_o_pdf_e_so_os_achados_abertos(tmp_path):
    pasta = _sprint_com_pdf(tmp_path, [
        {"id": "MAW-0001", "estado": "novo", "titulo": "a", "severidade": "baixa", "alvos": [{"alvo": "main"}]},
        {"id": "MAW-0002", "estado": "corrigido", "titulo": "b", "severidade": "alta", "alvos": [{"alvo": "main"}]},
        {"id": "MAW-0003", "estado": "regressao", "titulo": "c", "severidade": "media", "alvos": [{"alvo": "main"}]}])
    (tmp_path / "entrega").mkdir()
    (tmp_path / "entrega" / "modelo-pedido.md").write_text("Sprint {sprint}: {n_abertos} ({ids}) em {pasta}",
                                                         encoding="utf-8")
    destino = N.montar_pacote(pasta, tmp_path / "entrega")
    assert destino == tmp_path / "entrega" / "sprint-07"
    assert (destino / "MAW-Sprint-07.pdf").read_bytes() == b"%PDF-1.4 falso"
    abertos = json.loads((destino / "achados-abertos.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in abertos] == ["MAW-0001", "MAW-0003"]
    assert "MAW-0003" in (destino / "achados-abertos.md").read_text(encoding="utf-8")
    assert (destino / "pedido.md").read_text(encoding="utf-8") == f"Sprint sprint-07: 2 (MAW-0001, MAW-0003) em {destino}"


def test_pacote_sem_modelo_nao_escreve_pedido_e_sem_pdf_nao_monta(tmp_path):
    pasta = _sprint_com_pdf(tmp_path, [{"id": "MAW-0001", "estado": "aberto", "titulo": "a", "alvos": []}])
    destino = N.montar_pacote(pasta, tmp_path / "entrega")
    assert (destino / "achados-abertos.json").exists() and not (destino / "pedido.md").exists()
    (pasta / "MAW-Sprint-07.pdf").unlink()
    assert N.montar_pacote(pasta, tmp_path / "entrega2") is None


def test_pacote_sem_achado_aberto_nao_monta(tmp_path):
    pasta = _sprint_com_pdf(tmp_path, [{"id": "MAW-0001", "estado": "corrigido", "titulo": "a", "alvos": []}])
    assert N.montar_pacote(pasta, tmp_path / "entrega") is None
