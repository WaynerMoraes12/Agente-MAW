"""Áudio pela placa interna: captura por loopback WASAPI, medidas e geração de sinais de teste.

Sem VB-Cable: a saída vai pela placa física e a entrada é o loopback WASAPI da própria saída
padrão (`pyaudiowpatch`). Toda medida trabalha em float64 normalizado em -1..1 (cheio de escala
= 1.0 = 0 dBFS). Gravar e medir nunca é "achado" por si: quem chama decide o que fazer com o
número.
"""
from __future__ import annotations
import base64
import io
import math
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf

from . import sandbox

_RECURSOS = Path(__file__).parent / "recursos"
_PS_LISTAR_VOZES = _RECURSOS / "listar_vozes_sapi.ps1"
_PS_FALAR = _RECURSOS / "falar_sapi.ps1"
_TAMANHO_BLOCO = 1024  # quadros por leitura do stream de loopback
_FOLGA_JOIN_S = 5.0  # folga do join() sobre segundos_max, pra dar tempo de uma leitura terminar

# Continuidade da captura. O PortAudio joga fora, sem avisar, o que a leitura não buscou a tempo quando
# `exception_on_overflow=False`: a captura sai "emendada" (o que tocou depois aparece colado no que tocou
# antes) e todo intervalo medido nela encolhe. Por isso a leitura pede o aviso de transbordo e, além dele,
# confere os quadros recebidos contra o relógio do PC (o aviso nem sempre vem, conforme o driver).
_PA_TRANSBORDO = getattr(pyaudio, "paInputOverflowed", -9981)
_TRANSBORDOS_SEGUIDOS_MAX = 4  # transbordo em toda leitura: desiste do aviso para não ficar sem áudio nenhum
_AQUECIMENTO_S = 0.25  # o começo da captura (o stream assentando) fica fora da conta do relógio
_JANELA_PISO_S = 0.25  # cada trecho em que se procura o piso do atraso (o mínimo filtra o jitter das leituras)
_LIMIAR_PASSO_S = 0.005  # degrau do piso que conta como perda: menos de meio bloco de 512 a 48 kHz (10,7 ms)
_relogio = time.perf_counter  # relógio do PC para a conta (trocado nos testes)


class LoopbackIndisponivel(Exception):
    """Não há dispositivo de loopback WASAPI para a saída padrão (ou não foi possível abri-lo)."""


class VozIndisponivel(Exception):
    """SAPI (System.Speech) não respondeu ou não sintetizou o áudio pedido."""


# ---------------------------------------------------------------------------
# Captura por loopback
# ---------------------------------------------------------------------------

def _dispositivo_loopback(p: "pyaudio.PyAudio") -> dict:
    try:
        return p.get_default_wasapi_loopback()
    except (OSError, LookupError) as e:
        raise LoopbackIndisponivel(f"sem loopback WASAPI para a saída padrão: {e}") from e


def _para_mono(dados: bytes, canais: int) -> np.ndarray:
    x = np.frombuffer(dados, dtype=np.float32).astype(np.float64)
    if canais > 1:
        x = x.reshape(-1, canais).mean(axis=1)
    return x


def _eh_transbordo(e: OSError) -> bool:
    """O OSError que o PyAudio levanta quando o PortAudio avisa que a entrada transbordou (-9981)."""
    return getattr(e, "errno", None) == _PA_TRANSBORDO or bool(e.args and e.args[0] == _PA_TRANSBORDO)


