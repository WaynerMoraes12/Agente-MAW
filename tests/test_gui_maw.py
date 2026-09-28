"""Driver de GUI contra o aplicativo real (alvo `main`, Release). Lento. Roda dentro do desktop oculto
(nada aparece na tela do usuário; lá os menus da JUCE ficam abertos sem primeiro plano):
    .venv/Scripts/python.exe -m maw_agent.desktop_oculto -- .venv/Scripts/python.exe -m pytest tests/test_gui_maw.py -m lento -v
ou à noite com MAW_AGENTE_ENTRADA_REAL=1. Nunca com a instância do usuário aberta."""
import contextlib
import json

import pytest

from maw_agent import config, gui, sandbox, trava_appdata

EXE = config.ALVOS_DIR / "main" / "Builds" / "VisualStudio2022" / "x64" / "Release" / "App" / "MAW_APP.exe"
# o rótulo do botão do menu principal é conhecimento do aplicativo: vem do privado
BANCADA = config.PRIVADO / "bancada" / "gui-maw-main.json"


def _botao_do_menu() -> str | None:
    try:
        return json.loads(BANCADA.read_text(encoding="utf-8"))["menu"]["botao"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


@pytest.mark.lento
def test_app_real_abre_lista_botoes_menu_captura_fecha_e_restaura(tmp_path, monkeypatch):
    from PIL import Image

    if not (gui.desktop_oculto() or gui.entrada_real()):
        pytest.skip("abre o aplicativo: rode dentro do desktop oculto (python -m maw_agent.desktop_oculto -- ...)")
    if not EXE.exists():
        pytest.skip(f"executável não compilado: {EXE}")
    botao_menu = _botao_do_menu()
    if not botao_menu:
        pytest.skip(f"sem o rótulo do botão do menu em {BANCADA}")
    # a trava de verdade: as outras sessões do agente na máquina usam esta
    monkeypatch.setattr(trava_appdata, "caminho_padrao", trava_appdata._caminho_real)
    # e a restauração de verdade (o conftest recusa restaurar a pasta real em qualquer outro teste)
    monkeypatch.setattr(sandbox, "restaurar_pasta", sandbox.restaurar_pasta.__wrapped__)
    # a sessão de teste não entra no estado de uma sprint real (bandeira avulsa, fora do repositório)
    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")
    evid = tmp_path / "evidencias"
    # outras sessões do agente (outros cenários): a trava de sessão espera por elas; a pasta de
    # configuração é conferida pela própria sessão contra o manifesto do backup
    s = gui.SessaoApp(EXE, evid, timeout_abrir=90, espera_trava=1200)
    try:
        s.__enter__()
    except gui.AppJaAberta as ex:
        pytest.skip(f"aplicativo ocupado: {ex}")
    with contextlib.ExitStack() as pilha:
        pilha.push(s)  # só o __exit__: a sessão já foi aberta acima
        assert s.viva() and s.respondendo()
        nomes = [c.nome for c in s.botoes() if c.nome]
        assert botao_menu in nomes, nomes
        try:
            itens = s.abrir_menu(botao_menu)
            assert len(itens) >= 5, itens
            assert s.fechar_menus()
        finally:
            s.devolver_primeiro_plano()
        cap = s.captura("principal")
        img = Image.open(cap).convert("RGB")
        assert img.width >= 800 and img.height >= 500
        assert len(img.getcolors(maxcolors=1 << 20) or []) > 16  # não é uma imagem chapada (preta/branca)
        sandbox.escrever_json(evid / "gui-maw.json", {"botoes": nomes, "itens_menu": itens,
                                                      "janelas": s.janelas(), "alertas_abertura": s.alertas_abertura})
        print(json.dumps({"botoes": nomes, "itens_menu": itens}, ensure_ascii=False))
    assert s.fechamentos[-1]["modo"] == "graciosa", s.fechamentos
    assert s.restauracao["verificado"] and s.restauracao["backup_apagado"], s.restauracao
