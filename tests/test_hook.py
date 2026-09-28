"""Tests para o modulo de hook de pre-commit."""
from maw_agent import hook

TERMOS = ["ClasseSecreta", "re:Segredo[A-Z]\\w+"]


def test_publico_bloqueia_caminhos_de_resultado():
    v = hook.verificar({"privado/a.md": "", "work/x": "", "relatorios/s/a.pdf": ""}, "publico", TERMOS)
    assert len(v) == 3


def test_publico_bloqueia_termo_interno():
    v = hook.verificar({"maw_agent/x.py": "usa ClasseSecreta aqui"}, "publico", TERMOS)
    assert v and "ClasseSecreta" in v[0]


def test_publico_bloqueia_termo_regex():
    assert hook.verificar({"a.md": "SegredoMotor"}, "publico", TERMOS)


def test_publico_sem_lista_recusa():
    v = hook.verificar({"a.md": "nada"}, "publico", None)
    assert v and "termos" in v[0]


def test_bloqueia_chave_nos_dois():
    chave = "AIza" + "C" * 35
    for repo in ("publico", "privado"):
        assert hook.verificar({"a.txt": chave}, repo, TERMOS)


def test_privado_aceita_pdf_mas_nao_evidencia_bruta():
    assert hook.verificar({"relatorios/sprint-01/MAW-Sprint-01.pdf": ""}, "privado", None) == []
    assert hook.verificar({"relatorios/sprint-01/evidencias/a.png": ""}, "privado", None)


def test_privado_bloqueia_settings_e_chave_em_arquivo():
    assert hook.verificar({"x/MAW.settings": ""}, "privado", None)
    assert hook.verificar({"gemini_api_key.txt": ""}, "privado", None)


def test_carregar_termos(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("# comentario\nClasseSecreta\n\nre:X\\d\n", encoding="utf-8")
    assert hook.carregar_termos(p) == ["ClasseSecreta", "re:X\\d"]
    assert hook.carregar_termos(tmp_path / "nao.txt") is None
