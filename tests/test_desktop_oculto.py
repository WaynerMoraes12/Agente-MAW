"""Desktop oculto: processos criados num desktop que nunca aparece na tela, com a árvore num job,
saída redigida no log e vigia do desktop do usuário. Os processos daqui são de console (sem janela)
ou a janela de teste (WinForms), criada só no desktop oculto."""
import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from maw_agent import desktop_oculto as do

POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
SCRIPT = Path(__file__).with_name("app_teste_gui.ps1")
NOME = f"AgenteMAW-teste-{os.getpid()}"
# Estes testes criam o próprio desktop oculto; lá dentro o job proíbe criar desktop.
pytestmark = pytest.mark.skipif(do.no_desktop_oculto(), reason="cria o próprio desktop oculto: rode fora dele")
PY = sys.executable


def _log(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _morto(pid: int, espera: float = 10.0) -> bool:
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    _, vivos = psutil.wait_procs([p], timeout=espera)
    return not vivos


def test_nome_invalido_e_caminho():
    for ruim in ("", "Default", "default", "a\\b", "a/b"):
        with pytest.raises(ValueError):
            do.DesktopOculto(ruim)
    assert do.DesktopOculto(NOME).caminho == f"{do.estacao()}\\{NOME}"


def test_dentro_do_oculto_pela_variavel(monkeypatch):
    monkeypatch.delenv(do.VARIAVEL, raising=False)
    assert not do.dentro_do_oculto()
    monkeypatch.setenv(do.VARIAVEL, "1")
    assert do.dentro_do_oculto()
    assert not do.no_desktop_oculto()  # a variável sozinha não basta: esta thread está no desktop do usuário


def test_desktop_existe_so_dentro_do_with():
    d = do.DesktopOculto(NOME)
    assert NOME not in do.desktops()
    with d:
        assert d.existe() and NOME in do.desktops()
        assert do.desktop_de_entrada() != NOME
    assert NOME not in do.desktops()  # sem processo nem handle, o Windows apaga o desktop


def test_executa_no_desktop_oculto_com_variavel_codigo_e_log(tmp_path):
    codigo_py = ("import ctypes, os, sys; from maw_agent import desktop_oculto as d; "
                 "print('desktop=' + d.desktop_da_thread()); print('var=' + os.environ.get(d.VARIAVEL, '')); "
                 "print('no_oculto=' + str(d.no_desktop_oculto())); "
                 "print('acentuação ok'); print('erro no stderr', file=sys.stderr); sys.exit(3)")
    log = tmp_path / "sub" / "saida.log"
    ecos: list[str] = []
    with do.DesktopOculto(NOME) as d:
        codigo = d.executar([PY, "-c", codigo_py], cwd=Path.cwd(), log=log, timeout=60, eco=ecos.append)
    linhas = _log(log)
    assert codigo == 3
    assert f"desktop={NOME}" in linhas and "var=1" in linhas and "no_oculto=True" in linhas
    assert "acentuação ok" in linhas and "erro no stderr" in linhas
    assert ecos == linhas
    assert d.execucoes[-1]["codigo"] == 3 and not d.execucoes[-1]["estourou"]


def test_saida_redigida_antes_do_disco(tmp_path):
    chave = "AIza" + "B" * 35  # formato de chave do Google, montada aqui para não existir no código
    log = tmp_path / "saida.log"
    with do.DesktopOculto(NOME) as d:
        d.executar([PY, "-c", f"print('chave={chave}')"], cwd=tmp_path, log=log, timeout=60)
    texto = log.read_text(encoding="utf-8")
    assert chave not in texto and "chave=[REDACTED]" in texto


def test_tempo_esgotado_mata_a_arvore(tmp_path):
    neto = "import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); " \
           "print('neto=' + str(p.pid), flush=True); time.sleep(120)"
    log = tmp_path / "saida.log"
    t0 = time.monotonic()
    with do.DesktopOculto(NOME) as d:
        codigo = d.executar([PY, "-c", neto], cwd=tmp_path, log=log, timeout=4)
    assert codigo == do.CODIGO_ESTOUROU
    assert time.monotonic() - t0 < 40
    pid_neto = int(next(l for l in _log(log) if l.startswith("neto=")).split("=")[1])
    assert _morto(pid_neto)
    assert d.execucoes[-1]["estourou"]
    assert any("tempo esgotado" in l for l in _log(log))


def test_o_que_sobra_da_arvore_morre_no_fim(tmp_path):
    solta = "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); " \
            "print('neto=' + str(p.pid), flush=True)"
    log = tmp_path / "saida.log"
    with do.DesktopOculto(NOME) as d:
        codigo = d.executar([PY, "-c", solta], cwd=tmp_path, log=log, timeout=60)
    assert codigo == 0
    pid_neto = int(next(l for l in _log(log) if l.startswith("neto=")).split("=")[1])
    assert _morto(pid_neto)
    assert any(str(pid_neto) in s for s in d.execucoes[-1]["sobras"])


def test_desktop_que_sumiu_nao_cria_processo(tmp_path, monkeypatch):
    chamadas = []
    monkeypatch.setattr(do, "CreateProcessW", lambda *a: chamadas.append(a) or 0)
    with do.DesktopOculto(NOME) as d:
        monkeypatch.setattr(do.DesktopOculto, "existe", lambda self: False)
        with pytest.raises(do.DesktopOcultoIndisponivel):
            d.executar([PY, "-c", "print(1)"], cwd=tmp_path, log=tmp_path / "l.log", timeout=5)
    assert chamadas == []


def test_desktop_na_tela_nao_cria_processo(tmp_path, monkeypatch):
    chamadas = []
    monkeypatch.setattr(do, "CreateProcessW", lambda *a: chamadas.append(a) or 0)
    with do.DesktopOculto(NOME) as d:
        monkeypatch.setattr(do, "desktop_de_entrada", lambda: NOME)
        with pytest.raises(do.DesktopOcultoIndisponivel):
            d.executar([PY, "-c", "print(1)"], cwd=tmp_path, log=tmp_path / "l.log", timeout=5)
    assert chamadas == []


def test_vigia_mata_na_hora_janela_no_desktop_do_usuario(tmp_path, monkeypatch):
    """Simulação (nenhuma janela real): o desktop do usuário "mostra" uma janela do processo do job."""
    alvo: dict = {}

    def falsas(self):
        return [{"hwnd": 1, "pid": alvo.get("pid", -1), "titulo": "janela falsa", "classe": "Falsa", "visivel": True}]

    monkeypatch.setattr(do.DesktopOculto, "_janelas_do_usuario", falsas)
    real_criar = do.DesktopOculto._criar

    def criar(self, *a, **kw):
        fd, h, pid = real_criar(self, *a, **kw)
        alvo["pid"] = pid
        return fd, h, pid

    monkeypatch.setattr(do.DesktopOculto, "_criar", criar)
    t0 = time.monotonic()
    with do.DesktopOculto(NOME) as d:
        with pytest.raises(do.JanelaNoDesktopDoUsuario) as e:
            d.executar([PY, "-c", "import time; time.sleep(60)"], cwd=tmp_path, log=tmp_path / "l.log", timeout=60)
    assert time.monotonic() - t0 < 20
    assert "janela falsa" in str(e.value)
    assert d.execucoes[-1]["violacao"] and d.execucoes[-1]["codigo"] == do.CODIGO_VIOLACAO
    assert _morto(alvo["pid"])


def test_janela_nasce_no_desktop_oculto_e_nunca_no_do_usuario(tmp_path):
    """Ponto 1 do brief, medido: a janela de teste (WinForms) está no desktop oculto e nenhuma janela
    do processo existe no desktop do usuário. Um terceiro processo (console) confere de dentro."""
    janela_log = tmp_path / "janela.log"
    janela_log.write_text("", encoding="utf-8")
    sonda = tmp_path / "sonda.json"
    confere = (
        "import json, sys, time\n"
        "from maw_agent import desktop_oculto as d\n"
        "fim = time.monotonic() + 30\n"
        "while time.monotonic() < fim and 'pronto' not in open(sys.argv[1], encoding='utf-8-sig').read():\n"
        "    time.sleep(0.1)\n"
        "time.sleep(0.5)\n"
        "oculto = [j for j in d.janelas_do_desktop(d.desktop_da_thread()) if j['titulo'] == 'Janela de teste GUI']\n"
        "json.dump({'desktop': d.desktop_da_thread(), 'oculto': oculto}, open(sys.argv[2], 'w'))\n"
    )
    import threading

    with do.DesktopOculto(NOME) as d:
        janela = threading.Thread(target=lambda: d.executar(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Log", str(janela_log)],
            cwd=tmp_path, log=tmp_path / "ps.log", timeout=90), daemon=True)
        janela.start()
        assert d.executar([PY, "-c", confere, str(janela_log), str(sonda)], cwd=Path.cwd(),
                          log=tmp_path / "sonda.log", timeout=60) == 0
        dados = json.loads(sonda.read_text(encoding="utf-8"))
        pids_ps = {j["pid"] for j in dados["oculto"]}
        do_usuario = [j for j in do.janelas_do_desktop(do.DESKTOP_DO_USUARIO) if j["pid"] in pids_ps]
    janela.join(20)
    assert dados["desktop"] == NOME
    assert dados["oculto"] and all(j["visivel"] for j in dados["oculto"])
    assert do_usuario == []
    assert d.violacoes == []
    assert all(_morto(pid) for pid in pids_ps)


def test_cli_repassa_saida_e_codigo(tmp_path, capsys):
    log = tmp_path / "cli.log"
    codigo = do.main(["--nome", NOME, "--timeout", "60", "--log", str(log), "--", PY, "-c",
                      "print('linha da cli'); raise SystemExit(5)"])
    assert codigo == 5
    saida = capsys.readouterr()
    assert "linha da cli" in saida.out
    assert "código 5" in saida.err and str(log) in saida.err
    assert "linha da cli" in _log(log)


def test_cli_sem_comando(capsys):
    assert do.main(["--nome", NOME]) == 2
    assert do.main(["--"]) == 2


def test_fuga_so_de_processo_novo_e_sem_titulo(tmp_path, monkeypatch):
    """Simulação: durante a execução aparecem uma janela de Explorer de um processo novo e uma janela nova
    de um navegador que já estava aberto. Só a primeira conta, e sem o título (é do usuário)."""
    import subprocess

    novo = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"], creationflags=subprocess.CREATE_NO_WINDOW)
    velho = psutil.Process()  # este processo nasceu antes da execução
    fase = {"n": 0}

    def falsas(self):
        fase["n"] += 1
        janelas = [{"hwnd": 10, "pid": velho.pid, "titulo": "Meet do usuário", "classe": "Chrome_WidgetWin_1",
                    "visivel": True}]
        if fase["n"] > 1:
            janelas += [{"hwnd": 11, "pid": novo.pid, "titulo": "Segredo do usuário", "classe": "CabinetWClass",
                         "visivel": True},
                        {"hwnd": 12, "pid": velho.pid, "titulo": "Outra reunião", "classe": "Chrome_WidgetWin_1",
                         "visivel": True}]
        return janelas

    try:
        # o "processo novo" nasceu antes deste teste: a execução começa a contar a partir dele
        monkeypatch.setattr(do.DesktopOculto, "_janelas_do_usuario", falsas)
        monkeypatch.setattr(do.time, "time", lambda: psutil.Process(novo.pid).create_time() - 1)
        relatorio: dict = {}
        codigo = do.rodar_oculto([PY, "-c", "import time; time.sleep(1.5)"], cwd=tmp_path, log=tmp_path / "l.log",
                                 timeout=30, nome=NOME, relatorio=relatorio)
    finally:
        novo.kill()
    assert codigo == 0
    assert [f["classe"] for f in relatorio["fugas"]] == ["CabinetWClass"]
    f = relatorio["fugas"][0]
    assert set(f) == {"classe", "processo", "quando"} and f["processo"].lower().startswith("python")
    texto = do.descrever_fuga(f) + repr(relatorio["fugas"])
    assert "Segredo" not in texto and "reunião" not in texto and "Meet" not in texto
    assert do.descrever_fuga(f).startswith("possível janela fora do desktop oculto: CabinetWClass (python")


def test_log_que_nao_abre_nao_deixa_processo(tmp_path, monkeypatch):
    chamadas = []
    monkeypatch.setattr(do, "CreateProcessW", lambda *a: chamadas.append(a) or 0)
    pasta_no_lugar_do_log = tmp_path / "log.txt"
    pasta_no_lugar_do_log.mkdir()
    with do.DesktopOculto(NOME) as d:
        with pytest.raises(OSError):
            d.executar([PY, "-c", "print(1)"], cwd=tmp_path, log=pasta_no_lugar_do_log, timeout=5)
    assert chamadas == [] and d._jobs == []
