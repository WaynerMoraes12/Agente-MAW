"""O cache por usuário que o servidor de IA da MAW cria (modelos de separação em %LOCALAPPDATA%\MAW, desde o
main 419b4c4). Rodando a MAW, a E2E faz o servidor criá-lo; ele não existia antes da fase e não pode ficar para trás.
Se já existia (é do usuário), fica intocado."""
from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from . import config, sandbox


def pasta() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "MAW"


@contextmanager
def temporario(limitar: Callable[[str], None], onde: Path | None = None) -> Iterator[None]:
    """Durante o bloco a MAW pode criar o cache; no fim ele é apagado só se não existia antes."""
    alvo = Path(onde) if onde is not None else pasta()
    existia = alvo.exists()
    try:
        yield
    finally:
        if not existia and alvo.exists():
            try:
                sandbox.garantir_escrita(alvo)
                shutil.rmtree(alvo)
                limitar(f"o cache por usuário da MAW ({alvo}) foi criado pela MAW durante a fase e apagado no fim "
                        "(não existia antes)")
            except OSError as ex:
                limitar(f"o cache por usuário da MAW ({alvo}) foi criado pela MAW durante a fase e não pôde ser "
                        f"apagado: {ex}")
