from maw_agent import estado

def test_numeracao_sequencial(tmp_path):
    a = estado.nova_sprint(tmp_path); b = estado.nova_sprint(tmp_path)
    assert (a.nome, b.nome) == ("sprint-01", "sprint-02")

def test_passos_persistem_e_retomam(tmp_path):
    e = estado.nova_sprint(tmp_path)
    e.iniciar("compilar:main:Release"); e.concluir("compilar:main:Release", {"ok": True})
    e.iniciar("compilar:feature-x:Release")  # interrompido aqui
    r = estado.carregar(e.pasta)
    assert r.feito("compilar:main:Release") and r.dados("compilar:main:Release") == {"ok": True}
    assert not r.feito("compilar:feature-x:Release")

def test_em_andamento_so_olha_a_mais_nova(tmp_path):
    a = estado.nova_sprint(tmp_path); a.concluir("encerrar"); a.concluir("relatorio")
    b = estado.nova_sprint(tmp_path); b.concluir("encerrar")  # restaurou, mas o PDF não saiu
    assert estado.em_andamento(tmp_path).nome == b.nome
    b.concluir("relatorio")
    assert estado.em_andamento(tmp_path) is None

def test_sprint_antiga_abandonada_nunca_e_retomada(tmp_path):
    a = estado.nova_sprint(tmp_path); a.iniciar("compilar:main:Release")  # sprint-01 abandonada
    b = estado.nova_sprint(tmp_path); b.concluir("relatorio")              # sprint-02 concluída
    assert estado.em_andamento(tmp_path) is None

def test_restauracoes_registradas_persistem(tmp_path):
    e = estado.nova_sprint(tmp_path)
    e.registrar_restauracao({"quem": "suite", "backup": "b1", "verificado": True, "erro": None})
    r = estado.carregar(e.pasta)
    assert [x["backup"] for x in r.restauracoes] == ["b1"] and "quando" in r.restauracoes[0]

def test_anterior_e_a_ultima_concluida(tmp_path):
    a = estado.nova_sprint(tmp_path); a.concluir("relatorio")
    b = estado.nova_sprint(tmp_path)
    assert estado.anterior(b) == a.pasta
    assert estado.anterior(a) is None

def test_falha_registrada(tmp_path):
    e = estado.nova_sprint(tmp_path); e.iniciar("x"); e.falhar("x", "boom")
    assert estado.carregar(e.pasta).passos["x"]["status"] == "falhou"