class _Leitor:
    """Lê blocos de um stream de entrada sem engolir perda: pede o aviso de transbordo ao PortAudio (e
    continua a gravar depois dele), anota cada leitura (instante em que voltou, quadros acumulados) para
    conferir contra o relógio, e só desiste do aviso se ele vier em toda leitura — nesse caso o relógio
    ainda mede a perda. Qualquer outro OSError sobe (o dispositivo caiu)."""

    def __init__(self, stream, canais: int) -> None:
        self.stream = stream
        self.canais = max(1, canais)
        self.blocos: list[bytes] = []
        self.leituras: list[tuple[float, int]] = []
        self.transbordos: list[tuple[float, int]] = []
        self.sem_aviso = False  # o aviso de transbordo veio em toda leitura e foi desligado
        self._recebidos = 0
        self._seguidos = 0

    def ler(self, n: int) -> None:
        try:
            bloco = self.stream.read(n, exception_on_overflow=not self.sem_aviso)
        except OSError as e:
            if not _eh_transbordo(e):
                raise
            # o PyAudio descarta o bloco que avisou o transbordo: a perda aparece também no relógio
            self.transbordos.append((_relogio(), self._recebidos))
            self._seguidos += 1
            if self._seguidos >= _TRANSBORDOS_SEGUIDOS_MAX:
                self.sem_aviso = True
            return
        self._seguidos = 0
        self.blocos.append(bloco)
        self._recebidos += len(bloco) // (4 * self.canais)
        self.leituras.append((_relogio(), self._recebidos))

    def continuidade(self, sr: int) -> dict:
        c = continuidade(self.leituras, sr, self.transbordos)
        if self.sem_aviso:
            c["sem_aviso_de_transbordo"] = True
            if c["motivo"]:
                c["motivo"] += (f"; o PortAudio avisou transbordo em {_TRANSBORDOS_SEGUIDOS_MAX} leituras seguidas e "
                                f"a captura seguiu sem o aviso")
        return c


