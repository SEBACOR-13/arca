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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        token=os.getenv("ASTRA_DB_TOKEN", ""),
        scb_path=os.getenv("ASTRA_SCB_PATH", ""),
        scb_b64=os.getenv("ASTRA_SCB_B64", ""),
        keyspace=os.getenv("ASTRA_KEYSPACE", "shortener"),
        gh_repo=os.getenv("GH_REPO", "SEBACOR-13/arca"),
    )
