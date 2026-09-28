"""Pré-voo: o que falta vira limitação declarada, nunca um teste pulado em silêncio."""
from __future__ import annotations
import ctypes
import platform
import shutil
import subprocess
import winreg
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

from . import config


@dataclass
class Verificacao:
    nome: str
    ok: bool
    detalhe: str
    bloqueia: bool
    afeta: list[str]

    def como_dict(self) -> dict:
        return asdict(self)


EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def _processos() -> list[str]:
    return [p.info["name"] or "" for p in psutil.process_iter(["name"])]


def _dispositivos_audio() -> list[str]:
    try:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        try:
            return sorted({p.get_device_info_by_index(i)["name"] for i in range(p.get_device_count())})
        finally:
            p.terminate()
    except Exception as e:  # sem PortAudio utilizável: registrado, não escondido
        return [f"(erro ao listar: {e})"]


def _loopback_padrao() -> str | None:
    """Nome do dispositivo de loopback WASAPI da saída de áudio padrão, se existir.

    Sem VB-Cable (decisão de 28/09), este é o caminho de observação independente: a MAW
    toca pela placa interna e o agente captura por aqui. Nunca levanta: PortAudio
    ausente/quebrado ou nenhum loopback disponível viram None (limitação declarada,
    não erro escondido)."""
    try:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        try:
            return p.get_default_wasapi_loopback()["name"]
        finally:
            p.terminate()
    except Exception:
        return None


def _portas_midi() -> list[str]:
    try:
        import mido
        return sorted(set(mido.get_input_names()) | set(mido.get_output_names()))
    except Exception as e:
        return [f"(erro ao listar: {e})"]


def _python310() -> tuple[str | None, str]:
    """Localiza Python 3.10 e retorna (caminho, detalhe)."""
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")
    if not Path(uv).exists():
        return None, "uv não encontrado"
    try:
        p = subprocess.run([uv, "python", "find", "3.10"], capture_output=True, text=True, timeout=30)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip(), p.stdout.strip()
        return None, "Python 3.10 ausente (uv python install 3.10)"
    except subprocess.TimeoutExpired:
        return None, "Python 3.10 ausente (timeout ao procurar)"
    except OSError:
        return None, "Python 3.10 ausente (uv python install 3.10)"


def _ffmpeg() -> str | None:
    local = config.FERRAMENTAS / "ffmpeg" / "bin" / "ffmpeg.exe"
    return str(local) if local.exists() else shutil.which("ffmpeg")


def _msbuild() -> tuple[str | None, str]:
    """Localiza MSBuild; retorna (caminho, detalhe).

    Trata FileNotFoundError, TimeoutExpired e OSError para nunca quebrar.
    """
    try:
        from .build import localizar_msbuild
        caminho = str(localizar_msbuild())
        return caminho, caminho
    except FileNotFoundError as e:
        return None, str(e)
    except subprocess.TimeoutExpired:
        return None, "MSBuild não encontrado (timeout ao procurar com vswhere)"
    except OSError as e:
        return None, f"Erro ao procurar MSBuild: {e}"


def _nao_perturbe() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings") as k:
            v, _ = winreg.QueryValueEx(k, "NOC_GLOBAL_SETTING_TOASTS_ENABLED")
            return v == 0
    except OSError:
        return False


def _dpi_e_tela() -> tuple[int, str, int]:
    """Retorna (dpi, resolucao, escala_percent) com processo DPI aware.

    Tenta ativar DPI awareness para obter valores físicos, não virtualizados.
    """
    u = ctypes.windll.user32
    # Tentar ativar DPI awareness (de mais recente para mais antigo)
    try:
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                u.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass  # Falha é ok; já pode estar ativado

    try:
        dpi = u.GetDpiForSystem()
    except AttributeError:
        dpi = 96

    resolucao = f"{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}"
    escala = dpi * 100 // 96
    return dpi, resolucao, escala


def verificar_tudo() -> list[Verificacao]:
    vs: list[Verificacao] = []
    aberta = any(n.lower() == "maw_app.exe" for n in _processos())
    vs.append(Verificacao("maw_fechada", not aberta,
                          "MAW está aberta: feche-a antes da fase de GUI" if aberta else "MAW fechada",
                          False, ["gui"]))
    msb_path, msb_detail = _msbuild()
    vs.append(Verificacao("msbuild", msb_path is not None, msb_detail, True, []))
    vs.append(Verificacao("edge", EDGE.exists(), str(EDGE) if EDGE.exists() else "Edge não encontrado", True, []))
    livre = shutil.disk_usage(config.RAIZ).free / 1e9
    vs.append(Verificacao("disco", livre >= 30, f"{livre:.0f} GB livres (mínimo 30)", True, []))
    audio = _dispositivos_audio()
    cabo = any("CABLE" in d.upper() for d in audio)
    vs.append(Verificacao("entrada_injetavel", cabo,
                          "cabo virtual presente: dá para injetar sinal conhecido na entrada da MAW" if cabo else
                          "nenhum cabo virtual instalado: sem entrada injetável (modo placa interna, sem "
                          "sinal conhecido na entrada)",
                          False, ["vb-cable"]))
    loop_padrao = _loopback_padrao()
    vs.append(Verificacao("loopback", loop_padrao is not None,
                          loop_padrao or "nenhum dispositivo de loopback da saída padrão (pyaudiowpatch)",
                          False, ["loopback"]))
    py_path, py_detail = _python310()
    vs.append(Verificacao("python310", py_path is not None, py_path or py_detail,
                          False, ["python310"]))
    ff = _ffmpeg()
    vs.append(Verificacao("ffmpeg", bool(ff), ff or "ffmpeg ausente", False, ["ffmpeg"]))
    midi = _portas_midi()
    loop = any("loopback" in m.lower() for m in midi)
    vs.append(Verificacao("midi_loopback", loop, ", ".join(midi) or "nenhuma porta MIDI", False, ["midi-loopback"]))
    np_ok = _nao_perturbe()
    vs.append(Verificacao("nao_perturbe", np_ok, "Não perturbe ligado" if np_ok else
                          "Não perturbe desligado: notificações podem roubar o foco da GUI", False, []))
    dpi, tela, escala = _dpi_e_tela()
    vs.append(Verificacao("tela", True, f"{tela} a {dpi} dpi ({escala}%)", False, []))
    return vs


def requisitos_ausentes(vs: list[Verificacao]) -> set[str]:
    return {r for v in vs if not v.ok for r in v.afeta}


def ambiente() -> dict:
    dpi, tela, escala = _dpi_e_tela()
    msb_path, _ = _msbuild()
    return {
        "windows": f"{platform.system()} {platform.release()} ({platform.version()})",
        "python": platform.python_version(),
        "cpu": platform.processor(),
        "nucleos": psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "dpi": dpi,
        "resolucao": tela,
        "escala": escala,
        "dispositivos_audio": _dispositivos_audio(),
        "portas_midi": _portas_midi(),
        "msbuild": msb_path,
        "edge": str(EDGE) if EDGE.exists() else None,
    }
