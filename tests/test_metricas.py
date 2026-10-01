import csv
import math
import os
import time

import pytest

from maw_agent import metricas


# ---------- amostragem ----------

def test_amostrador_le_o_proprio_processo():
    respostas = iter([True, False])
    a = metricas.AmostradorProcesso(os.getpid(), respondendo=lambda: next(respostas))
    s1 = a.amostrar(tocando=True)
    time.sleep(0.05)
    s2 = a.amostrar()
    assert s1.vivo and s2.vivo
    assert s1.rss_mb > 1 and s1.privado_mb > 1
    assert s1.handles > 0 and s1.threads >= 1
    assert s1.respondendo is True and s2.respondendo is False
    assert s1.latencia_ms is not None and s1.latencia_ms >= 0
    assert s1.extra == {"tocando": True} and s2.extra == {}
    assert 0 <= s1.t <= s2.t
    assert s2.cpu_pct is not None and s2.cpu_pct >= 0
    assert a.amostras == [s1, s2]


def test_cpu_com_intervalo_curto_demais_nao_vira_zero():
    a = metricas.AmostradorProcesso(os.getpid())
    a._t_cpu = time.perf_counter() + 10  # a leitura anterior "acabou de acontecer"
    assert a.amostrar().cpu_pct is None
    time.sleep(0.06)
    assert a.amostrar().cpu_pct is not None


def test_amostrador_sem_vigia_de_janela_nao_inventa_resposta():
    s = metricas.AmostradorProcesso(os.getpid()).amostrar()
    assert s.respondendo is None and s.latencia_ms is None


def test_amostrador_de_processo_que_nao_existe_marca_morto():
    pid = 2 ** 22 + 7  # não existe (pids do Windows são múltiplos de 4)
    a = metricas.AmostradorProcesso(pid, respondendo=lambda: True)
    s = a.amostrar()
    assert s.vivo is False
    assert s.rss_mb is None and s.handles is None and s.respondendo is None


def test_vigia_que_levanta_conta_como_nao_respondendo():
    def explode():
        raise OSError("janela sumiu")
    s = metricas.AmostradorProcesso(os.getpid(), respondendo=explode).amostrar()
    assert s.respondendo is False


def test_objetos_gui_do_proprio_processo():
    gdi, user = metricas.objetos_gui(os.getpid())
    assert gdi is None or gdi >= 0
    assert user is None or user >= 0


# ---------- tendência ----------

def test_inclinacao_por_minuto_reta_exata():
    ts = [0, 60, 120, 180]
    assert metricas.inclinacao_por_minuto(ts, [100, 102, 104, 106]) == pytest.approx(2.0)
    assert metricas.inclinacao_por_minuto(ts, [5, 5, 5, 5]) == pytest.approx(0.0)


def test_inclinacao_ignora_buracos_e_precisa_de_dois_pontos():
    assert metricas.inclinacao_por_minuto([0, 60, 120], [10, None, 14]) == pytest.approx(2.0)
    assert metricas.inclinacao_por_minuto([0, 60], [10, None]) is None
    assert metricas.inclinacao_por_minuto([0, 0], [1, 2]) is None  # tempo sem variação


def _serie(minutos: float, passo_s: float, f):
    ts = [i * passo_s for i in range(int(minutos * 60 / passo_s) + 1)]
    return ts, [f(t) for t in ts]


def test_crescimento_linear_constante_e_vazamento():
    ts, vs = _serie(30, 15, lambda t: 200 + 2.0 * t / 60)  # 2 MB/min, sempre
    v = metricas.avaliar_crescimento(ts, vs, aquecimento_s=120, inclinacao_max=1.0, crescimento_max=20)
    assert v.julgado and v.excedeu
    assert v.medidas["inclinacao_mb_min"] == pytest.approx(2.0)
    assert v.medidas["inclinacao_2a_metade_mb_min"] == pytest.approx(2.0)
    assert v.medidas["crescimento_mb"] == pytest.approx(2.0 * 27, rel=0.05)


def test_cache_que_satura_nao_e_vazamento():
    # sobe 60 MB nos primeiros minutos e fica: a 2a metade é plana
    ts, vs = _serie(30, 15, lambda t: 200 + 60 * (1 - math.exp(-t / 240)))
    v = metricas.avaliar_crescimento(ts, vs, aquecimento_s=120, inclinacao_max=1.0, crescimento_max=20)
    assert v.julgado and not v.excedeu
    assert v.medidas["inclinacao_2a_metade_mb_min"] < 0.5


def test_ruido_sem_tendencia_nao_e_vazamento():
    ts, vs = _serie(30, 15, lambda t: 300 + (7 if int(t / 15) % 3 == 0 else -4))
    v = metricas.avaliar_crescimento(ts, vs, aquecimento_s=120, inclinacao_max=1.0, crescimento_max=20)
    assert v.julgado and not v.excedeu


