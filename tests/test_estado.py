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


def test_duas_instancias_do_mesmo_estado_nao_se_apagam(tmp_path):
    base = estado.nova_sprint(tmp_path)
    a = estado.carregar(base.pasta)
    b = estado.carregar(base.pasta)  # outro processo, carregado antes da mudança de A
    a.concluir("catalogar:main")
    b.concluir("compilar:main:Release", {"ok": True})
    r = estado.carregar(base.pasta)
    assert r.feito("catalogar:main") and r.feito("compilar:main:Release")
    assert b.feito("catalogar:main")  # quem grava enxerga o que já estava no disco


def test_iniciar_falhar_e_restauracao_tambem_mesclam(tmp_path):
    base = estado.nova_sprint(tmp_path)
    a, b = estado.carregar(base.pasta), estado.carregar(base.pasta)
    a.iniciar("suite:main")
    b.falhar("noturno:julgamento-revisao", "código 1")
    a.registrar_restauracao({"quem": "suite", "verificado": True})
    b.registrar_restauracao({"quem": "e2e", "verificado": True})
    r = estado.carregar(base.pasta)
    assert r.passos["suite:main"]["status"] == "em_andamento"
    assert r.passos["noturno:julgamento-revisao"]["status"] == "falhou"
    assert [x["quem"] for x in r.restauracoes] == ["suite", "e2e"]


def test_escritas_concorrentes_em_threads_nao_perdem_passos(tmp_path):
    import threading
    base = estado.nova_sprint(tmp_path)

    def marcar(prefixo):
        e = estado.carregar(base.pasta)
        for i in range(25):
            e.concluir(f"{prefixo}:{i}")

    ts = [threading.Thread(target=marcar, args=(p,)) for p in ("a", "b", "c")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    r = estado.carregar(base.pasta)
    assert sum(1 for k in r.passos if r.feito(k)) == 75


def test_trava_ocupada_por_muito_tempo_levanta(tmp_path, monkeypatch):
    import msvcrt
    base = estado.nova_sprint(tmp_path)
    monkeypatch.setattr(estado, "ESPERA_TRAVA", 0.3)
    with open(base.pasta / "estado.lock", "a+b") as f:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            with __import__("pytest").raises(TimeoutError):
                base.concluir("x")
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
