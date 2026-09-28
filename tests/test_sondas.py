import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from maw_agent import sandbox, sondas

NS = "{http://schemas.microsoft.com/developer/msbuild/2003}"

PROJETO = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n'
    '\r\n'
    '<Project DefaultTargets="Build"\r\n'
    '         ToolsVersion="17.0"\r\n'
    '         xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\r\n'
    '  <ItemGroup>\r\n'
    '    <ClCompile Include="..\\..\\Source\\Main.cpp"/>\r\n'
    '  </ItemGroup>\r\n'
    '  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets"/>\r\n'
    '</Project>\r\n'
)

SONDA = '''// SONDA-REQUER: Source/Motor.h: tocar parar
#include <JuceHeader.h>
class SondaX final : public juce::UnitTest
{
public:
    SondaX() : juce::UnitTest ("SONDA Motor toca e para", "MAW") {}
    void runTest() override {}
};
static SondaX sondaX;
'''


def _worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    (wt / "Builds" / "VisualStudio2022").mkdir(parents=True)
    (wt / "Builds" / "VisualStudio2022" / "MAW_APP_App.vcxproj").write_bytes(PROJETO.encode("utf-8"))
    (wt / "Source").mkdir()
    (wt / "Source" / "Motor.h").write_text("void tocar(); void parar();\n", encoding="utf-8")
    return wt


def _sonda(pasta: Path, nome: str, texto: str = SONDA) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    p = pasta / nome
    p.write_text(texto, encoding="utf-8")
    return p


def _projeto(wt: Path) -> bytes:
    return (wt / "Builds" / "VisualStudio2022" / "MAW_APP_App.vcxproj").read_bytes()


def _includes(wt: Path) -> list[str]:
    raiz = ET.fromstring(_projeto(wt))
    return [c.get("Include") for c in raiz.iter(NS + "ClCompile")]


def test_injetar_copia_e_acrescenta_ao_projeto_com_xml_valido(tmp_path):
    wt = _worktree(tmp_path)
    a = _sonda(tmp_path / "sondas", "SondaA.cpp")
    b = _sonda(tmp_path / "sondas", "SondaB.cpp", SONDA.replace("SondaX", "SondaY").replace("toca e para", "outra"))
    r = sondas.injetar(wt, [a, b])
    assert r["alterado"] is True and sorted(r["injetadas"]) == ["SondaA.cpp", "SondaB.cpp"]
    assert (wt / "Source" / "Tests" / "Sondas" / "SondaA.cpp").read_bytes() == a.read_bytes()
    inc = _includes(wt)
    assert "..\\..\\Source\\Main.cpp" in inc
    assert "..\\..\\Source\\Tests\\Sondas\\SondaA.cpp" in inc and "..\\..\\Source\\Tests\\Sondas\\SondaB.cpp" in inc
    # só no Debug: o Release (E2E e benchmark) nunca leva as sondas
    grupo = [g for g in ET.fromstring(_projeto(wt)).iter(NS + "ItemGroup") if g.get("Label") == sondas.ROTULO]
    assert len(grupo) == 1 and "Debug" in grupo[0].get("Condition")
    # o bloco novo segue o fim de linha do arquivo (CRLF)
    assert _projeto(wt).count(b"\n") == _projeto(wt).count(b"\r\n")


def test_injetar_e_idempotente(tmp_path):
    wt = _worktree(tmp_path)
    a = _sonda(tmp_path / "sondas", "SondaA.cpp")
    sondas.injetar(wt, [a])
    antes = _projeto(wt)
    mtime = (wt / "Source" / "Tests" / "Sondas" / "SondaA.cpp").stat().st_mtime_ns
    r = sondas.injetar(wt, [a])
    assert r["alterado"] is False
    assert _projeto(wt) == antes
    assert _includes(wt).count("..\\..\\Source\\Tests\\Sondas\\SondaA.cpp") == 1
    # cópia igual não é regravada: o MSBuild não recompila a sonda à toa
    assert (wt / "Source" / "Tests" / "Sondas" / "SondaA.cpp").stat().st_mtime_ns == mtime


