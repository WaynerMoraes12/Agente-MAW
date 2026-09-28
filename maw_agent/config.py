"""Caminhos e constantes do agente. Nada aqui escreve em disco."""
from __future__ import annotations
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
WORK = RAIZ / "work"
PRIVADO = RAIZ / "privado"
ESPELHO = WORK / "espelho"
ALVOS_DIR = WORK / "alvos"
BACKUPS = WORK / "appdata-backup"
FERRAMENTAS = WORK / "ferramentas"
RELATORIOS = PRIVADO / "relatorios"
HISTORICO = PRIVADO / "historico" / "achados.json"
CATALOGO = PRIVADO / "catalogo" / "funcionalidades.yaml"
PRINCIPIOS = PRIVADO / "catalogo" / "principios.yaml"
TERMOS_PROIBIDOS = PRIVADO / "catalogo" / "termos-proibidos.txt"
APPDATA_MAW = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "MAW"
URL_MAW = "https://github.com/WaynerMoraes12/MAW.git"
USUARIO = Path(os.environ.get("USERPROFILE", str(Path.home())))


def pastas_protegidas() -> list[Path]:
    """Pastas da MAW do usuário: só leitura para o agente.

    `MAW_AGENTE_PROTEGIDAS` (separado por `;`) substitui a descoberta — usado nos testes.
    """
    override = os.environ.get("MAW_AGENTE_PROTEGIDAS")
    if override:
        return [Path(p).resolve() for p in override.split(";") if p]
    return sorted(p.resolve() for p in USUARIO.glob("MAW*") if p.is_dir())
