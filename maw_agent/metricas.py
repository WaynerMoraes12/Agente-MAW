"""Métricas de um processo ao longo do tempo, para testes de resistência (o aplicativo aberto por muito tempo).

`AmostradorProcesso` lê, de fora e sem tocar no processo, o que o Windows sabe dele: working set (RSS), bytes
privados (a memória que o processo pediu e não devolveu — o número que denuncia vazamento), handles do kernel,
objetos GDI e USER (`GetGuiResources`), threads e CPU desde a amostra anterior; e, se receber um vigia, se a janela
responde e quanto tempo levou para responder. Nada aqui manda mensagem para a janela por conta própria: o vigia é
de quem chama.

`avaliar_crescimento` e `avaliar_subida` julgam uma série depois do aquecimento, com os limites que quem chama
escolhe (e justifica). Os dois só dizem "excedeu" quando há pontos e janela suficientes; numa série curta devolvem
`julgado=False` com o motivo, nunca um palpite.

`gravar_csv`, `resumo` e `grafico_png` viram evidência (o gráfico é desenhado com a Pillow, sem matplotlib). Toda
escrita passa pelo `sandbox`.
"""
from __future__ import annotations

import csv
import ctypes
import io
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import psutil

from . import sandbox

MB = 1024 * 1024

CPU_INTERVALO_MIN_S = 0.05  # abaixo disso a CPU "desde a leitura anterior" não tem amostra (vira None)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_GR_GDIOBJECTS, _GR_USEROBJECTS = 0, 1


@dataclass
class Amostra:
    """Uma leitura. `t` em segundos desde o início do amostrador; `cpu_pct` em % de UM núcleo desde a leitura
    anterior (passa de 100 com várias threads ocupadas). None = não deu para ler (processo morto, acesso negado)."""
    t: float
    rss_mb: float | None
    privado_mb: float | None
    handles: int | None
    gdi: int | None
    user: int | None
    threads: int | None
    cpu_pct: float | None
    respondendo: bool | None
    latencia_ms: float | None
    vivo: bool = True
    extra: dict = field(default_factory=dict)


def objetos_gui(pid: int) -> tuple[int | None, int | None]:
    """(objetos GDI, objetos USER) do processo; (None, None) quando o Windows não deixa abrir o processo."""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError):  # fora do Windows
        return None, None
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    u32.GetGuiResources.restype = ctypes.c_uint32
    u32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not h:
        return None, None
    try:
        return int(u32.GetGuiResources(h, _GR_GDIOBJECTS)), int(u32.GetGuiResources(h, _GR_USEROBJECTS))
    finally:
        k32.CloseHandle(h)


class AmostradorProcesso:
    """Lê o processo `pid` a cada `amostrar()`; guarda as leituras em `amostras`.

    `respondendo`: função sem argumentos que diz se a janela do processo responde (ex.: um WM_NULL com prazo);
    o tempo que ela leva vira `latencia_ms`. Exceção nela conta como "não respondeu"."""

    def __init__(self, pid: int, respondendo: Callable[[], bool] | None = None,
                 relogio: Callable[[], float] = time.monotonic) -> None:
        self.pid = int(pid)
        self.respondendo = respondendo
        self.relogio = relogio
        self.inicio = relogio()
        self.amostras: list[Amostra] = []
        try:
            self._ps: psutil.Process | None = psutil.Process(self.pid)
            self._ps.cpu_percent(None)  # a primeira leitura de CPU só arma a conta
        except psutil.Error:
            self._ps = None
        self._t_cpu = time.perf_counter()

    def _vivo(self) -> bool:
        try:
            return self._ps is not None and self._ps.is_running() and self._ps.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def amostrar(self, **extra) -> Amostra:
        t = self.relogio() - self.inicio
        if not self._vivo():
            a = Amostra(t=t, rss_mb=None, privado_mb=None, handles=None, gdi=None, user=None, threads=None,
                        cpu_pct=None, respondendo=None, latencia_ms=None, vivo=False, extra=dict(extra))
            self.amostras.append(a)
            return a
        rss = privado = cpu = None
        handles = threads = None
        try:
            with self._ps.oneshot():
                mi = self._ps.memory_info()
                rss = mi.rss / MB
                privado = getattr(mi, "private", mi.vms) / MB
                threads = self._ps.num_threads()
                handles = self._ps.num_handles() if hasattr(self._ps, "num_handles") else None
                cpu = self._ps.cpu_percent(None)
                agora = time.perf_counter()
                if agora - self._t_cpu < CPU_INTERVALO_MIN_S:
                    cpu = None  # intervalo curto demais: o psutil devolveria 0, que não é medida
                self._t_cpu = agora
        except psutil.Error:
            pass
        gdi, user = objetos_gui(self.pid)
        respondeu = latencia = None
        if self.respondendo is not None:
            t0 = time.perf_counter()
            try:
                respondeu = bool(self.respondendo())
                latencia = (time.perf_counter() - t0) * 1000.0
            except Exception:  # noqa: BLE001 - janela sumindo no meio da pergunta: não respondeu
                respondeu = False
        a = Amostra(t=t, rss_mb=rss, privado_mb=privado, handles=handles, gdi=gdi, user=user, threads=threads,
                    cpu_pct=cpu, respondendo=respondeu, latencia_ms=latencia, vivo=True, extra=dict(extra))
        self.amostras.append(a)
        return a


