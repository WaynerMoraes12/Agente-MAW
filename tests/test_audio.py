"""Medidas com sinais sintéticos (sem placa de som) + geração de fixtures + fala SAPI.

O único teste que toca a placa de som de verdade é `test_loopback_capta_tom_pela_placa_real`,
marcado `lento`: toca um tom curto e baixo (-30 dBFS, ~1 s) e não deve rodar em excesso.
"""
from __future__ import annotations
import threading
import time

import numpy as np
import pytest
import soundfile as sf

from maw_agent import audio, sandbox

SR = 44100


def _seno(freq: float, duracao_s: float, dbfs: float = -12.0, sr: int = SR, fase: float = 0.0) -> np.ndarray:
    n = int(round(duracao_s * sr))
    t = np.arange(n) / sr
    amp = 10 ** (dbfs / 20)
    return amp * np.sin(2 * np.pi * freq * t + fase)


# ---------------------------------------------------------------------------
# Medidas
# ---------------------------------------------------------------------------

def test_freq_dominante_440hz():
    x = _seno(440.0, 1.0)
    assert audio.freq_dominante(x, SR) == pytest.approx(440.0, abs=1.0)


def test_freq_dominante_outra_frequencia_e_duracao_curta():
    x = _seno(2000.0, 0.2)
    assert audio.freq_dominante(x, SR) == pytest.approx(2000.0, abs=2.0)


def test_freq_dominante_227hz_captura_curta():
    # 227 Hz é a fixture do AutoTune (deveria virar 220 Hz); 0,2 s é perto do que uma captura
    # curta de cenário real dá — a interpolação parabólica sub-bin precisa segurar ±1 Hz aqui.
    x = _seno(227.0, 0.2)
    assert audio.freq_dominante(x, SR) == pytest.approx(227.0, abs=1.0)


def test_rms_dbfs_menos12():
    # RMS não depende da forma de onda: um sinal de amplitude constante tem RMS == amplitude,
    # o que dá uma conta exata para conferir rms_dbfs (um seno "-12 dBFS" é referenciado ao
    # pico — ver test_pico_dbfs_seno_menos12dbfs — e teria RMS 3 dB abaixo disso).
    amplitude_constante = np.full(SR, 10 ** (-12.0 / 20))
    assert audio.rms_dbfs(amplitude_constante) == pytest.approx(-12.0, abs=0.2)


def test_pico_dbfs_seno_cheio_de_escala():
    x = _seno(1000.0, 0.5, dbfs=0.0)
    assert audio.pico_dbfs(x) == pytest.approx(0.0, abs=0.05)


def test_pico_dbfs_seno_menos12dbfs():
    x = _seno(1000.0, 0.5, dbfs=-12.0)
    assert audio.pico_dbfs(x) == pytest.approx(-12.0, abs=0.2)


def test_dbfs_do_silencio_e_menos_infinito():
    silencio = np.zeros(1000)
    assert audio.rms_dbfs(silencio) == float("-inf")
    assert audio.pico_dbfs(silencio) == float("-inf")
    assert audio.rms_dbfs(np.array([])) == float("-inf")


def test_ativo_limiar():
    alto = _seno(1000.0, 0.5, dbfs=-10.0)
    baixo = _seno(1000.0, 0.5, dbfs=-60.0)
    assert audio.ativo(alto) is True
    assert audio.ativo(baixo) is False
    assert audio.ativo(baixo, limiar_dbfs=-70.0) is True


def test_cliques_120bpm_oito_cliques_a_meio_segundo():
    x = audio._sinal_cliques(SR, 4.0, bpm=120.0, dbfs=-6.0)
    tempos = audio.cliques(x, SR)
    esperados = [i * 0.5 for i in range(8)]
    assert len(tempos) == 8
    for obtido, esperado in zip(tempos, esperados):
        assert obtido == pytest.approx(esperado, abs=0.005)


def test_cliques_em_silencio_nao_acha_nada():
    assert audio.cliques(np.zeros(SR), SR) == []