def continuidade(leituras: list[tuple[float, int]], sr: int,
                 transbordos: list[tuple[float, int]] | tuple = ()) -> dict:
    """A captura foi contínua no tempo? `leituras`: (instante em que cada read() voltou, no relógio do PC;
    quadros recebidos até ali); `transbordos`: (instante, quadros recebidos até ali) de cada aviso de
    transbordo do PortAudio.

    Sem perda, os quadros acompanham o relógio: o atraso D = (t − t0)·sr − quadros só oscila com o que está
    no buffer na hora da leitura (sempre ≥ 0). Uma perda é um degrau no PISO desse atraso. Por trechos de
    `_JANELA_PISO_S`, o piso é o mínimo de D no trecho; o piso que vale em cada ponto é o menor dali até o fim
    (assim uma leitura que só se atrasou, e depois alcançou, não vira perda); cada degrau acima de
    `_LIMIAR_PASSO_S` é uma perda. Deriva entre o relógio da placa e o do PC é uma rampa suave (degraus
    pequenos), não perda. O começo (`_AQUECIMENTO_S`) fica de fora, e lá um transbordo é só registrado.

    Devolve {"medida", "perdeu", "transbordos", "quadros_recebidos", "quadros_esperados", "quadros_perdidos",
    "perdas": [{"em_s", "quadros"}], "duracao_s", "motivo"} — `motivo` é o texto (pt-BR) quando perdeu."""
    out = {"medida": False, "perdeu": False, "transbordos": len(transbordos), "quadros_recebidos": 0,
           "quadros_esperados": 0, "quadros_perdidos": 0, "perdas": [], "duracao_s": 0.0, "motivo": None}
    if not leituras or sr <= 0:
        return out
    t0, f0 = leituras[0]
    t_fim, f_fim = leituras[-1]
    duracao = t_fim - t0
    out.update(quadros_recebidos=int(f_fim), quadros_esperados=int(round(f0 + duracao * sr)),
               duracao_s=round(duracao, 3))
    aquecimento = min(_AQUECIMENTO_S, duracao * 0.2)
    validos_transb = [t for t, _ in transbordos if t - t0 >= aquecimento]
    janela = min(_JANELA_PISO_S, max(duracao, 1e-9) / 4)
    trechos: dict[int, tuple[float, int]] = {}  # índice do trecho -> (piso do atraso, quadros na leitura do piso)
    for t, f in leituras:
        if t - t0 < aquecimento:
            continue
        k = int((t - t0 - aquecimento) // janela)
        d = (t - t0) * sr - f
        if k not in trechos or d < trechos[k][0]:
            trechos[k] = (d, f)
    if len(leituras) >= 4 and duracao >= 0.2 and len(trechos) >= 2:
        out["medida"] = True
        chaves = sorted(trechos)
        pisos = [trechos[k][0] for k in chaves]
        menor_ate_o_fim = pisos[:]
        for i in range(len(pisos) - 2, -1, -1):
            menor_ate_o_fim[i] = min(pisos[i], menor_ate_o_fim[i + 1])
        limiar = _LIMIAR_PASSO_S * sr
        for i in range(len(pisos) - 1):
            degrau = menor_ate_o_fim[i + 1] - menor_ate_o_fim[i]
            if degrau > limiar:
                q = int(round(degrau))
                out["quadros_perdidos"] += q
                out["perdas"].append({"em_s": round(trechos[chaves[i + 1]][1] / sr, 2), "quadros": q})
    partes = []
    if out["quadros_perdidos"]:
        ms = out["quadros_perdidos"] / sr * 1000
        onde = ", ".join(f"{p['quadros']} em ~{p['em_s']:.2f} s" for p in out["perdas"][:6])
        partes.append(f"~{out['quadros_perdidos']} quadros ({ms:.0f} ms) a menos que o relógio ({onde})")
    if validos_transb:
        partes.append(f"{len(validos_transb)} transbordo(s) avisado(s) pelo PortAudio")
    if partes:
        out["perdeu"] = True
        out["motivo"] = "a captura do loopback perdeu entrada: " + "; ".join(partes)
    return out


def captura_loopback(segundos: float, continuidade: dict | None = None) -> tuple[np.ndarray, int]:
    """Grava `segundos` do loopback WASAPI da saída padrão. Devolve (sinal_mono, taxa_amostragem).

    `continuidade`: se vier um dicionário, recebe o resultado de `continuidade()` da captura (perdeu
    entrada?). Levanta `LoopbackIndisponivel` se não houver loopback ou o stream não abrir.
    """
    p = pyaudio.PyAudio()
    try:
        info = _dispositivo_loopback(p)
        sr = int(info["defaultSampleRate"])
        canais = int(info["maxInputChannels"]) or 2
        try:
            stream = p.open(format=pyaudio.paFloat32, channels=canais, rate=sr, input=True,
                             input_device_index=info["index"], frames_per_buffer=_TAMANHO_BLOCO)
        except OSError as e:
            raise LoopbackIndisponivel(f"não foi possível abrir o loopback: {e}") from e
        leitor = _Leitor(stream, canais)
        try:
            alvo = int(segundos * sr)
            lidos = 0
            while lidos < alvo:
                n = min(_TAMANHO_BLOCO, alvo - lidos)
                try:
                    leitor.ler(n)
                except OSError as e:
                    raise LoopbackIndisponivel(f"a captura do loopback caiu no meio: {e}") from e
                lidos += n
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        p.terminate()
    if continuidade is not None:
        continuidade.update(leitor.continuidade(sr))
    return _para_mono(b"".join(leitor.blocos), canais), sr


class Gravador:
    """Context manager: grava o loopback da saída padrão enquanto o bloco `with` roda.

        with Gravador() as g:
            tocar_alguma_coisa()
        sinal, sr = g.sinal

    `segundos_max` é um teto de segurança (a gravação termina de verdade quando o `with` sai).
    Levanta `LoopbackIndisponivel` no `__enter__` se não houver loopback, e também no `__exit__`
    se a captura cair com um `OSError` no meio (a menos que o bloco `with` já esteja saindo com
    uma exceção sua — nesse caso a original nunca é substituída, só ganha uma nota sobre a queda
    da captura). Qualquer outra exceção na thread de captura (um bug nosso, não uma falha de
    dispositivo) sobe do `__exit__` como ela mesma, sem virar `LoopbackIndisponivel`.

    Perda de entrada nunca levanta: transbordo avisado pelo PortAudio e quadros a menos que o relógio
    ficam em `continuidade` (ver `continuidade()`), com `perdeu_entrada`/`motivo_perda` para o cenário
    decidir — quem mede intervalo na captura repete a gravação (`capturar_sem_perda`) ou pula.
    """

    def __init__(self, segundos_max: float = 10.0) -> None:
        self.segundos_max = segundos_max
        self.sinal: tuple[np.ndarray, int] | None = None
        self.limitacao: str | None = None
        self.continuidade: dict | None = None
        self._p: "pyaudio.PyAudio | None" = None
        self._stream = None
        self._sr = 0
        self._canais = 1
        self._leitor: _Leitor | None = None
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._erro: BaseException | None = None

    @property
    def perdeu_entrada(self) -> bool:
        """A captura perdeu entrada (blocos jogados fora no meio)? False até o `with` terminar."""
        return bool(self.continuidade and self.continuidade.get("perdeu"))

    @property
    def motivo_perda(self) -> str | None:
        """O que a captura perdeu (texto pt-BR), ou None se ela foi contínua."""
        return self.continuidade.get("motivo") if self.perdeu_entrada else None

    @property
    def _blocos(self) -> list[bytes]:
        return self._leitor.blocos if self._leitor is not None else []

    def __enter__(self) -> "Gravador":
        self._p = pyaudio.PyAudio()
        try:
            info = _dispositivo_loopback(self._p)
            self._sr = int(info["defaultSampleRate"])
            self._canais = int(info["maxInputChannels"]) or 2
            try:
                self._stream = self._p.open(format=pyaudio.paFloat32, channels=self._canais, rate=self._sr,
                                             input=True, input_device_index=info["index"],
                                             frames_per_buffer=_TAMANHO_BLOCO)
            except OSError as e:
                raise LoopbackIndisponivel(f"não foi possível abrir o loopback: {e}") from e
        except BaseException:
            self._p.terminate()
            self._p = None
            raise
        self._leitor = _Leitor(self._stream, self._canais)
        self._thread = threading.Thread(target=self._laco, daemon=True)
        self._thread.start()
        return self

    def _laco(self) -> None:
        maximo = int(self.segundos_max * self._sr)
        lidos = 0
        try:
            while not self._parar.is_set() and lidos < maximo:
                self._leitor.ler(_TAMANHO_BLOCO)  # transbordo avisado não levanta: fica anotado no leitor
                lidos += _TAMANHO_BLOCO
        except Exception as e:  # guarda qualquer coisa; o __exit__ decide o que fazer com ela —
            self._erro = e     # ver ali: só OSError vira LoopbackIndisponivel, o resto sobe como é

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=self.segundos_max + _FOLGA_JOIN_S)
            if self._thread.is_alive():
                # a leitura travou de vez (driver preso): não dá pra esperar para sempre — a
                # thread é daemon e morre com o processo, mas isso não pode passar em silêncio.
                self.limitacao = f"thread de captura não terminou em {self.segundos_max + _FOLGA_JOIN_S:.2f} s"
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
        if self._p is not None:
            self._p.terminate()
        if self._erro is not None:
            if exc_type is not None:
                # o bloco `with` já está propagando uma exceção sua: a queda da captura nunca
                # pode mascará-la — só registra uma nota (3.11+) e deixa a original seguir.
                if hasattr(exc_val, "add_note"):
                    exc_val.add_note(f"além disso, a captura do loopback caiu no meio: {self._erro}")
                return
            if isinstance(self._erro, OSError):
                raise LoopbackIndisponivel(f"a captura do loopback caiu no meio: {self._erro}") from self._erro
            # não é falha de dispositivo: é bug nosso dentro da thread de captura — sobe como é,
            # não finge "sem loopback" (só anexa uma nota dizendo de onde veio).
            if hasattr(self._erro, "add_note"):
                self._erro.add_note("exceção na thread de captura")
            raise self._erro
        self.sinal = (_para_mono(b"".join(self._blocos), self._canais), self._sr)
        if self._thread is not None and not self._thread.is_alive():
            # com a thread presa (ver `limitacao`) a lista ainda pode mudar: aí a conta fica de fora
            self.continuidade = self._leitor.continuidade(self._sr)