# ------------------------------------------------------------------------------------------- tendência

@dataclass
class Veredito:
    """`julgado`: havia dados para decidir. `excedeu`: passou de todos os limites (só com `julgado`)."""
    julgado: bool
    excedeu: bool
    motivo: str
    medidas: dict = field(default_factory=dict)


def _pares(tempos_s: Sequence[float], valores: Sequence[float | None]) -> list[tuple[float, float]]:
    return [(float(t), float(v)) for t, v in zip(tempos_s, valores)
            if v is not None and t is not None and math.isfinite(float(v))]


def _inclinacao(pares: list[tuple[float, float]]) -> float | None:
    """Mínimos quadrados, em unidades por minuto."""
    if len(pares) < 2:
        return None
    mt = sum(t for t, _ in pares) / len(pares)
    mv = sum(v for _, v in pares) / len(pares)
    den = sum((t - mt) ** 2 for t, _ in pares)
    if den <= 0:
        return None
    return 60.0 * sum((t - mt) * (v - mv) for t, v in pares) / den


def inclinacao_por_minuto(tempos_s: Sequence[float], valores: Sequence[float | None]) -> float | None:
    """Inclinação da reta de mínimos quadrados (unidade/min), ignorando leituras None. None com menos de 2 pontos."""
    return _inclinacao(_pares(tempos_s, valores))


def _depois_do_aquecimento(tempos_s, valores, aquecimento_s: float) -> list[tuple[float, float]]:
    pares = _pares(tempos_s, valores)
    if not pares:
        return []
    t0 = float(tempos_s[0]) if len(tempos_s) else pares[0][0]
    return [(t, v) for t, v in pares if t - t0 >= aquecimento_s]