def test_subida_lenta_abaixo_do_crescimento_minimo_nao_e_vazamento():
    ts, vs = _serie(30, 15, lambda t: 200 + 1.5 * t / 60)  # passa da inclinação, mas o total é pequeno?
    v = metricas.avaliar_crescimento(ts, vs, aquecimento_s=120, inclinacao_max=1.0, crescimento_max=60)
    assert v.julgado and not v.excedeu  # ~40 MB < 60 MB


def test_janela_curta_nao_julga():
    ts, vs = _serie(2, 15, lambda t: 200 + 50 * t / 60)
    v = metricas.avaliar_crescimento(ts, vs, aquecimento_s=90, inclinacao_max=1.0, crescimento_max=20)
    assert not v.julgado and not v.excedeu
    assert "curta" in v.motivo


def test_aquecimento_fica_de_fora():
    # degrau grande só no aquecimento; depois plano
    ts, vs = _serie(30, 15, lambda t: 100 if t < 120 else 400)
    v = metricas.avaliar_crescimento(ts, vs, aquecimento_s=120, inclinacao_max=1.0, crescimento_max=20)
    assert v.julgado and not v.excedeu
    assert v.medidas["crescimento_mb"] == pytest.approx(0.0)


# ---------- subida monótona (handles, threads) ----------

def test_handles_subindo_sempre_excedem():
    ts, vs = _serie(30, 15, lambda t: 800 + int(t / 15) * 3)  # +3 por amostra, nunca desce
    v = metricas.avaliar_subida(ts, vs, aquecimento_s=120, aumento_max=200)
    assert v.julgado and v.excedeu
    assert v.medidas["aumento"] > 200 and v.medidas["fracao_sem_queda"] == pytest.approx(1.0)


def test_handles_que_sobem_e_descem_nao_excedem():
    ts, vs = _serie(30, 15, lambda t: 800 + int(t / 15) * 3 - (40 if int(t / 15) % 2 else 0))
    v = metricas.avaliar_subida(ts, vs, aquecimento_s=120, aumento_max=200)
    assert v.julgado and not v.excedeu


def test_subida_pequena_nao_excede():
    ts, vs = _serie(30, 15, lambda t: 800 + int(t / 60))
    v = metricas.avaliar_subida(ts, vs, aquecimento_s=120, aumento_max=200)
    assert v.julgado and not v.excedeu


def test_subida_sem_pontos_suficientes_nao_julga():
    v = metricas.avaliar_subida([0, 15, 30], [1, 500, 1000], aquecimento_s=0, aumento_max=10)
    assert not v.julgado and not v.excedeu


# ---------- evidências ----------

def _amostras():
    out = []
    for i in range(12):
        out.append(metricas.Amostra(t=i * 15.0, rss_mb=300 + i, privado_mb=250 + 2 * i, handles=900 + i,
                                    gdi=40, user=30, threads=50, cpu_pct=12.5 if i else None,
                                    respondendo=True, latencia_ms=1.5, vivo=True,
                                    extra={"tocando": i % 2 == 0}))
    out.append(metricas.Amostra(t=180.0, rss_mb=None, privado_mb=None, handles=None, gdi=None, user=None,
                                threads=None, cpu_pct=None, respondendo=None, latencia_ms=None, vivo=False))
    return out


def test_csv_tem_uma_linha_por_amostra_e_as_colunas_extras(tmp_path):
    p = metricas.gravar_csv(_amostras(), tmp_path / "serie.csv")
    linhas = list(csv.DictReader(p.open(encoding="utf-8")))
    assert len(linhas) == 13
    assert linhas[0]["privado_mb"] == "250.0" and linhas[0]["tocando"] == "True"
    assert linhas[-1]["vivo"] == "False" and linhas[-1]["rss_mb"] == ""


def test_resumo_da_serie():
    r = metricas.resumo(_amostras())
    assert r["n"] == 13 and r["duracao_s"] == 180.0
    assert r["privado_mb"]["primeiro"] == 250 and r["privado_mb"]["ultimo"] == 272
    assert r["privado_mb"]["max"] == 272 and r["handles"]["min"] == 900
    assert r["cpu_pct"]["media"] == pytest.approx(12.5)
    assert r["nao_respondeu"] == 0 and r["morto"] == 1


def test_grafico_png(tmp_path):
    p = metricas.grafico_png(_amostras(), tmp_path / "serie.png", titulo="teste")
    from PIL import Image
    img = Image.open(p)
    assert img.size[0] >= 600 and img.size[1] >= 400
    # há traço desenhado (não é uma imagem de uma cor só)
    assert len(img.convert("RGB").getcolors(1 << 16) or []) > 3


def test_grafico_sem_dados_nao_quebra(tmp_path):
    p = metricas.grafico_png([], tmp_path / "vazio.png")
    assert p.exists()