def test_atraso_entre_positivo_quando_b_vem_depois():
    a = np.zeros(SR)
    a[1000] = 1.0
    deslocamento = 220  # amostras (~5 ms a 44100 Hz)
    b = np.zeros(SR)
    b[1000 + deslocamento] = 1.0
    assert audio.atraso_entre(a, b, SR) == pytest.approx(deslocamento / SR, abs=1e-4)


def test_atraso_entre_negativo_quando_b_vem_antes():
    a = np.zeros(SR)
    a[2000] = 1.0
    b = np.zeros(SR)
    b[1500] = 1.0
    assert audio.atraso_entre(a, b, SR) == pytest.approx((1500 - 2000) / SR, abs=1e-4)


# ---------------------------------------------------------------------------
# gerar_wav
# ---------------------------------------------------------------------------

def test_gerar_wav_seno(tmp_path):
    caminho = audio.gerar_wav(tmp_path / "seno.wav", "seno", freq=1000.0, duracao_s=1.0, dbfs=-12.0)
    assert caminho.exists()
    x, sr = sf.read(str(caminho))
    assert audio.freq_dominante(x, sr) == pytest.approx(1000.0, abs=1.0)
    assert audio.pico_dbfs(x) == pytest.approx(-12.0, abs=0.3)  # "seno" é referenciado ao pico


def test_gerar_wav_cliques(tmp_path):
    caminho = audio.gerar_wav(tmp_path / "cliques.wav", "cliques", bpm=120.0, duracao_s=4.0)
    x, sr = sf.read(str(caminho))
    assert len(audio.cliques(x, sr)) == 8


def test_gerar_wav_silencio(tmp_path):
    caminho = audio.gerar_wav(tmp_path / "silencio.wav", "silencio", duracao_s=1.0)
    x, sr = sf.read(str(caminho))
    assert not audio.ativo(np.asarray(x, dtype=np.float64))


def test_gerar_wav_ruido(tmp_path):
    caminho = audio.gerar_wav(tmp_path / "ruido.wav", "ruido", duracao_s=1.0, dbfs=-12.0)
    x, sr = sf.read(str(caminho))
    assert audio.rms_dbfs(x) == pytest.approx(-12.0, abs=1.0)


def test_gerar_wav_dbfs_pico_e_alias_de_dbfs_para_seno(tmp_path):
    caminho = audio.gerar_wav(tmp_path / "seno_novo.wav", "seno", freq=1000.0, duracao_s=0.5, dbfs_pico=-6.0)
    x, sr = sf.read(str(caminho))
    assert audio.pico_dbfs(x) == pytest.approx(-6.0, abs=0.3)


def test_gerar_wav_dbfs_continua_funcionando_para_seno(tmp_path):
    # `dbfs` é o nome antigo: precisa continuar com o mesmo significado (pico) de sempre.
    caminho = audio.gerar_wav(tmp_path / "seno_velho.wav", "seno", freq=1000.0, duracao_s=0.5, dbfs=-6.0)
    x, sr = sf.read(str(caminho))
    assert audio.pico_dbfs(x) == pytest.approx(-6.0, abs=0.3)


def test_gerar_wav_dbfs_pico_para_cliques(tmp_path):
    caminho = audio.gerar_wav(tmp_path / "cliques_novo.wav", "cliques", bpm=120.0, duracao_s=1.0, dbfs_pico=-6.0)
    x, sr = sf.read(str(caminho))
    assert audio.pico_dbfs(x) == pytest.approx(-6.0, abs=0.3)


def test_gerar_wav_dbfs_rms_e_alias_de_dbfs_para_ruido(tmp_path):
    caminho = audio.gerar_wav(tmp_path / "ruido_novo.wav", "ruido", duracao_s=1.0, dbfs_rms=-9.0)
    x, sr = sf.read(str(caminho))
    assert audio.rms_dbfs(x) == pytest.approx(-9.0, abs=1.0)


def test_gerar_wav_tipo_invalido(tmp_path):
    with pytest.raises(ValueError):
        audio.gerar_wav(tmp_path / "x.wav", "invalido", duracao_s=1.0)


