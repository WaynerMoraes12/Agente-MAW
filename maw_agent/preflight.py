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


def _portas_midi() -> list[str]:
    try:
        import mido
        return sorted(set(mido.get_input_names()) | set(mido.get_output_names()))
    except Exception as e:
        return [f"(erro ao listar: {e})"]


def _python310() -> str | None:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")
    try:
        p = subprocess.run([uv, "python", "find", "3.10"], capture_output=True, text=True, timeout=30)
        return p.stdout.strip() or None if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ffmpeg() -> str | None:
    local = config.FERRAMENTAS / "ffmpeg" / "bin" / "ffmpeg.exe"
    return str(local) if local.exists() else shutil.which("ffmpeg")


def _nao_perturbe() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings") as k:
            v, _ = winreg.QueryValueEx(k, "NOC_GLOBAL_SETTING_TOASTS_ENABLED")
            return v == 0
    except OSError:
        return False


def _dpi_e_tela() -> tuple[int, str]:
    u = ctypes.windll.user32
    try:
        dpi = u.GetDpiForSystem()
    except AttributeError:
        dpi = 96
    return dpi, f"{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}"


def verificar_tudo() -> list[Verificacao]:
    vs: list[Verificacao] = []
    aberta = any(n.lower() == "maw_app.exe" for n in _processos())
    vs.append(Verificacao("maw_fechada", not aberta,
                          "MAW está aberta: feche-a antes da fase de GUI" if aberta else "MAW fechada",
                          False, ["gui"]))
    try:
        from .build import localizar_msbuild
        vs.append(Verificacao("msbuild", True, str(localizar_msbuild()), True, []))
    except FileNotFoundError as e:
        vs.append(Verificacao("msbuild", False, str(e), True, []))
    vs.append(Verificacao("edge", EDGE.exists(), str(EDGE) if EDGE.exists() else "Edge não encontrado", True, []))
    livre = shutil.disk_usage(config.RAIZ).free / 1e9
    vs.append(Verificacao("disco", livre >= 30, f"{livre:.0f} GB livres (mínimo 30)", True, []))
    audio = _dispositivos_audio()
    cabo = any("CABLE" in d.upper() for d in audio)
    vs.append(Verificacao("vb_cable", cabo, "VB-Cable presente" if cabo else "VB-Cable não instalado",
                          False, ["vb-cable"]))
    py = _python310()
    vs.append(Verificacao("python310", bool(py), py or "Python 3.10 ausente (uv python install 3.10)",
                          False, ["python310"]))
    ff = _ffmpeg()
    vs.append(Verificacao("ffmpeg", bool(ff), ff or "ffmpeg ausente", False, ["ffmpeg"]))
    midi = _portas_midi()
    loop = any("loopback" in m.lower() for m in midi)
    vs.append(Verificacao("midi_loopback", loop, ", ".join(midi) or "nenhuma porta MIDI", False, ["midi-loopback"]))
    np_ok = _nao_perturbe()
    vs.append(Verificacao("nao_perturbe", np_ok, "Não perturbe ligado" if np_ok else
                          "Não perturbe desligado: notificações podem roubar o foco da GUI", False, []))
    dpi, tela = _dpi_e_tela()
    vs.append(Verificacao("tela", True, f"{tela} a {dpi} dpi ({dpi * 100 // 96}%)", False, []))
    return vs


def requisitos_ausentes(vs: list[Verificacao]) -> set[str]:
    return {r for v in vs if not v.ok for r in v.afeta}


def ambiente() -> dict:
    dpi, tela = _dpi_e_tela()
    try:
        from .build import localizar_msbuild
        msb = str(localizar_msbuild())
    except FileNotFoundError:
        msb = None
    return {
        "windows": f"{platform.system()} {platform.release()} ({platform.version()})",
        "python": platform.python_version(),
        "cpu": platform.processor(),
        "nucleos": psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "dpi": dpi,
        "resolucao": tela,
        "dispositivos_audio": _dispositivos_audio(),
        "portas_midi": _portas_midi(),
        "msbuild": msb,
        "edge": str(EDGE) if EDGE.exists() else None,
    }