def test_injetar_sincroniza_e_lista_vazia_devolve_o_projeto_original(tmp_path):
    wt = _worktree(tmp_path)
    a = _sonda(tmp_path / "sondas", "SondaA.cpp")
    b = _sonda(tmp_path / "sondas", "SondaB.cpp")
    sondas.injetar(wt, [a, b])
    sondas.injetar(wt, [a])
    assert not (wt / "Source" / "Tests" / "Sondas" / "SondaB.cpp").exists()
    assert "..\\..\\Source\\Tests\\Sondas\\SondaB.cpp" not in _includes(wt)
    r = sondas.injetar(wt, [])
    assert r["alterado"] is True
    assert _projeto(wt) == PROJETO.encode("utf-8")
    assert not (wt / "Source" / "Tests" / "Sondas").exists()


def test_injetar_pula_sonda_cujo_requisito_nao_existe_no_alvo(tmp_path):
    wt = _worktree(tmp_path)
    (wt / "Source" / "Motor.h").write_text("void tocar();\n", encoding="utf-8")  # sem parar()
    a = _sonda(tmp_path / "sondas", "SondaA.cpp")
    r = sondas.injetar(wt, [a])
    assert r["injetadas"] == [] and "parar" in r["puladas"]["SondaA.cpp"]
    assert "..\\..\\Source\\Tests\\Sondas\\SondaA.cpp" not in _includes(wt)


def test_injetar_recusa_arquivo_que_nao_e_cpp_e_projeto_ausente(tmp_path):
    wt = _worktree(tmp_path)
    h = _sonda(tmp_path / "sondas", "Sonda.h")
    with pytest.raises(ValueError):
        sondas.injetar(wt, [h])
    with pytest.raises(FileNotFoundError):
        sondas.injetar(tmp_path / "nada", [])


def test_injetar_respeita_a_sandbox(tmp_path, _sem_pastas_reais):
    wt = _worktree(_sem_pastas_reais)
    a = _sonda(tmp_path / "sondas", "SondaA.cpp")
    with pytest.raises(sandbox.EscritaProibida):
        sondas.injetar(wt, [a])


def test_nomes_e_requisitos_das_sondas(tmp_path):
    a = _sonda(tmp_path / "sondas", "SondaA.cpp")
    assert sondas.nomes(a) == ["SONDA Motor toca e para"]
    assert sondas.requisitos(a) == [("Source/Motor.h", ["tocar", "parar"])]
    sem_nome = _sonda(tmp_path / "sondas", "SondaZ.cpp", "int x;\n")
    assert sondas.nomes(sem_nome) == []


def test_listar_so_cpp_ordenados(tmp_path):
    p = tmp_path / "sondas"
    _sonda(p, "SondaB.cpp"); _sonda(p, "SondaA.cpp"); _sonda(p, "leia.md", "x")
    assert [x.name for x in sondas.listar(p)] == ["SondaA.cpp", "SondaB.cpp"]
    assert sondas.listar(tmp_path / "nao-existe") == []


def test_separar_blocos_tira_as_sondas_da_suite_existente():
    d = {"exit_code": 1, "declarado": "FALHOU", "incoerencias": [], "passou": False,
         "total_ok": 5, "total_falhas": 1, "blocos_declarados": 3,
         "blocos": [{"nome": "Grade", "sub": "a", "ok": 3, "falhas": 0},
                    {"nome": "SONDA Motor", "sub": "b", "ok": 1, "falhas": 1},
                    {"nome": "SONDA Motor", "sub": "c", "ok": 1, "falhas": 0}],
         "detalhes": ["- SONDA Motor / b", "!!! Test 1 failed: x"]}
    suite, das_sondas = sondas.separar_blocos(d)
    assert [b["nome"] for b in suite["blocos"]] == ["Grade"]
    assert suite["passou"] is True and suite["total_falhas"] == 0 and suite["detalhes"] == []
    assert [b["sub"] for b in das_sondas] == ["b", "c"]
    assert d["passou"] is False  # o original não muda


def test_separar_blocos_mantem_falha_da_suite_e_incoerencia():
    d = {"exit_code": 1, "declarado": "FALHOU", "incoerencias": ["x"], "passou": False,
         "blocos": [{"nome": "Grade", "sub": "a", "ok": 3, "falhas": 1}], "detalhes": []}
    suite, das_sondas = sondas.separar_blocos(d)
    assert suite["passou"] is False and das_sondas == []