def test_gerar_wav_recusa_pasta_protegida(_sem_pastas_reais):
    alvo = _sem_pastas_reais / "x.wav"
    with pytest.raises(sandbox.EscritaProibida):
        audio.gerar_wav(alvo, "silencio", duracao_s=0.1)
    assert not alvo.exists()


# ---------------------------------------------------------------------------
# Loopback com dispositivo falso (sem placa de som real)
# ---------------------------------------------------------------------------

class _StreamFalso:
    def __init__(self, dados: bytes) -> None:
        self._dados = dados
        self._pos = 0

    def read(self, n_quadros: int, exception_on_overflow: bool = False) -> bytes:
        tamanho = n_quadros * 4  # float32 mono "por canal já achatado" nos testes abaixo
        bloco = self._dados[self._pos:self._pos + tamanho]
        self._pos += tamanho
        if len(bloco) < tamanho:
            bloco = bloco + b"\x00" * (tamanho - len(bloco))
        return bloco

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


def _pyaudio_falso_com_sinal(sinal_intercalado: np.ndarray, sr: int, canais: int):
    dados = sinal_intercalado.astype(np.float32).tobytes()

    class _PyAudioFalso:
        def __init__(self, *a, **k) -> None:
            pass

        def get_default_wasapi_loopback(self) -> dict:
            return {"defaultSampleRate": float(sr), "maxInputChannels": canais, "index": 0,
                    "name": "Falso [Loopback]"}

        def open(self, **kwargs):
            return _StreamFalso(dados)

        def terminate(self) -> None:
            pass

    return _PyAudioFalso


def _pyaudio_falso_sem_loopback():
    class _PyAudioFalso:
        def __init__(self, *a, **k) -> None:
            pass

        def get_default_wasapi_loopback(self):
            raise OSError("driver WASAPI indisponível (teste)")

        def terminate(self) -> None:
            pass

    return _PyAudioFalso


def _pyaudio_falso_open_falha(sr: int, canais: int):
    """`get_default_wasapi_loopback` acha o dispositivo, mas `open()` falha (ex.: dispositivo
    ocupado em modo exclusivo por outro app) — cenário diferente de "sem loopback nenhum".
    Devolve (classe, contador) — o contador registra quantas vezes `terminate()` foi chamado,
    pra conferir que o PyAudio aberto antes do `open()` falhar é sempre limpo."""
    chamadas_terminate = {"n": 0}

    class _PyAudioFalso:
        def __init__(self, *a, **k) -> None:
            pass

        def get_default_wasapi_loopback(self) -> dict:
            return {"defaultSampleRate": float(sr), "maxInputChannels": canais, "index": 0,
                    "name": "Falso [Loopback]"}

        def open(self, **kwargs):
            raise OSError("dispositivo ocupado (teste)")

        def terminate(self) -> None:
            chamadas_terminate["n"] += 1

    return _PyAudioFalso, chamadas_terminate


class _StreamQueCaiNoMeio:
    """A primeira leitura funciona; a segunda em diante levanta OSError — simula o driver
    caindo no meio de uma captura já em andamento (diferente de nunca ter aberto)."""

    def __init__(self, dados: bytes) -> None:
        self._dados = dados
        self._chamadas = 0

    def read(self, n_quadros: int, exception_on_overflow: bool = False) -> bytes:
        self._chamadas += 1
        if self._chamadas > 1:
            raise OSError("dispositivo caiu no meio da captura (teste)")
        tamanho = n_quadros * 4
        bloco = self._dados[:tamanho]
        if len(bloco) < tamanho:
            bloco = bloco + b"\x00" * (tamanho - len(bloco))
        return bloco

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


def _pyaudio_falso_com_stream_que_cai(sr: int, canais: int, dados: bytes):
    class _PyAudioFalso:
        def __init__(self, *a, **k) -> None:
            pass

        def get_default_wasapi_loopback(self) -> dict:
            return {"defaultSampleRate": float(sr), "maxInputChannels": canais, "index": 0,
                    "name": "Falso [Loopback]"}

        def open(self, **kwargs):
            return _StreamQueCaiNoMeio(dados)

        def terminate(self) -> None:
            pass

    return _PyAudioFalso