def _pontas(pares: list[tuple[float, float]]) -> tuple[float, float]:
    """Mediana das primeiras e das últimas leituras (até 3 de cada lado): um pico isolado não move a ponta."""
    k = max(1, min(3, len(pares) // 4))
    return statistics.median(v for _, v in pares[:k]), statistics.median(v for _, v in pares[-k:])


def _num(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def avaliar_crescimento(tempos_s: Sequence[float], valores: Sequence[float | None], *, aquecimento_s: float,
                        inclinacao_max: float, crescimento_max: float, min_pontos: int = 8,
                        min_janela_s: float = 300.0) -> Veredito:
    """Crescimento sustentado (vazamento) numa série de memória, depois de `aquecimento_s`.

    Excede só quando as três coisas acontecem juntas: a reta da janela inteira sobe mais que `inclinacao_max`
    por minuto, a da SEGUNDA metade sobe mais que a metade disso (um cache que enche e para tem a segunda metade
    plana; um vazamento continua subindo) e o total (mediana das pontas) passa de `crescimento_max`."""
    pares = _depois_do_aquecimento(tempos_s, valores, aquecimento_s)
    janela = (pares[-1][0] - pares[0][0]) if len(pares) >= 2 else 0.0
    if len(pares) < min_pontos or janela < min_janela_s:
        return Veredito(False, False, (f"janela curta para julgar crescimento: {len(pares)} leitura(s) em "
                                       f"{janela / 60:.1f} min depois do aquecimento de {aquecimento_s / 60:.1f} min "
                                       f"(mínimo {min_pontos} leituras e {min_janela_s / 60:.0f} min)"),
                        {"n": len(pares), "janela_s": janela})
    inc = _inclinacao(pares) or 0.0
    meio = (pares[0][0] + pares[-1][0]) / 2
    inc2 = _inclinacao([p for p in pares if p[0] >= meio]) or 0.0
    ini, fim = _pontas(pares)
    cresc = fim - ini
    excedeu = inc > inclinacao_max and inc2 > inclinacao_max / 2 and cresc > crescimento_max
    medidas = {"n": len(pares), "janela_s": janela, "inclinacao_mb_min": inc, "inclinacao_2a_metade_mb_min": inc2,
               "crescimento_mb": cresc, "inicio_mb": ini, "fim_mb": fim}
    motivo = (f"{_num(inc)} MB/min na janela (2ª metade {_num(inc2)} MB/min), {cresc:+.1f} MB "
              f"({ini:.1f} → {fim:.1f} MB) em {janela / 60:.1f} min depois do aquecimento; limites: "
              f"> {_num(inclinacao_max)} MB/min, 2ª metade > {_num(inclinacao_max / 2)} MB/min e > "
              f"{crescimento_max:g} MB")
    return Veredito(True, excedeu, motivo, medidas)


def avaliar_subida(tempos_s: Sequence[float], valores: Sequence[float | None], *, aquecimento_s: float,
                   aumento_max: float, fracao_min: float = 0.9, min_pontos: int = 8) -> Veredito:
    """Subida monótona (handles, threads) depois do aquecimento: excede quando a contagem sobe mais que
    `aumento_max` (mediana das pontas) E não cai em pelo menos `fracao_min` dos passos — um recurso que só se
    abre e nunca se fecha. Uma contagem que sobe e desce (pool de threads, arquivos abertos e fechados) não excede."""
    pares = _depois_do_aquecimento(tempos_s, valores, aquecimento_s)
    if len(pares) < min_pontos:
        return Veredito(False, False, f"poucas leituras para julgar a subida: {len(pares)} (mínimo {min_pontos})",
                        {"n": len(pares)})
    passos = [b - a for (_, a), (_, b) in zip(pares, pares[1:])]
    sem_queda = sum(1 for d in passos if d >= 0) / len(passos)
    ini, fim = _pontas(pares)
    aumento = fim - ini
    excedeu = aumento > aumento_max and sem_queda >= fracao_min
    motivo = (f"{ini:.0f} → {fim:.0f} ({aumento:+.0f}), sem queda em {sem_queda * 100:.0f} % dos passos; limites: "
              f"> {aumento_max:g} e sem queda em ≥ {fracao_min * 100:.0f} %")
    return Veredito(True, excedeu, motivo, {"n": len(pares), "aumento": aumento, "fracao_sem_queda": sem_queda,
                                            "inicio": ini, "fim": fim})


# ------------------------------------------------------------------------------------------- evidências

_FLOAT = ("t", "latencia_ms", "rss_mb", "privado_mb", "cpu_pct")
_INT = ("handles", "gdi", "user", "threads")
_COLUNAS = ("t", "vivo", "respondendo", "latencia_ms", "rss_mb", "privado_mb", "handles", "gdi", "user", "threads",
            "cpu_pct")


def _celula(nome: str, v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if nome in _FLOAT:
        return str(round(float(v), 3))
    if nome in _INT:
        return str(int(v))
    return str(v)


def gravar_csv(amostras: Sequence[Amostra], caminho: Path) -> Path:
    """Uma linha por amostra; as chaves de `extra` viram colunas no fim (em ordem alfabética)."""
    extras = sorted({k for a in amostras for k in a.extra})
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([*_COLUNAS, *extras])
    for a in amostras:
        w.writerow([*(_celula(c, getattr(a, c)) for c in _COLUNAS), *(_celula(k, a.extra.get(k)) for k in extras)])
    return sandbox.escrever_texto(Path(caminho), buf.getvalue())


def resumo(amostras: Sequence[Amostra]) -> dict:
    """Primeiro, último, mínimo, máximo e média de cada métrica (sem as leituras None) e as contagens de
    leituras sem resposta da janela e com o processo morto."""
    out: dict = {"n": len(amostras), "duracao_s": (amostras[-1].t - amostras[0].t) if amostras else 0.0,
                 "nao_respondeu": sum(1 for a in amostras if a.respondendo is False),
                 "morto": sum(1 for a in amostras if not a.vivo)}
    for nome in ("rss_mb", "privado_mb", "handles", "gdi", "user", "threads", "cpu_pct", "latencia_ms"):
        vs = [getattr(a, nome) for a in amostras if getattr(a, nome) is not None]
        out[nome] = ({"primeiro": vs[0], "ultimo": vs[-1], "min": min(vs), "max": max(vs),
                      "media": sum(vs) / len(vs)} if vs else None)
    return out


_PAINEIS = (
    ("Memória (MB): privada / RSS", (("privado_mb", (157, 0, 255)), ("rss_mb", (0, 150, 200)))),
    ("Handles do kernel", (("handles", (220, 120, 0)),)),
    ("Objetos GDI / USER e threads", (("gdi", (0, 160, 90)), ("user", (200, 60, 60)), ("threads", (90, 90, 90)))),
    ("CPU (% de um núcleo)", (("cpu_pct", (40, 90, 220)),)),
)


def _fonte(ImageFont):
    """Arial (tem acento) quando existe; senão a fonte embutida da Pillow."""
    for nome in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(nome, 12)
        except OSError:
            continue
    return ImageFont.load_default()


def _rotulo(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 1000 else f"{v:.1f}"


def grafico_png(amostras: Sequence[Amostra], caminho: Path, titulo: str = "") -> Path:
    """Gráfico simples (Pillow): um painel por grupo de métrica, eixo x em minutos, cada painel na própria escala
    com o mínimo e o máximo escritos. Sem amostras, uma imagem com o aviso."""
    from PIL import Image, ImageDraw, ImageFont

    larg, alt_painel, margem, topo = 960, 150, 70, 34
    img = Image.new("RGB", (larg, topo + alt_painel * len(_PAINEIS) + 30), (255, 255, 255))
    d = ImageDraw.Draw(img)
    fonte = _fonte(ImageFont)
    d.text((10, 8), titulo or "série do processo", fill=(0, 0, 0), font=fonte)
    if not amostras:
        d.text((10, topo + 10), "sem amostras", fill=(120, 0, 0), font=fonte)
    else:
        t0, t1 = amostras[0].t, amostras[-1].t
        span = max(t1 - t0, 1e-9)
        x0, x1 = margem, larg - 20
        for i, (nome_painel, series) in enumerate(_PAINEIS):
            y0 = topo + i * alt_painel + 18
            y1 = y0 + alt_painel - 34
            d.rectangle((x0, y0, x1, y1), outline=(200, 200, 200))
            d.text((x0, y0 - 14), nome_painel, fill=(0, 0, 0), font=fonte)
            vals = [getattr(a, s) for s, _ in series for a in amostras if getattr(a, s) is not None]
            if not vals:
                d.text((x0 + 8, y0 + 8), "sem leitura", fill=(120, 0, 0), font=fonte)
                continue
            vmin, vmax = min(vals), max(vals)
            if vmax - vmin < 1e-9:
                vmin, vmax = vmin - 1, vmax + 1
            d.text((4, y0), _rotulo(vmax), fill=(80, 80, 80), font=fonte)
            d.text((4, y1 - 12), _rotulo(vmin), fill=(80, 80, 80), font=fonte)
            for j, (serie, cor) in enumerate(series):
                pts = [(x0 + (a.t - t0) / span * (x1 - x0), y1 - (getattr(a, serie) - vmin) / (vmax - vmin) * (y1 - y0))
                       for a in amostras if getattr(a, serie) is not None]
                if len(pts) >= 2:
                    d.line(pts, fill=cor, width=2)
                elif pts:
                    d.ellipse((pts[0][0] - 2, pts[0][1] - 2, pts[0][0] + 2, pts[0][1] + 2), fill=cor)
                d.text((x1 - 110, y0 + 4 + 13 * j), serie, fill=cor, font=fonte)
            for a in amostras:  # sem resposta / morto: marca vermelha no eixo
                if a.respondendo is False or not a.vivo:
                    x = x0 + (a.t - t0) / span * (x1 - x0)
                    d.line((x, y1 - 6, x, y1), fill=(220, 0, 0), width=2)
        d.text((x0, img.height - 18), f"0 a {span / 60:.1f} min ({len(amostras)} leituras)", fill=(0, 0, 0),
               font=fonte)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return sandbox.escrever_bytes(Path(caminho), buf.getvalue())