@dataclass
class Captura:
    """O que `capturar_sem_perda` devolve: o sinal da última gravação feita, se ela ainda perdeu entrada (e
    o motivo), e o motivo de cada tentativa descartada (inclusive a última, quando perdeu)."""
    sinal: tuple[np.ndarray, int]
    perdeu: bool
    motivo: str | None
    tentativas: list[str] = field(default_factory=list)
    gravador: object = None


def capturar_sem_perda(tocar: Callable[[], None], segundos_max: float = 10.0, tentativas: int = 3,
                       conferir: Callable[[np.ndarray, int], str | None] | None = None) -> Captura:
    """Grava o loopback enquanto `tocar()` roda e repete (até `tentativas` vezes) quando a captura perdeu
    entrada — pelo `Gravador` ou pelo `conferir(sinal, sr)` do cenário, que devolve o motivo de uma perda que
    ele mesmo viu no sinal (ex.: intervalo menor por blocos inteiros) ou None. Para cenário que mede tempo
    na captura: uma captura emendada encolhe os intervalos e nunca pode virar achado contra a fonte.
    Exceções de `tocar()` e do `Gravador` sobem como são."""
    motivos: list[str] = []
    g = None
    motivo = None
    for n in range(1, max(1, tentativas) + 1):
        with Gravador(segundos_max=segundos_max) as g:
            tocar()
        motivo = g.motivo_perda
        if motivo is None and conferir is not None:
            motivo = conferir(*g.sinal)
        if motivo is None:
            return Captura(g.sinal, False, None, motivos, g)
        motivos.append(f"tentativa {n}: {motivo}")
    return Captura(g.sinal, True, motivo, motivos, g)