class _StreamComBugInterno:
    """`read()` levanta algo que não é `OSError` — simula um bug nosso na thread de captura,
    não uma falha de dispositivo (não pode virar `LoopbackIndisponivel`)."""

    def read(self, n_quadros: int, exception_on_overflow: bool = False) -> bytes:
        raise ValueError("bug interno (teste)")

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


def _pyaudio_falso_com_stream_com_bug(sr: int, canais: int):
    class _PyAudioFalso:
        def __init__(self, *a, **k) -> None:
            pass

        def get_default_wasapi_loopback(self) -> dict:
            return {"defaultSampleRate": float(sr), "maxInputChannels": canais, "index": 0,
                    "name": "Falso [Loopback]"}

        def open(self, **kwargs):
            return _StreamComBugInterno()

        def terminate(self) -> None:
            pass

    return _PyAudioFalso


class _StreamTravado:
    """`read()` nunca retorna — simula um driver travado, só para testar o teto de segurança
    do `Gravador` (a thread é `daemon=True`: fica presa, mas não trava o processo/teste)."""

    def read(self, n_quadros: int, exception_on_overflow: bool = False) -> bytes:
        threading.Event().wait()
        return b""  # nunca chega aqui

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


def _pyaudio_falso_com_stream_travado(sr: int, canais: int):
    class _PyAudioFalso:
        def __init__(self, *a, **k) -> None:
            pass

        def get_default_wasapi_loopback(self) -> dict:
            return {"defaultSampleRate": float(sr), "maxInputChannels": canais, "index": 0,
                    "name": "Falso [Loopback]"}

        def open(self, **kwargs):
            return _StreamTravado()

        def terminate(self) -> None:
            pass

    return _PyAudioFalso


def test_captura_loopback_sem_dispositivo_levanta_indisponivel(monkeypatch):
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_sem_loopback())
    with pytest.raises(audio.LoopbackIndisponivel):
        audio.captura_loopback(0.1)


def test_captura_loopback_open_falha_levanta_indisponivel(monkeypatch):
    classe, chamadas_terminate = _pyaudio_falso_open_falha(SR, 2)
    monkeypatch.setattr(audio.pyaudio, "PyAudio", classe)
    with pytest.raises(audio.LoopbackIndisponivel):
        audio.captura_loopback(0.1)
    assert chamadas_terminate["n"] == 1  # o PyAudio aberto antes do open() falhar foi limpo


def test_captura_loopback_levanta_indisponivel_se_cair_no_meio(monkeypatch):
    mono = _seno(1000.0, 1.0, dbfs=-6.0)
    dados = np.repeat(mono[:, None], 2, axis=1).astype(np.float32).tobytes()
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_com_stream_que_cai(SR, 2, dados))
    with pytest.raises(audio.LoopbackIndisponivel):
        audio.captura_loopback(1.0)


def test_captura_loopback_le_sinal_conhecido(monkeypatch):
    canais = 2
    mono = _seno(1000.0, 0.5, dbfs=-6.0)
    intercalado = np.repeat(mono[:, None], canais, axis=1)
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_com_sinal(intercalado, SR, canais))
    sinal, sr = audio.captura_loopback(0.5)
    assert sr == SR
    assert audio.freq_dominante(sinal, sr) == pytest.approx(1000.0, abs=2.0)


def test_gravador_sem_dispositivo_levanta_no_enter(monkeypatch):
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_sem_loopback())
    with pytest.raises(audio.LoopbackIndisponivel):
        with audio.Gravador():
            pass  # nunca deveria chegar aqui


def test_gravador_open_falha_levanta_indisponivel_no_enter(monkeypatch):
    classe, chamadas_terminate = _pyaudio_falso_open_falha(SR, 2)
    monkeypatch.setattr(audio.pyaudio, "PyAudio", classe)
    with pytest.raises(audio.LoopbackIndisponivel):
        with audio.Gravador():
            pass  # nunca deveria chegar aqui
    assert chamadas_terminate["n"] == 1  # o PyAudio aberto antes do open() falhar foi limpo


