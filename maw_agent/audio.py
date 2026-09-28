"""Áudio pela placa interna: captura por loopback WASAPI, medidas e geração de sinais de teste.

Sem VB-Cable: a saída vai pela placa física e a entrada é o loopback WASAPI da própria saída
padrão (`pyaudiowpatch`). Toda medida trabalha em float64 normalizado em -1..1 (cheio de escala
= 1.0 = 0 dBFS). Gravar e medir nunca é "achado" por si: quem chama decide o que fazer com o
número.
"""
from __future__ import annotations
import base64
import io
import subprocess
import threading
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf

from . import sandbox

_RECURSOS = Path(__file__).parent / "recursos"
_PS_LISTAR_VOZES = _RECURSOS / "listar_vozes_sapi.ps1"
_PS_FALAR = _RECURSOS / "falar_sapi.ps1"
_TAMANHO_BLOCO = 1024  # quadros por leitura do stream de loopback
_FOLGA_JOIN_S = 5.0  # folga do join() sobre segundos_max, pra dar tempo de uma leitura terminar


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


def captura_loopback(segundos: float) -> tuple[np.ndarray, int]:
    """Grava `segundos` do loopback WASAPI da saída padrão. Devolve (sinal_mono, taxa_amostragem).

    Levanta `LoopbackIndisponivel` se não houver loopback ou o stream não abrir.
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
        try:
            alvo = int(segundos * sr)
            blocos: list[bytes] = []
            lidos = 0
            while lidos < alvo:
                n = min(_TAMANHO_BLOCO, alvo - lidos)
                try:
                    blocos.append(stream.read(n, exception_on_overflow=False))
                except OSError as e:
                    raise LoopbackIndisponivel(f"a captura do loopback caiu no meio: {e}") from e
                lidos += n
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        p.terminate()
    return _para_mono(b"".join(blocos), canais), sr


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
    """

    def __init__(self, segundos_max: float = 10.0) -> None:
        self.segundos_max = segundos_max
        self.sinal: tuple[np.ndarray, int] | None = None
        self.limitacao: str | None = None
        self._p: "pyaudio.PyAudio | None" = None
        self._stream = None
        self._sr = 0
        self._canais = 1
        self._blocos: list[bytes] = []
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._erro: BaseException | None = None

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
        self._thread = threading.Thread(target=self._laco, daemon=True)
        self._thread.start()
        return self

    def _laco(self) -> None:
        maximo = int(self.segundos_max * self._sr)
        lidos = 0
        try:
            while not self._parar.is_set() and lidos < maximo:
                self._blocos.append(self._stream.read(_TAMANHO_BLOCO, exception_on_overflow=False))
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