# ---------------------------------------------------------------------------
# Medidas
# ---------------------------------------------------------------------------

def rms_dbfs(x: np.ndarray) -> float:
    """RMS de `x` em dBFS (0 dBFS = amplitude 1.0 de pico numa senoide cheia de escala)."""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return float("-inf")
    rms = float(np.sqrt(np.mean(np.square(x))))
    return 20 * np.log10(rms) if rms > 0 else float("-inf")


def pico_dbfs(x: np.ndarray) -> float:
    """Amplitude de pico de `x` em dBFS."""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return float("-inf")
    pico = float(np.max(np.abs(x)))
    return 20 * np.log10(pico) if pico > 0 else float("-inf")


def ativo(x: np.ndarray, limiar_dbfs: float = -50.0) -> bool:
    """Há sinal em `x` acima de `limiar_dbfs` (RMS)?"""
    return bool(rms_dbfs(x) > limiar_dbfs)


def freq_dominante(x: np.ndarray, sr: int) -> float:
    """Frequência (Hz) de maior energia em `x`, com interpolação parabólica sub-bin."""
    x = np.asarray(x, dtype=np.float64)
    if x.size < 4:
        return 0.0
    janela = np.hanning(x.size)
    espectro = np.abs(np.fft.rfft(x * janela))
    if espectro.size < 3:
        return 0.0
    pico = int(np.argmax(espectro[1:])) + 1  # ignora o bin DC
    if 0 < pico < espectro.size - 1:
        alfa, beta, gama = espectro[pico - 1], espectro[pico], espectro[pico + 1]
        denom = alfa - 2 * beta + gama
        delta = 0.5 * (alfa - gama) / denom if denom != 0 else 0.0
    else:
        delta = 0.0
    return float((pico + delta) * sr / x.size)


def cliques(x: np.ndarray, sr: int, limiar_relativo: float = 0.3, distancia_min_s: float = 0.05) -> list[float]:
    """Tempos (s) dos transientes (cliques) em `x`: picos do envelope retificado acima de
    `limiar_relativo` do pico máximo, agrupando cada trecho contíguo acima do limiar num só
    clique (o instante do clique é o pico local, não a borda de subida)."""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return []
    envelope = np.abs(x)
    janela = max(1, int(0.002 * sr))  # suaviza ~2 ms para não contar ruído de amostra a amostra
    if janela > 1:
        nucleo = np.ones(janela) / janela
        envelope = np.convolve(envelope, nucleo, mode="same")
    pico_max = float(envelope.max())
    if pico_max <= 0:
        return []
    limiar = pico_max * limiar_relativo
    distancia_min = max(1, int(distancia_min_s * sr))
    tempos: list[float] = []
    ultimo_pico = -distancia_min
    n = len(envelope)
    i = 0
    while i < n:
        if envelope[i] >= limiar:
            fim = i
            while fim < n and envelope[fim] >= limiar:
                fim += 1
            pico_local = i + int(np.argmax(envelope[i:fim]))
            if pico_local - ultimo_pico >= distancia_min:
                tempos.append(pico_local / sr)
                ultimo_pico = pico_local
            i = fim
        else:
            i += 1
    return tempos