def test_gravador_levanta_indisponivel_se_a_captura_cair_no_meio(monkeypatch):
    mono = _seno(1000.0, 1.0, dbfs=-6.0)
    dados = np.repeat(mono[:, None], 2, axis=1).astype(np.float32).tobytes()
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_com_stream_que_cai(SR, 2, dados))
    with pytest.raises(audio.LoopbackIndisponivel):
        with audio.Gravador(segundos_max=2.0):
            time.sleep(0.1)  # dá tempo da thread de fundo bater no segundo read() e cair


def test_gravador_propaga_excecao_que_nao_e_oserror_da_thread_de_captura(monkeypatch):
    """Um bug nosso na thread de captura (não uma falha de dispositivo) não pode virar
    `LoopbackIndisponivel` — isso esconderia o bug atrás de um "sem loopback" errado."""
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_com_stream_com_bug(SR, 2))
    with pytest.raises(ValueError, match="bug interno"):
        with audio.Gravador(segundos_max=2.0):
            time.sleep(0.1)  # dá tempo da thread de fundo bater no read() e levantar o bug


def test_gravador_nao_mascara_excecao_do_bloco_quando_a_captura_tambem_cai(monkeypatch):
    """Se o bloco `with` já está saindo com uma exceção sua, a queda da captura (em outra
    thread) não pode substituí-la — só anexar uma nota, para não esconder o erro do cenário."""
    mono = _seno(1000.0, 1.0, dbfs=-6.0)
    dados = np.repeat(mono[:, None], 2, axis=1).astype(np.float32).tobytes()
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_com_stream_que_cai(SR, 2, dados))
    with pytest.raises(ValueError, match="erro do cenario") as excinfo:
        with audio.Gravador(segundos_max=2.0):
            time.sleep(0.1)
            raise ValueError("erro do cenario")
    notas = getattr(excinfo.value, "__notes__", [])
    assert any("loopback" in nota for nota in notas)


def test_gravador_registra_limitacao_quando_a_thread_nao_termina(monkeypatch):
    monkeypatch.setattr(audio, "_FOLGA_JOIN_S", 0.05)
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_com_stream_travado(SR, 2))
    with audio.Gravador(segundos_max=0.05) as g:
        pass
    assert g.limitacao is not None
    assert "não terminou" in g.limitacao


def test_gravador_captura_enquanto_o_bloco_with_roda(monkeypatch):
    canais = 2
    mono = _seno(440.0, 1.0, dbfs=-6.0)
    intercalado = np.repeat(mono[:, None], canais, axis=1)
    monkeypatch.setattr(audio.pyaudio, "PyAudio", _pyaudio_falso_com_sinal(intercalado, SR, canais))
    with audio.Gravador(segundos_max=1.0) as g:
        time.sleep(0.2)  # "o cenário toca" por um instante enquanto a thread de fundo grava
    sinal, sr = g.sinal
    assert sr == SR
    assert sinal.size > 0
    assert audio.freq_dominante(sinal, sr) == pytest.approx(440.0, abs=3.0)


# ---------------------------------------------------------------------------
# Fala sintética (SAPI / System.Speech)
# ---------------------------------------------------------------------------

def test_vozes_instaladas_lista_pelo_menos_uma():
    vozes = audio.vozes_instaladas()
    assert len(vozes) >= 1
    assert all({"nome", "cultura", "ativa"} <= v.keys() for v in vozes)


def test_vozes_instaladas_ignora_linhas_malformadas(monkeypatch):
    class _ResultadoFalso:
        returncode = 0
        stderr = ""
        stdout = "\n".join([
            "Voz Normal|pt-BR|True",
            "sem pipe nenhum",
            "so|um-pipe",
            "Voz com Barra Sobrando|pt-BR|True|sobrando",
        ])

    monkeypatch.setattr(audio, "_powershell", lambda *a, **k: _ResultadoFalso())
    vozes = audio.vozes_instaladas()  # não pode levantar ValueError por causa das linhas ruins
    nomes = {v["nome"] for v in vozes}
    # só a linha com exatamente 3 campos (2 barras) é uma voz válida; poucas ou barras demais
    # (a "Voz com Barra Sobrando" tem 3 barras -> 4 campos) são descartadas, não aceitas torto.
    assert nomes == {"Voz Normal"}


