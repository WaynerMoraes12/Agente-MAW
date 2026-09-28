import pytest
import yaml
from maw_agent import catalogo

ITENS = [
    {"id": "a/um", "area": "a", "titulo": "Um", "descricao": "d", "origem": ["r"], "verificacao": ["e2e"],
     "cenarios": [], "requisitos": [], "marco": "M2"},
    {"id": "a/dois", "area": "a", "titulo": "Dois", "descricao": "d", "origem": ["r"], "verificacao": ["suite"],
     "cenarios": [], "requisitos": [], "marco": "M1"},
]

def test_carregar_e_validar(tmp_path):
    p = tmp_path / "c.yaml"; p.write_text(yaml.safe_dump(ITENS, allow_unicode=True), encoding="utf-8")
    assert catalogo.validar(catalogo.carregar(p)) == []

def test_validar_pega_duplicado_e_verificacao_invalida():
    erros = catalogo.validar([dict(ITENS[0]), dict(ITENS[0], verificacao=["palpite"])])
    assert any("duplicado" in e for e in erros) and any("palpite" in e for e in erros)

def test_matriz_nunca_vazia(tmp_path):
    catalogo.registrar_resultado(tmp_path, "a/dois", "main", "passou", fonte="suite")
    m = catalogo.montar_matriz(ITENS, ["main", "feature-x"], catalogo.carregar_resultados(tmp_path),
                               {("a/um", "feature-x"): True})
    assert m["a/dois"]["main"]["resultado"] == "passou"
    assert m["a/dois"]["feature-x"]["resultado"] == "nao_testavel"
    assert "sem resultado" in m["a/dois"]["feature-x"]["motivo"]
    assert m["a/um"]["feature-x"]["resultado"] == "na"
    assert "M2" in m["a/um"]["main"]["motivo"]
    assert catalogo.resumo(m) == {"passou": 1, "falhou": 0, "nao_testavel": 2, "na": 1}

def test_ultimo_registro_vence(tmp_path):
    catalogo.registrar_resultado(tmp_path, "a/um", "main", "falhou", achados=["X"])
    catalogo.registrar_resultado(tmp_path, "a/um", "main", "passou")
    assert catalogo.carregar_resultados(tmp_path)[("a/um", "main")]["resultado"] == "passou"

def test_somente_em_vira_na_nos_outros(tmp_path):
    it = dict(ITENS[0], id="a/nova", somente_em=["feature-x"])
    m = catalogo.montar_matriz([it], ["main", "feature-x"], {})
    assert m["a/nova"]["main"]["resultado"] == "na"
    assert m["a/nova"]["feature-x"]["resultado"] == "nao_testavel"

def test_resultado_invalido_levanta(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        catalogo.registrar_resultado(tmp_path, "a/um", "main", "talvez")

def test_carregar_tenta_de_novo_com_yaml_corrompido_e_depois_consertado(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text(":\n  - quebrado [", encoding="utf-8")  # YAML inválido
    sleeps = []

    def sleep_e_conserta(s):
        sleeps.append(s)
        p.write_text(yaml.safe_dump(ITENS, allow_unicode=True), encoding="utf-8")

    monkeypatch.setattr(catalogo.time, "sleep", sleep_e_conserta)
    assert catalogo.carregar(p) == ITENS
    assert sleeps == [0.3]  # corrigiu já na primeira tentativa de novo


def test_carregar_levanta_apos_esgotar_tentativas_com_yaml_sempre_invalido(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text(":\n  - quebrado [", encoding="utf-8")
    sleeps = []
    monkeypatch.setattr(catalogo.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(yaml.YAMLError):
        catalogo.carregar(p)
    assert sleeps == [0.3, 0.3, 0.3, 0.3]  # 5 tentativas no total, 4 esperas entre elas


def test_descartar_fonte_tira_so_as_linhas_daquela_fonte(tmp_path):
    catalogo.registrar_resultado(tmp_path, "a/um", "main", "passou", fonte="suite")
    catalogo.registrar_resultado(tmp_path, "a/um", "main", "falhou", achados=["X"], fonte="consolidacao")
    catalogo.registrar_resultado(tmp_path, "a/dois", "main", "falhou", fonte="consolidacao")
    catalogo.descartar_fonte(tmp_path, "consolidacao")
    r = catalogo.carregar_resultados(tmp_path)
    assert r[("a/um", "main")]["resultado"] == "passou" and ("a/dois", "main") not in r
    catalogo.descartar_fonte(tmp_path / "nao-existe", "x")  # sem arquivo: nada acontece


def test_catalogo_rascunho_copia_e_publicar_recusa_perder_itens(tmp_path, monkeypatch):
    import argparse
    from pathlib import Path
    from maw_agent import config, fases
    cat = tmp_path / "catalogo" / "funcionalidades.yaml"
    cat.parent.mkdir(parents=True)
    itens = [{"id": "a/um"}, {"id": "a/dois"}]
    cat.write_text(yaml.safe_dump(itens), encoding="utf-8")
    monkeypatch.setattr(config, "CATALOGO", cat)
    monkeypatch.setattr(config, "PRINCIPIOS", tmp_path / "nao-existe.yaml")
    monkeypatch.setattr(fases, "_validar_catalogo_completo", lambda itens, princ: [])
    monkeypatch.setattr(fases.sandbox, "escrever_texto", lambda p, t: Path(p).write_text(t, encoding="utf-8"))
    assert fases.cmd_catalogo(argparse.Namespace(acao="rascunho", rascunho=None)) == 0
    rascunho = cat.with_name("funcionalidades.rascunho.yaml")
    assert rascunho.read_text(encoding="utf-8") == cat.read_text(encoding="utf-8")
    rascunho.write_text(yaml.safe_dump([{"id": "a/um"}]), encoding="utf-8")  # perdeu a/dois
    assert fases.cmd_catalogo(argparse.Namespace(acao="publicar", rascunho=str(rascunho))) != 0
    assert "a/dois" in cat.read_text(encoding="utf-8")
