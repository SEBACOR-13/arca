"""Descarga el Secure Connect Bundle desde la API de Astra.

Por que existe este script
--------------------------
El driver de Cassandra no se conecta a Astra sin el Secure Connect Bundle,
que es un .zip con los certificados. En CI eso obligaba a guardar el bundle
entero como secret en base64: dieciseis mil caracteres pegados a mano en un
formulario web, que es justo la clase de cosa que se corta y luego falla con
un "invalid input" sin explicacion.

No hace falta. La API de Astra entrega una URL de descarga del bundle a
quien presente el mismo token que ya usamos para conectarnos. Asi el
workflow tiene un secret menos y el bundle nunca queda congelado en una
variable: se pide fresco en cada ejecucion.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

API = "https://api.astra.datastax.com/v2/databases/{db}/secureBundleURL"


def descargar(db_id: str, token: str, destino: Path) -> Path:
    peticion = urllib.request.Request(
        API.format(db=db_id),
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(peticion, timeout=60) as respuesta:
        url = json.load(respuesta)["downloadURL"]

    with urllib.request.urlopen(url, timeout=120) as origen:
        destino.write_bytes(origen.read())
    return destino


def main() -> int:
    db_id = os.environ.get("ASTRA_DB_ID", "").strip()
    token = os.environ.get("ASTRA_DB_TOKEN", "").strip()
    destino = Path(sys.argv[1] if len(sys.argv) > 1 else "secure-connect.zip")

    if not db_id or not token:
        print("Faltan ASTRA_DB_ID o ASTRA_DB_TOKEN.", file=sys.stderr)
        return 1

    descargar(db_id, token, destino)
    print(f"bundle de {destino.stat().st_size} bytes en {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