def test_fala_tts_gera_wav_com_sinal(tmp_path):
    caminho = tmp_path / "fala.wav"
    voz_usada = audio.fala_tts(caminho, "Este é um teste.", voz="pt-BR")
    assert caminho.exists()
    assert isinstance(voz_usada, str) and voz_usada
    x, sr = sf.read(str(caminho))
    x = np.asarray(x, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    assert audio.ativo(x, limiar_dbfs=-40.0)


def test_fala_tts_recusa_pasta_protegida(_sem_pastas_reais):
    alvo = _sem_pastas_reais / "fala.wav"
    with pytest.raises(sandbox.EscritaProibida):
        audio.fala_tts(alvo, "teste")
    assert not alvo.exists()


# ---------------------------------------------------------------------------
# Placa de som real (lento): tom curto e baixo, medido pelo loopback
# ---------------------------------------------------------------------------

@pytest.mark.lento
def test_loopback_capta_tom_pela_placa_real(tmp_path):
    """Toca um tom de 1 kHz a -30 dBFS por ~1 s pela placa interna e confere no loopback.

    Também mede se o loopback ainda enxerga sinal com o terminal de saída mudo (o achado vai
    para o relatório da task: a execução noturna depende de saber se dá para deixar o PC mudo
    e ainda assim medir pelo loopback). Restaura o volume/mudo originais mesmo se o teste falhar.
    """
    import winsound
    from pycaw.utils import AudioUtilities

    caminho = audio.gerar_wav(tmp_path / "tom_-30dbfs_1s.wav", "seno", freq=1000.0, duracao_s=1.0, dbfs=-30.0)

    alto_falante = AudioUtilities.GetSpeakers()
    ev = alto_falante.EndpointVolume
    volume_original = ev.GetMasterVolumeLevelScalar()
    mudo_original = ev.GetMute()

    resultados: dict[str, dict] = {}
    try:
        condicoes = [("mudo", 1, volume_original), ("volume_baixo_20pct", 0, 0.20)]
        for nome_condicao, mudo, volume in condicoes:
            ev.SetMute(mudo, None)
            ev.SetMasterVolumeLevelScalar(volume, None)
            with audio.Gravador(segundos_max=3.0) as g:
                winsound.PlaySound(str(caminho), winsound.SND_FILENAME)
                time.sleep(0.2)  # cauda do buffer do loopback
            sinal, sr = g.sinal
            resultados[nome_condicao] = {
                "ativo_-50dbfs": audio.ativo(sinal, limiar_dbfs=-50.0),
                "rms_dbfs": round(audio.rms_dbfs(sinal), 1),
                "freq_dominante_hz": round(audio.freq_dominante(sinal, sr), 1),
            }
    finally:
        ev.SetMute(mudo_original, None)
        ev.SetMasterVolumeLevelScalar(volume_original, None)

    print("RESULTADO LOOPBACK x VOLUME/MUDO:", resultados)

    # Sanidade: em volume baixo (mas sem mudo) o loopback tem que enxergar o tom — se isso
    # falhar, o loopback não serve para a execução noturna e o achado é outro (mais grave).
    assert resultados["volume_baixo_20pct"]["ativo_-50dbfs"] is True
    assert resultados["volume_baixo_20pct"]["freq_dominante_hz"] == pytest.approx(1000.0, abs=5.0)
    # O caso "mudo" não tem asserção de resultado esperado: é exatamente o que este teste
    # existe para descobrir. O resultado (`resultados["mudo"]`) vai para o relatório da task,
    # não para um assert que quebraria a suíte dependendo do comportamento desta máquina/driver.
