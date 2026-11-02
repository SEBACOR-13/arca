"""Configuracion leida del entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    token: str
    scb_path: str
    scb_b64: str
    keyspace: str
    gh_repo: str

    @property
    def configured(self) -> bool:
        return bool(self.token) and bool(self.scb_path or self.scb_b64)


def _var(nombre: str, por_defecto: str = "") -> str:
    """Lee una variable de entorno y le quita los espacios de los extremos.

    Estos valores se pegan a mano en formularios web (Render, GitHub
    Secrets) y es facil que arrastren un salto de linea. Un token con un
    '\\n' al final falla la autenticacion CQL con "Bad credentials", que no
    se parece en nada a la causa real.
    """
    return os.getenv(nombre, por_defecto).strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        token=_var("ASTRA_DB_TOKEN"),
        scb_path=_var("ASTRA_SCB_PATH"),
        scb_b64=_var("ASTRA_SCB_B64"),
        keyspace=_var("ASTRA_KEYSPACE", "shortener"),
        gh_repo=_var("GH_REPO", "SEBACOR-13/arca"),
    )