def deficits_em_blocos(intervalos_s, esperados_s, sr: int, bloco: int = 512, folga_s: float = 0.001,
                       fracao_max: float = 0.4, maioria: float = 0.5) -> list[int]:
    """Para cada intervalo medido, quantos blocos inteiros de `bloco` amostras faltam nele em relação ao
    esperado (0 quando não falta um número inteiro de blocos, a ±`folga_s`). É a assinatura de uma captura
    que jogou blocos fora: o intervalo só encolhe, e sempre de blocos inteiros.

    Só conta como perda da captura o que é esparso e pequeno: um déficit maior que `fracao_max` do intervalo
    esperado (ex.: andamento errado, 0,5 s no lugar de 1,0 s) nunca conta, e se mais que `maioria` dos
    intervalos tem déficit, é desvio da fonte, não da captura — devolve tudo zero."""
    out: list[int] = []
    for iv, esp in zip(intervalos_s, esperados_s):
        falta = (float(esp) - float(iv)) * sr
        k = int(round(falta / bloco)) if bloco > 0 else 0
        ok = k >= 1 and abs(falta - k * bloco) <= folga_s * sr and falta <= fracao_max * float(esp) * sr
        out.append(k if ok else 0)
    if out and sum(1 for k in out if k) > maioria * len(out):
        return [0] * len(out)
    return out


def acentos(x: np.ndarray, sr: int, tempos: list[float], janela_s: float = 0.015,
            razao_min: float = 1.2) -> tuple[list[str], list[float]]:
    """Separa os cliques em `tempos` (de `cliques()`) em acento ('A', o bipe mais agudo — como os metrônomos
    marcam o 1 do compasso) e batida ('b'), pela frequência dominante dos primeiros `janela_s` de cada um.
    Devolve (tipos, frequências). Se os cliques não têm duas alturas (a mais aguda menos de `razao_min`
    vezes a mais grave), não inventa acento: tudo 'b'. Independe dos intervalos entre os cliques — vale
    mesmo numa captura que perdeu blocos."""
    x = np.asarray(x, dtype=np.float64)
    freqs: list[float] = []
    for t in tempos:
        a = max(0, int(round((t - 0.002) * sr)))  # o instante do clique é o pico do envelope, logo depois do ataque
        trecho = x[a:a + max(16, int(janela_s * sr))]
        freqs.append(freq_dominante(trecho, sr) if trecho.size >= 16 else 0.0)
    validas = [f for f in freqs if f > 0]
    if not validas or max(validas) < razao_min * min(validas):
        return ["b"] * len(freqs), freqs
    corte = math.sqrt(min(validas) * max(validas))
    return ["A" if f >= corte else "b" for f in freqs], freqs


