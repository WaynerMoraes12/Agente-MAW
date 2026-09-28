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

def test_em_andamento_ignora_concluidas(tmp_path):
    a = estado.nova_sprint(tmp_path); a.concluir("encerrar"); a.concluir("relatorio")
    b = estado.nova_sprint(tmp_path); b.concluir("encerrar")  # restaurou, mas o PDF não saiu
    assert estado.em_andamento(tmp_path).nome == b.nome
    b.concluir("relatorio")
    assert estado.em_andamento(tmp_path) is None

def test_anterior_e_a_ultima_concluida(tmp_path):
    a = estado.nova_sprint(tmp_path); a.concluir("relatorio")
    b = estado.nova_sprint(tmp_path)
    assert estado.anterior(b) == a.pasta
    assert estado.anterior(a) is None

def test_falha_registrada(tmp_path):
    e = estado.nova_sprint(tmp_path); e.iniciar("x"); e.falhar("x", "boom")
    assert estado.carregar(e.pasta).passos["x"]["status"] == "falhou"