def test_resultados_por_item_do_mapa():
    mapa = {"SONDA Motor": ["motor/tocar", "motor/parar"], "SONDA Ausente": ["motor/x"]}
    blocos = [{"nome": "SONDA Motor", "sub": "b", "ok": 1, "falhas": 1},
              {"nome": "SONDA Motor", "sub": "c", "ok": 1, "falhas": 0}]
    r = sondas.resultados_por_item(mapa, blocos)
    assert r["motor/tocar"][0] == "falhou" and r["motor/parar"][0] == "falhou"
    assert r["motor/x"][0] == "nao_testavel" and "SONDA Ausente" in r["motor/x"][1]
    r2 = sondas.resultados_por_item({"SONDA Motor": ["motor/tocar"]}, [blocos[1]])
    assert r2["motor/tocar"] == ("passou", None)


def test_carregar_mapa(tmp_path):
    p = tmp_path / "itens.yaml"
    p.write_text('"SONDA Motor":\n  - motor/tocar\n', encoding="utf-8")
    assert sondas.carregar_mapa(p) == {"SONDA Motor": ["motor/tocar"]}
    assert sondas.carregar_mapa(tmp_path / "nao.yaml") == {}


# ---------- CLI ----------

import json  # noqa: E402

from maw_agent import cli, config  # noqa: E402


def test_cli_injetar_sem_sprint_e_listar(tmp_path, monkeypatch, capsys):
    alvos_dir = tmp_path / "alvos"
    wt = _worktree(tmp_path)
    alvos_dir.mkdir()
    wt.rename(alvos_dir / "main")
    privado = tmp_path / "privado_sondas"
    _sonda(privado, "SondaA.cpp")
    (privado / "itens.yaml").write_text('"SONDA Motor toca e para": [motor/tocar]\n"SONDA Sumida": [motor/x]\n',
                                        encoding="utf-8")
    monkeypatch.setattr(config, "ALVOS_DIR", alvos_dir)
    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")
    monkeypatch.setattr(sondas, "PASTA_PRIVADA", privado)
    monkeypatch.setattr(sondas, "MAPA", privado / "itens.yaml")
    assert cli.main(["sondas", "injetar"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["alvos"]["main"]["injetadas"] == ["SondaA.cpp"]
    assert out["alvos"]["main"]["nomes"] == ["SONDA Motor toca e para"]
    assert r"..\..\Source\Tests\Sondas\SondaA.cpp" in _includes(alvos_dir / "main")
    # o mapa cita uma sonda que não existe: listar avisa e sai com 1
    assert cli.main(["sondas", "listar"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["sondas"][0]["itens"] == {"SONDA Motor toca e para": ["motor/tocar"]}
    assert out["mapa_sem_sonda"] == ["SONDA Sumida"]


def test_cli_registra_sondas_e_calibrar(capsys):
    assert cli.main(["--help-json"]) == 0
    subs = json.loads(capsys.readouterr().out)["subcomandos"]
    assert "sondas" in subs and "calibrar" in subs


def test_cli_injetar_erro_fatal_vai_para_as_limitacoes_da_sprint(tmp_path, monkeypatch, capsys):
    from maw_agent import estado
    alvos_dir = tmp_path / "alvos"
    alvos_dir.mkdir()
    (alvos_dir / "quebrado").mkdir()  # worktree sem o .vcxproj
    privado = tmp_path / "privado_sondas"
    _sonda(privado, "SondaA.cpp")
    monkeypatch.setattr(config, "ALVOS_DIR", alvos_dir)
    monkeypatch.setattr(config, "RELATORIOS", tmp_path / "relatorios")
    monkeypatch.setattr(sondas, "PASTA_PRIVADA", privado)
    e = estado.nova_sprint(tmp_path / "relatorios")
    (e.pasta / "alvos.json").write_text(json.dumps({"alvos": [
        {"nome": "quebrado", "commit": "a" * 40, "compartilha_com": None}]}), encoding="utf-8")
    assert cli.main(["sondas", "injetar"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert "não existe" in out["alvos"]["quebrado"]["erro"]
    lim = json.loads((e.pasta / "limitacoes-sondas.json").read_text(encoding="utf-8"))
    assert any("quebrado" in l and "não existe" in l for l in lim)
    assert "erro" in json.loads((e.pasta / "sondas.json").read_text(encoding="utf-8"))["quebrado"]