def atraso_entre(a: np.ndarray, b: np.ndarray, sr: int) -> float:
    """Atraso (s) de `b` em relação a `a` por correlação cruzada: positivo quando `b` vem
    depois de `a` (ex.: atraso de ida e volta, ou o quanto o baixo entra depois do bumbo).

    Correlação cruzada via FFT (equivalente a `np.correlate(b, a, mode="full")`, mas O(n log n)
    em vez de O(n²) — a diferença importa: alguns segundos a 44100 Hz já levariam minutos com a
    convolução direta)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    a = a - a.mean()
    b = b - b.mean()
    n = len(a) + len(b) - 1
    n_fft = 1 << max(1, (n - 1).bit_length())  # próxima potência de 2
    correlacao_circular = np.fft.irfft(np.fft.rfft(b, n_fft) * np.conj(np.fft.rfft(a, n_fft)), n_fft)
    # correlacao_circular[0] é o lag 0; os lags negativos ficam "enrolados" no fim (FFT
    # circular) — [-(len(a)-1):] é um no-op (array inteiro) quando len(a) == 1, daí o guarda.
    cauda = correlacao_circular[-(len(a) - 1):] if len(a) > 1 else np.array([])
    correlacao = np.concatenate((cauda, correlacao_circular[:len(b)]))
    indice = int(np.argmax(correlacao))
    atraso_amostras = indice - (len(a) - 1)
    return atraso_amostras / sr


# ---------------------------------------------------------------------------
# Geração de sinais de teste
# ---------------------------------------------------------------------------

def _amplitude(dbfs: float) -> float:
    return 10 ** (dbfs / 20)


def _juntar_dbfs(dbfs: float | None, explicito: float | None, padrao: float = -12.0) -> float:
    """`dbfs` é o nome antigo (continua funcionando); `explicito` é o novo nome, mais claro
    sobre o que está sendo fixado (`dbfs_pico` ou `dbfs_rms`, conforme o tipo). Os dois só
    existem porque significam a mesma coisa com nomes diferentes — quando `explicito` vem
    preenchido ele manda, senão cai para `dbfs`, senão usa o padrão."""
    if explicito is not None:
        return explicito
    if dbfs is not None:
        return dbfs
    return padrao


def _sinal_seno(sr: int, duracao_s: float, freq: float, dbfs: float | None = None,
                dbfs_pico: float | None = None, fase: float = 0.0) -> np.ndarray:
    nivel = _juntar_dbfs(dbfs, dbfs_pico)
    n = int(round(duracao_s * sr))
    t = np.arange(n) / sr
    return _amplitude(nivel) * np.sin(2 * np.pi * freq * t + fase)


def _sinal_silencio(sr: int, duracao_s: float) -> np.ndarray:
    return np.zeros(int(round(duracao_s * sr)))


def _sinal_ruido(sr: int, duracao_s: float, dbfs: float | None = None, dbfs_rms: float | None = None,
                 semente: int = 0) -> np.ndarray:
    """Ruído branco gaussiano normalizado por RMS (convenção usual para ruído de calibração:
    "-12 dBFS" aqui é o RMS do sinal, não o pico — diferente do "seno", que é referenciado
    ao pico). Recorte de segurança em ±1.0 contra os raros picos gaussianos acima da escala."""
    nivel = _juntar_dbfs(dbfs, dbfs_rms)
    n = int(round(duracao_s * sr))
    rng = np.random.default_rng(semente)
    ruido = rng.standard_normal(n)
    rms_atual = float(np.sqrt(np.mean(np.square(ruido)))) or 1.0
    return np.clip(ruido / rms_atual * _amplitude(nivel), -1.0, 1.0)


def _sinal_cliques(sr: int, duracao_s: float, bpm: float, dbfs: float | None = None,
                    dbfs_pico: float | None = None, freq: float = 1000.0,
                    duracao_click_s: float = 0.005) -> np.ndarray:
    nivel = _juntar_dbfs(dbfs, dbfs_pico)
    n = int(round(duracao_s * sr))
    sinal = np.zeros(n)
    intervalo = 60.0 / bpm
    n_click = max(2, int(round(duracao_click_s * sr)))
    t_click = np.arange(n_click) / sr
    # Ataque instantâneo, decaimento exponencial: o pico do envelope fica bem no início da
    # janela (t=0), não no meio — com uma rampa de subida (ex.: metade de um Hann), o pico do
    # envelope só chega perto do fim da janela, o que atrasa sistematicamente a detecção de
    # `cliques()` em quase a duração inteira do clique (~5 ms a mais, comendo boa parte da
    # tolerância de detecção de tempo).
    envelope = np.exp(-t_click / (duracao_click_s / 5.0))
    onda_click = np.sin(2 * np.pi * freq * t_click) * envelope
    # O seno começa em 0 (sin(0) = 0) e o envelope já está decaindo antes dele chegar ao
    # primeiro pico de verdade — sem isso, o pico real da onda fica alguns dB abaixo do `nivel`
    # pedido (quanto mais curto `duracao_click_s` frente ao período de `freq`, maior o desvio).
    # Renormaliza pelo pico que a onda teve de fato, para `pico_dbfs(...)` bater com `nivel`.
    pico_onda = float(np.max(np.abs(onda_click))) or 1.0
    onda_click = onda_click / pico_onda * _amplitude(nivel)
    tempo = 0.0
    while True:
        inicio = int(round(tempo * sr))
        if inicio >= n:
            break
        fim = min(inicio + n_click, n)
        sinal[inicio:fim] += onda_click[:fim - inicio]
        tempo += intervalo
    return sinal


_TIPOS_GERAVEIS = {"seno", "cliques", "silencio", "ruido"}


def gerar_wav(caminho: Path, tipo: str, sr: int = 44100, **p) -> Path:
    """Gera um WAV sintético (mono, PCM 16 bits) em `caminho` e devolve o caminho.

    `tipo` é um de "seno" (freq, duracao_s, dbfs_pico=-12.0, fase=0.0), "cliques" (bpm,
    duracao_s, dbfs_pico=-12.0, freq=1000.0, duracao_click_s=0.005), "silencio" (duracao_s) ou
    "ruido" (duracao_s, dbfs_rms=-12.0, semente=0). `sr` é a taxa de amostragem (padrão 44100 Hz).

    `dbfs_pico` ("seno"/"cliques") é o PICO da onda (convenção usual para tom de teste: "0 dBFS"
    bate exatamente na escala cheia); `dbfs_rms` ("ruido") é o RMS (convenção usual para ruído
    de calibração). Use `pico_dbfs`/`rms_dbfs` para conferir. `dbfs` continua aceito como alias
    do nome antigo, com o mesmo significado de cada tipo — quem já chamava assim não quebra.
    """
    if tipo not in _TIPOS_GERAVEIS:
        raise ValueError(f"tipo de sinal desconhecido: {tipo!r} (esperado um de {sorted(_TIPOS_GERAVEIS)})")
    geradores = {
        "seno": _sinal_seno,
        "cliques": _sinal_cliques,
        "silencio": _sinal_silencio,
        "ruido": _sinal_ruido,
    }
    sinal = geradores[tipo](sr, **p)
    buffer = io.BytesIO()
    sf.write(buffer, sinal.astype(np.float64), sr, format="WAV", subtype="PCM_16")
    sandbox.escrever_bytes(Path(caminho), buffer.getvalue())
    return Path(caminho)


# ---------------------------------------------------------------------------
# Fala sintética (SAPI / System.Speech via PowerShell)
# ---------------------------------------------------------------------------

def _powershell(argumentos: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *argumentos],
        capture_output=True, text=True, timeout=timeout,
    )


def vozes_instaladas() -> list[dict]:
    """Vozes SAPI (System.Speech) instaladas: [{"nome", "cultura", "ativa"}, ...]."""
    r = _powershell(["-File", str(_PS_LISTAR_VOZES)], timeout=30)
    if r.returncode != 0:
        raise VozIndisponivel(f"não foi possível listar vozes SAPI: {(r.stderr or r.stdout).strip()}")
    vozes = []
    for linha in r.stdout.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        partes = linha.split("|")
        if len(partes) != 3:
            continue  # linha inesperada (poucos ou barras demais); ignora, não é uma voz válida
        nome, cultura, ativa = partes
        vozes.append({"nome": nome, "cultura": cultura, "ativa": ativa.strip().lower() == "true"})
    return vozes


def fala_tts(caminho: Path, texto: str, voz: str = "pt-BR") -> str:
    """Sintetiza `texto` em `caminho` (WAV) via SAPI (System.Speech.Synthesis).

    Usa a primeira voz instalada cuja cultura comece com `voz` (ex.: "pt-BR"); se nenhuma
    existir, cai para a voz padrão do Windows sem travar — o retorno ("Nome (cultura)" da voz
    usada de fato) deixa isso visível para quem chamou registrar a limitação.
    """
    caminho = Path(caminho)
    sandbox.garantir_escrita(caminho)  # a SAPI escreve o WAV direto; só validamos o caminho antes
    caminho.parent.mkdir(parents=True, exist_ok=True)
    texto_b64 = base64.b64encode(texto.encode("utf-8")).decode("ascii")
    r = _powershell(["-File", str(_PS_FALAR), "-TextoBase64", texto_b64, "-Caminho", str(caminho),
                      "-VozPrefixo", voz], timeout=60)
    if r.returncode != 0 or not caminho.exists():
        raise VozIndisponivel(f"SAPI falhou ao sintetizar: {(r.stderr or r.stdout).strip()}")
    ultima_linha = next((linha for linha in reversed(r.stdout.splitlines()) if linha.strip()), "?|?")
    nome, _, cultura = ultima_linha.strip().partition("|")
    return f"{nome} ({cultura})"
