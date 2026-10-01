"""Driver de GUI contra uma janela de teste do próprio Windows (WinForms pelo Windows PowerShell):
captura de janela coberta, teclas e cliques por PostMessage, menus, avisos, diálogo de arquivo,
travamento e o ciclo do ambiente (backup → pasta de teste → restauração conferida)."""
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import psutil
import pytest

from maw_agent import config, desktop_oculto, estado, gui, sandbox, suite, trava_appdata

POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
SCRIPT = Path(__file__).with_name("app_teste_gui.ps1")
TITULO = r"^Janela de teste GUI$"
COR_JANELA = (200, 30, 120)
# Testes que abrem a janela de teste: só dentro do desktop oculto (nada aparece na tela do usuário).
#   .venv/Scripts/python.exe -m maw_agent.desktop_oculto -- .venv/Scripts/python.exe -m pytest tests/test_gui.py -v
com_janela = pytest.mark.skipif(not desktop_oculto.no_desktop_oculto(),
                                reason="abre janela: rode dentro do desktop oculto (python -m maw_agent.desktop_oculto -- ...)")


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Pasta de configuração falsa com conteúdo do "usuário", backups e sprints em tmp."""
    appdata = tmp_path / "roaming" / "MAW"
    (appdata / "sub").mkdir(parents=True)
    (appdata / "MAW.settings").write_text('<?xml version="1.0"?>\n<PROPERTIES>\n  <VALUE name="doUsuario" val="1"/>\n'
                                          '</PROPERTIES>\n', encoding="utf-8")
    (appdata / "sub" / "arquivo.txt").write_text("do usuário", encoding="utf-8")
    monkeypatch.setattr(config, "APPDATA_MAW", appdata)
    monkeypatch.setattr(config, "BACKUPS", tmp_path / "local" / "backups")
    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")
    monkeypatch.setattr(config, "WORK", tmp_path / "work")  # a bandeira da calibração também é de mentira
    monkeypatch.setattr(gui, "app_aberta", lambda exe: False)
    monkeypatch.setattr(gui, "outra_instancia", lambda exe, pid: False)
    # outra trilha rodando um binário da MAW faria a restauração pendente ser adiada (fases)
    monkeypatch.setattr(suite, "maw_aberta", lambda: False)
    monkeypatch.delenv(gui.VARIAVEL_ENTRADA_REAL, raising=False)
    monkeypatch.delenv(gui.VARIAVEL_SOM, raising=False)
    # a guarda do dia (a sessão só abre no desktop oculto): os testes sem janela fingem estar lá
    monkeypatch.setattr(gui, "desktop_oculto", lambda: True)
    return appdata


def _sessao(tmp_path, *extra, **kw) -> gui.SessaoApp:
    log = tmp_path / "log.txt"
    log.write_text("", encoding="utf-8")
    kw.setdefault("titulo", TITULO)
    kw.setdefault("timeout_abrir", 40)
    kw.setdefault("espera_alertas", 0.3)
    return gui.SessaoApp(POWERSHELL, tmp_path / "evidencias", argumentos=[
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Log", str(log), *extra], **kw)


def _log(tmp_path) -> list[str]:
    p = tmp_path / "log.txt"
    return p.read_text(encoding="utf-8-sig").splitlines() if p.exists() else []


def _no_log(tmp_path, linha: str | re.Pattern, timeout: float = 5.0) -> bool:
    def achou():
        return any((re.search(linha, l) if isinstance(linha, re.Pattern) else l == linha) for l in _log(tmp_path))
    return bool(gui.esperar(achou, timeout, 0.1))


def _pronta(s, tmp_path) -> None:
    assert _no_log(tmp_path, "pronto", 20)


# ---------------------------------------------------------------- sem processo

def test_interpretar_teclas():
    t = gui.interpretar_teclas("ctrl+z")
    assert t.modificadores == (0x11,) and t.vk == ord("Z")
    t = gui.interpretar_teclas("Ctrl+Shift+S")
    assert t.modificadores == (0x11, 0x10) and t.vk == ord("S")
    t = gui.interpretar_teclas("k")
    assert t.modificadores == () and t.vk == ord("K") and t.caractere == "k"
    assert gui.interpretar_teclas("space").vk == 0x20
    assert gui.interpretar_teclas("f5").vk == 0x74
    assert gui.interpretar_teclas("delete").vk == 0x2E
    t = gui.interpretar_teclas("ctrl++")
    assert t.modificadores == (0x11,) and t.caractere == "+"
    assert gui.interpretar_teclas("+").caractere == "+"
    assert gui.interpretar_teclas(",").caractere == ","
    for ruim in ("", "hiper+x", "ctrl+", "tecla-que-nao-existe"):
        with pytest.raises(ValueError):
            gui.interpretar_teclas(ruim)


def test_settings_xml_escapa_e_mescla():
    texto = gui.settings_xml({"caminho": r'C:\a "b" & <c>', "n": 3})
    raiz = ET.fromstring(texto)
    assert {v.get("name"): v.get("val") for v in raiz.findall("VALUE")} == {"caminho": r'C:\a "b" & <c>', "n": "3"}
    mesclado = ET.fromstring(gui._mesclar_settings(texto, {"n": 4, "novo": "x"}))
    assert {v.get("name"): v.get("val") for v in mesclado.findall("VALUE")} == {
        "caminho": r'C:\a "b" & <c>', "n": "4", "novo": "x"}


def test_app_ja_aberta_nao_toca_no_ambiente(ambiente, tmp_path, monkeypatch):
    antes = sandbox.manifesto(ambiente)
    monkeypatch.setattr(gui, "app_aberta", lambda exe: True)
    with pytest.raises(gui.AppJaAberta):
        with _sessao(tmp_path):
            pass
    assert sandbox.manifesto(ambiente) == antes
    assert not config.BACKUPS.exists()


def test_exe_que_nao_inicia_restaura_ambiente_sem_janela(ambiente, tmp_path):
    """Ciclo do ambiente sem abrir janela: backup → bandeira → pasta de teste → falha ao iniciar →
    restauração conferida, bandeira e backup apagados."""
    antes = sandbox.manifesto(ambiente)
    s = gui.SessaoApp(tmp_path / "nao-existe.exe", tmp_path / "evidencias", settings_extra={"a": "b"})
    with pytest.raises(gui.AppNaoAbriu):
        with s:
            pass
    assert sandbox.manifesto(ambiente) == antes
    assert s.restauracao["verificado"] and s.restauracao["backup_apagado"]
    assert not (config.BACKUPS.parent / "appdata-sujo.json").exists()
    assert not any(config.BACKUPS.iterdir())


def test_retomada_em_sprint_sem_janela(ambiente, tmp_path):
    """Queda no meio da noite: a bandeira da sprint e o backup ficaram; a próxima sessão restaura o
    original antes do próprio backup e registra as duas restaurações no estado da sprint."""
    e = estado.nova_sprint(config.RELATORIOS)
    original = sandbox.manifesto(ambiente)
    backup = sandbox.backup_pasta(ambiente, config.BACKUPS)
    sandbox.escrever_json(e.pasta / "appdata-sujo.json", {"backup": str(backup), "desde": "x"})
    (ambiente / "MAW.settings").write_text("sujo da sessão que caiu", encoding="utf-8")
    (ambiente / "lixo.txt").write_text("lixo", encoding="utf-8")
    with pytest.raises(gui.AppNaoAbriu):
        with gui.SessaoApp(tmp_path / "nao-existe.exe", tmp_path / "evidencias"):
            pass
    assert sandbox.manifesto(ambiente) == original
    assert not (e.pasta / "appdata-sujo.json").exists() and not backup.exists()
    regs = estado.carregar(e.pasta).restauracoes
    assert [(r["quem"], r["verificado"], r["backup_apagado"]) for r in regs] == [
        ("retomada", True, True), ("gui", True, True)]


def test_pendencias_de_todos_os_lugares_mais_antiga_vence(ambiente, tmp_path):
    """A bandeira avulsa (desta sessão) e a da calibração: a mais antiga é restaurada, a mais nova
    descartada (o backup dela já pode ter copiado um estado sujo)."""
    original = sandbox.manifesto(ambiente)
    antigo = sandbox.backup_pasta(ambiente, config.BACKUPS)
    sandbox.escrever_json(config.BACKUPS.parent / "appdata-sujo.json",
                          {"backup": str(antigo), "desde": "2026-09-01T01:00:00", "quem": "gui"})
    (ambiente / "lixo.txt").write_text("sujo", encoding="utf-8")
    novo = sandbox.backup_pasta(ambiente, config.BACKUPS)
    sandbox.escrever_json(config.WORK / "calibracao" / "appdata-sujo.json",
                          {"backup": str(novo), "desde": "2026-09-02T01:00:00"})
    assert {p.name for p in gui.pendencias()} == {config.BACKUPS.parent.name, "calibracao"}
    regs = gui.restaurar_pendencias()
    assert [r["verificado"] for r in regs] == [True, True] and "descartado" in regs[1]
    assert sandbox.manifesto(ambiente) == original
    assert gui.pendencias() == [] and not antigo.exists() and not novo.exists()


def test_pasta_de_teste_limpa_preserva_e_mescla(ambiente, tmp_path):
    """Sem abrir processo: a pasta de teste montada pela sessão."""
    s = gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev", settings_extra={"k": "v"}, preservar=["sub"])
    s._montar_pasta_de_teste(ambiente)
    assert sorted(sandbox.manifesto(ambiente)) == ["MAW.settings", "sub/arquivo.txt"]
    raiz = ET.fromstring((ambiente / "MAW.settings").read_text(encoding="utf-8"))
    assert [(v.get("name"), v.get("val")) for v in raiz.findall("VALUE")] == [("k", "v")]
    s2 = gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev", settings_extra={"novo": "1"}, limpar=False)
    (ambiente / "MAW.settings").write_text(gui.settings_xml({"doUsuario": "1"}), encoding="utf-8")
    s2._montar_pasta_de_teste(ambiente)
    raiz = ET.fromstring((ambiente / "MAW.settings").read_text(encoding="utf-8"))
    assert {v.get("name"): v.get("val") for v in raiz.findall("VALUE")} == {"doUsuario": "1", "novo": "1"}


class _Registro:
    def __init__(self):
        self.chamadas: list[tuple] = []

    def __call__(self, nome):
        def f(*a):
            self.chamadas.append((nome, *a))
            return 1
        return f


def _sessao_falsa(tmp_path, monkeypatch, classe: str) -> tuple[gui.SessaoApp, _Registro]:
    """Sessão sem processo: janela falsa (hwnd 0x7FFF0001) e toda chamada Win32 que age registrada."""
    reg = _Registro()
    for nome in ("PostMessageW", "SendMessageTimeoutW", "AttachThreadInput", "SetKeyboardState", "SetForegroundWindow",
                 "SendInput", "SetCursorPos", "BringWindowToTop"):
        monkeypatch.setattr(gui, nome, reg(nome))
    monkeypatch.setattr(gui, "_classe", lambda h: classe)
    monkeypatch.setattr(gui, "GetAncestor", lambda h, f: h)
    monkeypatch.setattr(gui, "_thread_de", lambda h: 4242)
    monkeypatch.setattr(gui, "_pid_de", lambda h: 2)
    monkeypatch.setattr(gui.SessaoApp, "pid", property(lambda self: 2))
    s = gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev")
    monkeypatch.setattr(s, "_exigir_viva", lambda: None)
    monkeypatch.setattr(s, "_hwnd_de", lambda janela=None: janela if isinstance(janela, int) else 0x7FFF0001)
    monkeypatch.setattr(s, "_alvo_teclado", lambda h: h)
    monkeypatch.setattr(s, "_alvo_mouse", lambda h, x, y: (h, x, y))
    return s, reg


def test_modificador_em_janela_juce_recusa_sem_postar_nada(tmp_path, monkeypatch):
    monkeypatch.delenv(gui.VARIAVEL_ENTRADA_REAL, raising=False)
    s, reg = _sessao_falsa(tmp_path, monkeypatch, "JUCE_18f3a")
    with pytest.raises(gui.EntradaRealNecessaria) as e:
        s.teclas("ctrl+z")
    assert "GetAsyncKeyState" in str(e.value)
    with pytest.raises(gui.EntradaRealNecessaria):
        s.arrastar((1, 1), (5, 5), modificadores=("shift",))
    assert reg.chamadas == []
    s.teclas("k")  # sem modificador: PostMessage, como sempre
    assert [c[0] for c in reg.chamadas] == ["PostMessageW", "PostMessageW"]


def test_modificador_fora_da_juce_vai_so_no_estado_da_thread(tmp_path, monkeypatch):
    monkeypatch.delenv(gui.VARIAVEL_ENTRADA_REAL, raising=False)
    s, reg = _sessao_falsa(tmp_path, monkeypatch, "WindowsForms10.Window")
    s.teclas("ctrl+z")
    nomes = [c[0] for c in reg.chamadas]
    assert nomes[0] == "AttachThreadInput" and nomes[-1] == "AttachThreadInput"
    assert reg.chamadas[0][3] == 1 and reg.chamadas[-1][3] == 0  # liga e desliga
    assert nomes.count("SetKeyboardState") == 2  # liga o Ctrl e devolve o estado anterior
    assert {"SendInput", "SetCursorPos", "SetForegroundWindow"}.isdisjoint(nomes)
    postadas = [c for c in reg.chamadas if c[0] == "PostMessageW"]
    assert [(c[2], c[3]) for c in postadas] == [(gui.WM_KEYDOWN, ord("Z")), (gui.WM_KEYUP, ord("Z"))]


def test_sem_entrada_real_nada_muda_o_foco_nem_usa_teclado_e_mouse_reais(tmp_path, monkeypatch):
    """Fora do desktop oculto e sem MAW_AGENTE_ENTRADA_REAL: nenhuma chamada a SetForegroundWindow,
    BringWindowToTop, AttachThreadInput com outra thread, SendInput ou SetCursorPos."""
    monkeypatch.delenv(gui.VARIAVEL_ENTRADA_REAL, raising=False)
    monkeypatch.setattr(gui, "desktop_oculto", lambda: False)
    s, reg = _sessao_falsa(tmp_path, monkeypatch, "JUCE_18f3a")
    monkeypatch.setattr(gui, "GetForegroundWindow", lambda: 0x7FFF0099)  # uma janela do usuário na frente
    monkeypatch.setattr(gui, "_pid_de", lambda h: 1 if h == 0x7FFF0099 else 2)
    monkeypatch.setattr(gui.SessaoApp, "pid", property(lambda self: 2))
    monkeypatch.setattr(gui.SessaoApp, "hwnd", property(lambda self: 0x7FFF0001))
    monkeypatch.setattr(gui.SessaoApp, "botao", lambda self, *a, **k: pytest.fail("abriu o menu sem poder"))
    with pytest.raises(gui.EntradaRealNecessaria):
        s.trazer_para_frente()
    with pytest.raises(gui.EntradaRealNecessaria):
        s.abrir_menu("Arquivo", primeiro_plano=True)
    with pytest.raises(gui.EntradaRealNecessaria):
        s.menu("Arquivo", "Novo", primeiro_plano=True)
    s.teclas("k")
    s.digitar("ab")
    s.clicar(10, 10)
    s.clicar(10, 10, duplo=True)
    s.arrastar((1, 1), (9, 9))
    s._frente_anterior = 0x7FFF0099  # a janela do usuário continua na frente: nada a devolver
    s.devolver_primeiro_plano()
    proibidas = {"SetForegroundWindow", "BringWindowToTop", "AttachThreadInput", "SendInput", "SetCursorPos"}
    assert proibidas.isdisjoint(c[0] for c in reg.chamadas), reg.chamadas
    assert {c[0] for c in reg.chamadas} == {"PostMessageW"}


def test_no_desktop_oculto_primeiro_plano_nao_faz_nada(tmp_path, monkeypatch):
    monkeypatch.delenv(gui.VARIAVEL_ENTRADA_REAL, raising=False)
    monkeypatch.setattr(gui, "desktop_oculto", lambda: True)
    s, reg = _sessao_falsa(tmp_path, monkeypatch, "JUCE_18f3a")
    assert s.trazer_para_frente() is True
    assert reg.chamadas == []


def test_no_desktop_oculto_nunca_ha_entrada_real(monkeypatch):
    monkeypatch.setenv(gui.VARIAVEL_ENTRADA_REAL, "1")
    monkeypatch.setattr(gui, "desktop_oculto", lambda: True)
    assert not gui.entrada_real()
    monkeypatch.setattr(gui, "desktop_oculto", lambda: False)
    assert gui.entrada_real()


# ---------------------------------------------------------------- trava de sessão (sem janela)

def _sem_processo(monkeypatch):
    """Lançador falso: a sessão monta e restaura o ambiente, mas não abre processo nenhum."""
    monkeypatch.setattr(gui.SessaoApp, "_abrir", lambda self: None)


def _trava_livre() -> bool:
    """Confere de outra thread (a mesma thread entraria de novo na trava reentrante)."""
    caixa = {}
    t = threading.Thread(target=lambda: caixa.update(p=trava_appdata.adquirir(0)))
    t.start()
    t.join(10)
    if caixa.get("p") is not None:
        caixa["p"].liberar()
        return True
    return False


class _OutraSessao:
    """Segura a trava numa thread à parte, como outra sessão do agente."""

    def __enter__(self):
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


def test_trava_de_sessao_serializa_duas_sessoes(ambiente, tmp_path, monkeypatch, capsys):
    _sem_processo(monkeypatch)
    monkeypatch.setattr(trava_appdata, "PASSO", 0.05)
    antes = sandbox.manifesto(ambiente)
    marcas: dict[str, float] = {}
    a_dentro = threading.Event()

    def primeira():
        with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev1") as s:
            marcas["a_entrou"] = time.monotonic()
            a_dentro.set()
            time.sleep(1.0)
            marcas["a_saindo"] = time.monotonic()
        marcas["a_saiu"] = time.monotonic()
        assert s.restauracao["verificado"]

    def segunda():
        a_dentro.wait(10)
        marcas["b_pediu"] = time.monotonic()
        with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev2", espera_trava=30) as s:
            marcas["b_entrou"] = time.monotonic()
            assert sorted(sandbox.manifesto(ambiente)) == ["MAW.settings"]  # o ambiente de teste da B
        assert s.restauracao["verificado"]

    ts = [threading.Thread(target=primeira), threading.Thread(target=segunda)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert set(marcas) == {"a_entrou", "a_saindo", "a_saiu", "b_pediu", "b_entrou"}, marcas
    assert marcas["b_entrou"] >= marcas["a_saindo"]  # B só entrou depois que A começou a sair
    assert marcas["b_entrou"] - marcas["b_pediu"] >= 0.8  # e esperou por isso
    assert trava_appdata.AVISO in capsys.readouterr().err
    assert sandbox.manifesto(ambiente) == antes
    assert _trava_livre()


def test_trava_esgotada_vira_app_ja_aberta_sem_tocar_no_ambiente(ambiente, tmp_path, monkeypatch):
    _sem_processo(monkeypatch)
    monkeypatch.setattr(trava_appdata, "PASSO", 0.05)
    antes = sandbox.manifesto(ambiente)
    with _OutraSessao():
        s = gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev", espera_trava=0.3)
        with pytest.raises(gui.AppJaAberta) as e:
            with s:
                pass
        assert "outra sessão do agente está usando a MAW há mais de 0.3 s" in str(e.value)
        assert not s.esperando_trava
    assert sandbox.manifesto(ambiente) == antes
    assert not config.BACKUPS.exists() or not any(config.BACKUPS.iterdir())
    assert _trava_livre()


def test_trava_liberada_em_toda_saida(ambiente, tmp_path, monkeypatch):
    # aplicativo já aberto: recusa e solta a trava
    monkeypatch.setattr(gui, "app_aberta", lambda exe: True)
    with pytest.raises(gui.AppJaAberta):
        with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev"):
            pass
    assert _trava_livre()
    monkeypatch.setattr(gui, "app_aberta", lambda exe: False)
    # executável que não inicia: encerra, restaura e solta
    with pytest.raises(gui.AppNaoAbriu):
        with gui.SessaoApp(tmp_path / "nao-existe.exe", tmp_path / "ev"):
            pass
    assert _trava_livre()
    # exceção no meio do cenário
    _sem_processo(monkeypatch)
    with pytest.raises(RuntimeError):
        with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev"):
            assert not _trava_livre()  # presa durante a sessão
            raise RuntimeError("cenário quebrou")
    assert _trava_livre()


def test_trava_vale_entre_processos(ambiente, tmp_path, monkeypatch):
    """Outro processo segurando o arquivo de trava: a sessão espera e desiste no prazo."""
    import subprocess
    import sys

    _sem_processo(monkeypatch)
    monkeypatch.setattr(trava_appdata, "PASSO", 0.05)
    trava = trava_appdata.caminho_padrao()
    trava.parent.mkdir(parents=True, exist_ok=True)
    codigo = ("import msvcrt, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
              "msvcrt.locking(fd, msvcrt.LK_NBLCK, 1); print('presa', flush=True); time.sleep(30)")
    outro = subprocess.Popen([sys.executable, "-c", codigo, str(trava)], stdout=subprocess.PIPE, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert outro.stdout.readline().strip() == "presa"
        with pytest.raises(gui.AppJaAberta):
            with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev", espera_trava=0.4):
                pass
    finally:
        outro.kill()
        outro.wait(10)
    with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev", espera_trava=5):  # morto o outro, a trava é solta
        pass


def test_janelas_do_indicador_de_entrada_nao_contam(monkeypatch):
    classes = {1: "JUCE_1", 2: "UAC_InputIndicatorOverlayWnd", 3: "UAC Input Indicator", 4: "#32770"}
    monkeypatch.setattr(gui, "EnumWindows", lambda cb, lp: [cb(h, 0) for h in classes] and True)
    monkeypatch.setattr(gui, "_pid_de", lambda h: 99)
    monkeypatch.setattr(gui, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(gui, "_encoberta", lambda h: False)
    monkeypatch.setattr(gui, "_classe", lambda h: classes[h])
    assert gui.janelas_do_processo(99) == [1, 4]
    assert gui.janelas_do_processo(99, visiveis=False) == [1, 2, 3, 4]


# ---------------------------------------------------------------- fim da sessão (sem janela)

def test_outra_instancia_no_fim_adia_a_restauracao(ambiente, tmp_path, monkeypatch):
    """O usuário abriu o aplicativo enquanto o nosso fechava: nada é restaurado (ele gravaria por
    cima), bandeira e backup ficam, e a próxima retomada (com ele fechado) restaura."""
    _sem_processo(monkeypatch)
    original = sandbox.manifesto(ambiente)
    s = gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev", settings_extra={"k": "v"})
    with pytest.raises(gui.AmbienteNaoRestaurado) as e:
        with s:
            monkeypatch.setattr(gui, "outra_instancia", lambda exe, pid: True)
    assert "adiada" in str(e.value) and "outra instância" in str(e.value)
    assert s.restauracao["adiada"] and not s.restauracao["verificado"]
    bandeira = config.BACKUPS.parent / "appdata-sujo.json"
    assert bandeira.exists() and Path(s.restauracao["backup"]).exists()
    assert sorted(sandbox.manifesto(ambiente)) == ["MAW.settings"]  # ainda o ambiente de teste
    assert _trava_livre()
    monkeypatch.setattr(gui, "outra_instancia", lambda exe, pid: False)
    gui.restaurar_pendencias()
    assert sandbox.manifesto(ambiente) == original and not bandeira.exists()


def test_reabrir_recusa_com_outra_instancia_aberta(ambiente, tmp_path, monkeypatch):
    _sem_processo(monkeypatch)
    with pytest.raises(gui.AppJaAberta):
        with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev") as s:
            monkeypatch.setattr(gui, "outra_instancia", lambda exe, pid: True)
            s.reabrir()
    assert s.restauracao["adiada"]
    monkeypatch.setattr(gui, "outra_instancia", lambda exe, pid: False)
    gui.restaurar_pendencias()
    assert gui.pendencias() == []


def test_registro_que_falha_nao_impede_a_restauracao(ambiente, tmp_path, monkeypatch):
    _sem_processo(monkeypatch)
    antes = sandbox.manifesto(ambiente)

    def quebra(self):
        raise OSError("estado.json travado")

    with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev") as s:
        monkeypatch.setattr(gui.SessaoApp, "_estado_para_registro", quebra)
    assert s.restauracao["verificado"] and s.restauracao["backup_apagado"]
    assert "estado.json travado" in s.restauracao["erro_ao_registrar"]
    assert sandbox.manifesto(ambiente) == antes


def test_matar_sem_psutil_mata_pelo_popen_e_nao_diz_morta_se_viva(tmp_path, monkeypatch):
    import subprocess
    import sys

    s = gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev")
    s._proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    s._ps = None
    s._matar_arvore()
    assert s._proc.poll() is not None
    monkeypatch.setattr(gui.SessaoApp, "viva", lambda self: True)
    monkeypatch.setattr(gui.SessaoApp, "_matar_arvore", lambda self: None)
    monkeypatch.setattr(gui.SessaoApp, "_capturar_erro", lambda self, *a: "sem captura")
    assert s.derrubar()["modo"] == "nao_morreu"


def _pilha_z(monkeypatch, janelas: dict[int, tuple], ordem: list[int]):
    """Janelas falsas: {hwnd: (retângulo, classe, título)}; `ordem` de cima para baixo."""
    monkeypatch.setattr(gui, "IsWindow", lambda h: h in janelas)
    monkeypatch.setattr(gui, "IsIconic", lambda h: False)
    monkeypatch.setattr(gui, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(gui, "_encoberta", lambda h: False)
    monkeypatch.setattr(gui, "_retangulo", lambda h: janelas[h][0])
    monkeypatch.setattr(gui, "_classe", lambda h: janelas[h][1])
    monkeypatch.setattr(gui, "_texto", lambda h: janelas[h][2])

    def acima(h, cmd):
        assert cmd == gui.GW_HWNDPREV
        i = ordem.index(h)
        return ordem[i - 1] if i > 0 else 0

    monkeypatch.setattr(gui, "GetWindow", acima)


def test_copia_da_tela_so_sem_nada_por_cima(monkeypatch):
    app = ((100, 100, 500, 400), "JUCE_1", "App")
    # nada acima; acima mas sem cruzar: pode copiar
    _pilha_z(monkeypatch, {1: app}, [1])
    assert gui._visivel_na_tela(1)
    _pilha_z(monkeypatch, {1: app, 2: ((600, 0, 700, 50), "Outra", "x")}, [2, 1])
    assert gui._visivel_na_tela(1)
    # outra janela cruzando por cima: não
    _pilha_z(monkeypatch, {1: app, 2: ((450, 350, 800, 600), "Outra", "x")}, [2, 1])
    assert not gui._visivel_na_tela(1)
    # o fantasma dela (mesmo retângulo, título dela): pode
    _pilha_z(monkeypatch, {1: app, 2: ((100, 100, 500, 400), "Ghost", "App (Não Respondendo)")}, [2, 1])
    assert gui._visivel_na_tela(1)
    # o fantasma de outro aplicativo: não
    _pilha_z(monkeypatch, {1: app, 2: ((100, 100, 500, 400), "Ghost", "Outro (Não Respondendo)")}, [2, 1])
    assert not gui._visivel_na_tela(1)


def test_janela_de_outro_processo_e_recusada(tmp_path, monkeypatch):
    s = gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev")
    monkeypatch.setattr(gui.SessaoApp, "pid", property(lambda self: 10))
    monkeypatch.setattr(gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(gui, "_pid_de", lambda h: 10 if h == 0x7FFF0001 else 11)
    assert s._hwnd_de(0x7FFF0001) == 0x7FFF0001
    with pytest.raises(gui.ControleNaoEncontrado):
        s._hwnd_de(0x7FFF0002)


def test_de_dia_a_sessao_so_abre_no_desktop_oculto(ambiente, tmp_path, monkeypatch):
    """Fora do desktop oculto e sem entrada real (de dia, na tela do usuário): a sessão recusa antes de
    pegar a trava ou tocar no %APPDATA% da MAW, e nenhum processo é criado."""
    monkeypatch.setattr(gui, "desktop_oculto", lambda: False)
    monkeypatch.setattr(gui.subprocess, "Popen", lambda *a, **k: pytest.fail("criou processo na tela do usuário"))
    pedidos = []
    monkeypatch.setattr(trava_appdata, "adquirir", lambda *a, **k: pedidos.append(a) and None)
    antes = sandbox.manifesto(ambiente)
    with pytest.raises(gui.EntradaRealNecessaria) as e:
        with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev"):
            pass
    assert "a MAW só abre no desktop oculto ou na execução noturna" in str(e.value)
    assert sandbox.manifesto(ambiente) == antes and not config.BACKUPS.exists() and pedidos == []
    monkeypatch.setenv(gui.VARIAVEL_ENTRADA_REAL, "1")  # à noite, com entrada real, passa da guarda
    with pytest.raises(gui.AppJaAberta):  # e vai pedir a trava (aqui: nunca livre)
        with gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev", espera_trava=0):
            pass
    assert len(pedidos) == 1


# ---------------------------------------------------------------- som, itens proibidos, a MAW do usuário

def test_com_aviso_aberto_entrada_na_janela_de_tras_e_recusada(tmp_path, monkeypatch):
    monkeypatch.delenv(gui.VARIAVEL_ENTRADA_REAL, raising=False)
    monkeypatch.delenv(gui.VARIAVEL_SOM, raising=False)
    monkeypatch.setattr(gui, "desktop_oculto", lambda: True)
    s, reg = _sessao_falsa(tmp_path, monkeypatch, "WindowsForms10.Window")
    aviso = 0x7FFF0050
    monkeypatch.setattr(s, "_modais", lambda: [aviso])
    monkeypatch.setattr(gui, "_texto", lambda h: "Aviso de teste")
    for acao in (lambda: s.clicar(5, 5), lambda: s.teclas("k"), lambda: s.digitar("a"),
                 lambda: s.arrastar((1, 1), (5, 5))):
        with pytest.raises(gui.AlertaAberto) as e:
            acao()
        assert "som de alerta" in str(e.value) and "Aviso de teste" in str(e.value)
    assert reg.chamadas == []

    class Info:  # elemento UIA da janela principal (acha a janela de topo pelo handle)
        name, handle, parent = "Botão", 0x7FFF0001, None

    with pytest.raises(gui.AlertaAberto):
        gui.Controle(Info(), s).invocar()
    s.teclas("k", janela=aviso)  # no próprio aviso, pode
    assert [c[0] for c in reg.chamadas] == ["PostMessageW", "PostMessageW"]
    monkeypatch.setenv(gui.VARIAVEL_SOM, "1")  # à noite, com som liberado, pode
    s.clicar(5, 5)


def test_itens_proibidos_lidos_do_privado(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "PRIVADO", tmp_path / "privado")
    gui._proibidos.clear()
    assert gui.itens_proibidos() == [] and gui.item_proibido("Abrir a pasta") is None
    assert "sem lista de itens proibidos" in capsys.readouterr().err
    arq = tmp_path / "privado" / "bancada" / "itens-proibidos.yaml"
    arq.parent.mkdir(parents=True)
    arq.write_text('itens:\n  - rotulo: "Abrir a pasta de coisas"\n    motivo: "abre o Explorer"\n', encoding="utf-8")
    assert gui.item_proibido("&Abrir a pasta de coisas...")["motivo"] == "abre o Explorer"
    assert gui.item_proibido("Abrir") is None
    gui._proibidos.clear()


def test_item_proibido_nunca_e_acionado_de_dia(tmp_path, monkeypatch):
    monkeypatch.delenv(gui.VARIAVEL_ENTRADA_REAL, raising=False)
    monkeypatch.setattr(gui, "desktop_oculto", lambda: True)
    monkeypatch.setattr(gui, "itens_proibidos", lambda: [{"rotulo": "Abrir a pasta de coisas", "motivo": "Explorer"}])
    s, reg = _sessao_falsa(tmp_path, monkeypatch, "JUCE_1")
    monkeypatch.setattr(s, "_modais", lambda: [])

    class Info:
        def __init__(self, nome):
            self.name, self.handle, self.parent = nome, 0x7FFF0001, None

    monkeypatch.setattr(s, "_itens_abertos", lambda: [Info("Abrir a pasta de coisas"), Info("Abrir projeto")])
    with pytest.raises(gui.ItemProibido):
        s._item("Abrir a pasta de coisas")
    assert s._item("Abrir").nome == "Abrir projeto"  # o prefixo pula o proibido
    with pytest.raises(gui.ItemProibido):
        gui.Controle(Info("Abrir a pasta de coisas"), s).invocar()
    monkeypatch.setattr(s, "_itens_abertos", lambda: [Info("Abrir a pasta de coisas")])
    with pytest.raises(gui.ControleNaoEncontrado):
        s._item("Abrir")
    monkeypatch.setattr(gui, "entrada_real", lambda: True)  # à noite, com entrada real, pode
    assert s._item("Abrir a pasta de coisas").nome == "Abrir a pasta de coisas"


def test_salvar_por_cima_sem_som(tmp_path, monkeypatch):
    monkeypatch.delenv(gui.VARIAVEL_SOM, raising=False)
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    s, _ = _sessao_falsa(tmp_path, monkeypatch, "JUCE_1")
    monkeypatch.setattr(s, "_capturar_erro", lambda *a: "sem captura")
    fora = tmp_path / "fora.txt"
    fora.write_text("x", encoding="utf-8")
    with pytest.raises(gui.SomNecessario):
        s.dialogo_arquivo(fora, salvar=True, timeout=0.2)
    assert fora.exists()
    dentro = tmp_path / "work" / "e2e" / "saida.maw"
    dentro.parent.mkdir(parents=True)
    dentro.write_text("x", encoding="utf-8")
    with pytest.raises(gui.ControleNaoEncontrado):  # sem diálogo de verdade aqui
        s.dialogo_arquivo(dentro, salvar=True, timeout=0.2)
    assert not dentro.exists()  # em work/: apagado antes, sem a pergunta de substituir


def test_janela_segura_nao_entrega_entrada_real(monkeypatch):
    class Falso:
        nome = "x"

        def click_input(self):
            return "clicou"

        def texto(self):
            return "ok"

    monkeypatch.delenv(gui.VARIAVEL_ENTRADA_REAL, raising=False)
    j = gui.JanelaSegura(Falso())
    assert j.nome == "x" and j.texto() == "ok"
    for metodo in ("click_input", "type_keys", "set_focus", "draw_outline", "capture_as_image"):
        with pytest.raises(gui.EntradaRealNecessaria):
            getattr(j, metodo)
    monkeypatch.setattr(gui, "entrada_real", lambda: True)
    assert j.click_input() == "clicou"


def test_maw_do_usuario_durante_a_sessao_encerra_e_restaura(ambiente, tmp_path, monkeypatch):
    _sem_processo(monkeypatch)
    monkeypatch.setattr(gui, "PASSO_ESTRANGEIRA", 0.05)
    antes = sandbox.manifesto(ambiente)
    vistos = {"n": 0}

    def outra(exe, nossos):
        vistos["n"] += 1
        return 999999 if vistos["n"] == 3 else None  # aparece uma vez, some depois (a instância única)

    monkeypatch.setattr(gui, "outra_instancia", outra)
    monkeypatch.setattr(gui.SessaoApp, "viva", lambda self: not self._encerrada)
    monkeypatch.setattr(gui.SessaoApp, "_filhos", lambda self: [])
    s = gui.SessaoApp(tmp_path / "x.exe", tmp_path / "ev")
    with pytest.raises(gui.AppDoUsuarioAberto):
        with s:
            assert gui.esperar(lambda: s.maw_estrangeira, 5, 0.05)
            s.botao("Qualquer")
    assert s.maw_estrangeira["pid"] == 999999
    assert s.restauracao["verificado"] and sandbox.manifesto(ambiente) == antes
    assert _trava_livre()


def test_esperar_devolve_valor_ou_falso():
    assert gui.esperar(lambda: 7, 1) == 7
    t0 = time.monotonic()
    assert gui.esperar(lambda: 0, 0.3, 0.05) == 0
    assert time.monotonic() - t0 >= 0.3


# ---------------------------------------------------------------- com a janela de teste

@com_janela
def test_ciclo_ambiente_teclas_e_fechamento_com_pergunta(ambiente, tmp_path):
    antes = sandbox.manifesto(ambiente)
    with _sessao(tmp_path, settings_extra={"caminhoTeste": r"C:\x y\z.exe"}) as s:
        _pronta(s, tmp_path)
        # ambiente de teste: bandeira, pasta limpa só com a configuração de teste
        bandeira = config.BACKUPS.parent / "appdata-sujo.json"
        assert bandeira.exists()
        assert sorted(sandbox.manifesto(ambiente)) == ["MAW.settings"]
        raiz = ET.fromstring((ambiente / "MAW.settings").read_text(encoding="utf-8"))
        assert [(v.get("name"), v.get("val")) for v in raiz.findall("VALUE")] == [("caminhoTeste", r"C:\x y\z.exe")]
        assert s.viva() and s.respondendo()
        assert "Janela de teste GUI" in s.janelas()
        assert s.janela().element_info.name == "Janela de teste GUI"
        # teclas por PostMessage, com a janela no fundo
        s.teclas("k")
        s.teclas("f5")
        s.teclas("delete")
        assert _no_log(tmp_path, "down:K:None")
        assert _no_log(tmp_path, "char:107")
        assert _no_log(tmp_path, "down:F5:None")
        assert _no_log(tmp_path, "down:Delete:None")
        s.digitar("ab")
        assert _no_log(tmp_path, "char:97") and _no_log(tmp_path, "char:98")
        # modificador sem teclado real: só no estado de teclado da thread da janela (GetKeyState)
        s.teclas("ctrl+z")
        assert _no_log(tmp_path, "down:Z:Control") and _no_log(tmp_path, "char:26")
        s.teclas("x")  # o estado anterior volta: sem Ctrl
        assert _no_log(tmp_path, "down:X:None")
    # a janela perguntou se salva (houve tecla): respondido "Não", fechou com graça
    f = s.fechamentos[-1]
    assert f["modo"] == "graciosa", f
    assert any("Salvar as alteracoes" in a for a in f["alertas"]), f
    assert _no_log(tmp_path, "fechar:No")
    assert s.restauracao["verificado"] and s.restauracao["backup_apagado"]
    assert sandbox.manifesto(ambiente) == antes
    assert not (config.BACKUPS.parent / "appdata-sujo.json").exists()
    assert not any(config.BACKUPS.iterdir())
    assert not psutil.pid_exists(s.pid) or psutil.Process(s.pid).status() == psutil.STATUS_ZOMBIE


@com_janela
def test_mouse_por_postmessage(ambiente, tmp_path):
    with _sessao(tmp_path) as s:
        _pronta(s, tmp_path)
        s.clicar(300, 200)
        assert _no_log(tmp_path, "mousedown:Left:300,200:1")
        assert _no_log(tmp_path, "mouseup:Left:300,200")
        s.clicar(310, 210, duplo=True)
        assert _no_log(tmp_path, "mousedown:Left:310,210:2")
        s.clicar(320, 220, botao="direito")
        assert _no_log(tmp_path, "mousedown:Right:320,220:1")
        s.arrastar((100, 150), (250, 250))
        assert _no_log(tmp_path, "mousedown:Left:100,150:1")
        assert _no_log(tmp_path, "drag:175,200")  # ponto do meio
        assert _no_log(tmp_path, "drag:250,250")
        assert _no_log(tmp_path, "mouseup:Left:250,250")
        s.arrastar((100, 150), (200, 200), modificadores=("shift",))
        assert _no_log(tmp_path, "mods:Shift")
        assert sum(l == "mouseup:Left:200,200" for l in _log(tmp_path)) == 1
        with pytest.raises(ValueError):
            s.clicar(1, 1, botao="quarto")


@com_janela
def test_captura_de_janela_coberta(ambiente, tmp_path):
    from PIL import Image

    with _sessao(tmp_path, "-Cobrir") as s:
        _pronta(s, tmp_path)
        assert s.janela_por_titulo(r"^Capa de teste$", 10)
        assert set(s.janelas()) >= {"Janela de teste GUI", "Capa de teste"}
        arq = s.captura("coberta")
        assert arq.parent == tmp_path / "evidencias" and arq.suffix == ".png"
        img = Image.open(arq).convert("RGB")
        w, h = img.size
        assert w > 400 and h > 250
        pixel = img.getpixel((w * 3 // 4, h * 3 // 4))  # área sem controles: a cor da janela, não a da capa
        assert all(abs(a - b) <= 8 for a, b in zip(pixel, COR_JANELA)), pixel
        # a capa também se captura pelo título
        capa = Image.open(s.captura("capa", janela=r"^Capa de teste$")).convert("RGB")
        assert all(abs(a - b) <= 8 for a, b in zip(capa.getpixel((capa.width // 2, capa.height // 2)), (20, 200, 40)))
        assert s.captura("coberta").name == "coberta-2.png"


@com_janela
def test_botoes_alternar_e_valores(ambiente, tmp_path):
    with _sessao(tmp_path) as s:
        _pronta(s, tmp_path)
        nomes = {c.nome for c in s.botoes()}
        assert {"Abrir menu", "Aviso", "Salvar", "Travar", "Opcao"} <= nomes
        opcao = s.botao("opcao")  # nome comparado sem caixa
        assert opcao.valor() is False
        opcao.alternar()
        assert _no_log(tmp_path, "opcao:True")
        assert gui.esperar(lambda: opcao.valor() is True, 3)
        assert s.botao(re.compile(r"^Abrir")).nome == "Abrir menu"
        nivel = next(c for c in s.controles("Slider") if c.nome == "Nivel")
        assert float(nivel.valor()) == 25.0  # TrackBar pelo proxy do MSAA: padrão Value (texto)
        with pytest.raises(gui.ControleNaoEncontrado) as e:
            s.botao("Nao existe")
        assert "Abrir menu" in str(e.value)


@com_janela
def test_menus_por_uia(ambiente, tmp_path):
    with _sessao(tmp_path) as s:
        _pronta(s, tmp_path)
        assert s.abrir_menu("Abrir menu") == ["Primeiro", "Submenu"]
        assert s.fechar_menus()
        assert s.abrir_menu("Abrir menu", "Submenu") == ["Interno"]
        assert s.fechar_menus()
        s.menu("Abrir menu", "Submenu", "Interno")
        assert _no_log(tmp_path, "menu:Interno")
        s.menu("Abrir menu", "primeiro")
        assert _no_log(tmp_path, "menu:Primeiro")
        with pytest.raises(gui.ControleNaoEncontrado):
            s.menu("Abrir menu", "Inexistente")
        s.fechar_menus()


@com_janela
def test_alerta_lido_e_respondido(ambiente, tmp_path):
    with _sessao(tmp_path) as s:
        _pronta(s, tmp_path)
        assert s.alertas() == []
        s.botao("Aviso").invocar()
        textos = s.esperar(s.alertas, 5)
        assert textos and "Aviso de teste" in textos[0] and "Mensagem de teste do aviso" in textos[0]
        texto = s.responder_alerta(re.compile(r"^(Cancelar|Cancel)$"))
        assert "Mensagem de teste do aviso" in texto
        assert _no_log(tmp_path, "aviso:Cancel")
        assert gui.esperar(lambda: s.alertas() == [], 3)
        with pytest.raises(gui.ControleNaoEncontrado):
            s.responder_alerta("OK", timeout=0.3)


@com_janela
@pytest.mark.skipif(os.environ.get("MAW_AGENTE_SOM") != "1",
                    reason="a pergunta 'substituir?' do Windows toca som: só com MAW_AGENTE_SOM=1 (à noite)")
def test_dialogo_de_arquivo_salvar_e_substituir(ambiente, tmp_path):
    destino = tmp_path / "saida teste.txt"
    with _sessao(tmp_path) as s:
        _pronta(s, tmp_path)
        s.botao("Salvar").invocar()
        s.dialogo_arquivo(destino, salvar=True)
        assert _no_log(tmp_path, f"salvo:{destino}")
        assert gui.esperar(destino.exists, 5)
        s.botao("Salvar").invocar()
        s.dialogo_arquivo(destino, salvar=True)  # agora pergunta se substitui: Sim
        assert gui.esperar(lambda: sum(l == f"salvo:{destino}" for l in _log(tmp_path)) == 2, 5)


@com_janela
def test_travamento_detectado_e_sessao_mata(ambiente, tmp_path, monkeypatch):
    antes = sandbox.manifesto(ambiente)
    monkeypatch.setattr(gui, "LIMITE_TRAVADA", 1.0)
    with pytest.raises(gui.AppTravou) as e:
        with _sessao(tmp_path) as s:
            _pronta(s, tmp_path)
            s.botao("Travar").invocar()
            assert gui.esperar(lambda: not s.respondendo(), 15, 0.5)
            s.esperar(lambda: False, 30)
    # janela travada não pinta para o PrintWindow: com ela à mostra copia-se a tela; coberta, o motivo
    assert (e.value.captura is not None and e.value.captura.exists()) or "coberta" in e.value.sem_captura
    f = s.fechamentos[-1]
    assert f["modo"] == "morta"
    assert not s.viva()
    assert sandbox.manifesto(ambiente) == antes and s.restauracao["verificado"]


@com_janela
def test_nao_abriu_mata_e_restaura(ambiente, tmp_path):
    antes = sandbox.manifesto(ambiente)
    s = _sessao(tmp_path, titulo=r"^titulo que nunca aparece$", timeout_abrir=6)
    with pytest.raises(gui.AppNaoAbriu) as e:
        s.__enter__()
    assert "Janela de teste GUI" in str(e.value)
    assert not s.viva()
    assert sandbox.manifesto(ambiente) == antes
    assert not (config.BACKUPS.parent / "appdata-sujo.json").exists()


@com_janela
def test_matar_de_outra_thread_e_saida_idempotente(ambiente, tmp_path):
    antes = sandbox.manifesto(ambiente)
    s = _sessao(tmp_path)
    s.__enter__()
    _pronta(s, tmp_path)
    t = threading.Thread(target=s.matar)
    t.start()
    t.join(30)
    assert not s.viva()
    assert sandbox.manifesto(ambiente) == antes
    s.__exit__(None, None, None)  # nada de novo: já encerrada
    assert len(s.fechamentos) == 1 and s.restauracao["verificado"]
    with pytest.raises(gui.ErroApp):
        s.botao("Aviso")


@com_janela
def test_derrubar_e_reabrir_no_mesmo_ambiente(ambiente, tmp_path):
    antes = sandbox.manifesto(ambiente)
    with _sessao(tmp_path, settings_extra={"k": "v"}) as s:
        _pronta(s, tmp_path)
        pid1 = s.pid
        s.derrubar()
        assert not s.viva()
        with pytest.raises(gui.AppTravou):
            s.botao("Aviso")
        assert (ambiente / "MAW.settings").exists()  # o ambiente de teste continua
        (tmp_path / "log.txt").write_text("", encoding="utf-8")
        s.reabrir()
        _pronta(s, tmp_path)
        assert s.viva() and s.pid != pid1
    assert [f["modo"] for f in s.fechamentos] == ["morta", "graciosa"]
    assert sandbox.manifesto(ambiente) == antes


@com_janela
def test_retomada_restaura_bandeira_de_sessao_que_caiu(ambiente, tmp_path):
    """Queda no meio de uma sessão avulsa: a bandeira e o backup ficaram; a próxima sessão restaura
    o original antes de fazer o próprio backup."""
    original = sandbox.manifesto(ambiente)
    backup = sandbox.backup_pasta(ambiente, config.BACKUPS)
    sandbox.escrever_json(config.BACKUPS.parent / "appdata-sujo.json", {"backup": str(backup), "desde": "x"})
    (ambiente / "MAW.settings").write_text("sujo da sessão que caiu", encoding="utf-8")
    (ambiente / "lixo.txt").write_text("lixo", encoding="utf-8")
    with _sessao(tmp_path) as s:
        _pronta(s, tmp_path)
    assert sandbox.manifesto(ambiente) == original
    assert not backup.exists()


@com_janela
def test_retomada_em_sprint_registra_no_estado(ambiente, tmp_path):
    e = estado.nova_sprint(config.RELATORIOS)
    original = sandbox.manifesto(ambiente)
    backup = sandbox.backup_pasta(ambiente, config.BACKUPS)
    sandbox.escrever_json(e.pasta / "appdata-sujo.json", {"backup": str(backup), "desde": "x"})
    (ambiente / "lixo.txt").write_text("lixo", encoding="utf-8")
    with _sessao(tmp_path) as s:  # a sprint em andamento é achada sozinha
        _pronta(s, tmp_path)
        assert (e.pasta / "appdata-sujo.json").exists()  # a bandeira desta sessão
        assert not (config.BACKUPS.parent / "appdata-sujo.json").exists()
    assert sandbox.manifesto(ambiente) == original
    assert not (e.pasta / "appdata-sujo.json").exists()
    regs = estado.carregar(e.pasta).restauracoes
    assert [(r["quem"], r["verificado"], r["backup_apagado"]) for r in regs] == [
        ("retomada", True, True), ("gui", True, True)]


def test_restauracao_pendente_que_falha_recusa_a_sessao(ambiente, tmp_path):
    sandbox.escrever_json(config.BACKUPS.parent / "appdata-sujo.json",
                          {"backup": str(tmp_path / "backup-que-nao-existe"), "desde": "x"})
    with pytest.raises(gui.AmbienteSujo):
        with _sessao(tmp_path):
            pass
    assert (ambiente / "sub" / "arquivo.txt").exists()



def test_fracao_cor_dominante_distingue_janela_vazia_de_janela_com_conteudo():
    from PIL import Image, ImageDraw
    vazia = Image.new("RGB", (800, 600), (0, 0, 0))
    assert gui.fracao_cor_dominante(vazia) >= gui.LIMITE_CAPTURA_VAZIA
    com_texto = Image.new("RGB", (800, 600), (30, 20, 40))
    d = ImageDraw.Draw(com_texto)
    for y in range(40, 560, 40):
        d.rectangle((40, y, 760, y + 12), fill=(200, 180, 255))
    assert gui.fracao_cor_dominante(com_texto) < gui.LIMITE_CAPTURA_VAZIA


def test_tecla_real_com_modificador_segura_o_ctrl_em_lote_proprio(monkeypatch):
    """A JUCE lê os modificadores pelo estado físico (GetAsyncKeyState) quando trata a tecla: Ctrl↓ C↓ C↑ Ctrl↑ num
    único SendInput chega como C sem Ctrl. O modificador desce num lote, a tecla em outro e o modificador sobe depois."""
    lotes, pausas = [], []
    monkeypatch.setattr(gui, "_enviar", lambda entradas: lotes.append(
        [(e.u.ki.wVk, bool(e.u.ki.dwFlags & gui.KEYEVENTF_KEYUP)) for e in entradas]))
    monkeypatch.setattr(gui.time, "sleep", lambda s: pausas.append(s))
    gui._enviar_tecla_real(gui.interpretar_teclas("ctrl+c"))
    assert lotes == [[(0x11, False)], [(ord("C"), False), (ord("C"), True)], [(0x11, True)]]
    assert len(pausas) == 2 and all(p >= 0.03 for p in pausas)


def test_tecla_real_sem_modificador_vai_num_lote_so(monkeypatch):
    lotes = []
    monkeypatch.setattr(gui, "_enviar", lambda entradas: lotes.append(len(entradas)))
    monkeypatch.setattr(gui.time, "sleep", lambda s: None)
    gui._enviar_tecla_real(gui.interpretar_teclas("t"))
    assert lotes == [2]
