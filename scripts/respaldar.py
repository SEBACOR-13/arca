"""Respaldo desatendido. Lo ejecuta GitHub Actions con cron.

    python scripts/respaldar.py [--incremental-desde <writetime>]

Escribe el paquete en respaldos/ e imprime su ruta en la salida estandar,
para que el workflow la recoja y la suba como asset de una release.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.respaldo import conectar, estado, exportar  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incremental-desde", type=int, default=None)
    parser.add_argument("--etiqueta", default="programado")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.configured:
        print("ERROR: faltan ASTRA_DB_TOKEN y el Secure Connect Bundle", file=sys.stderr)
        return 1

    sesion = conectar(settings)
    print(f"conectado al keyspace {settings.keyspace}")
    for t in estado(sesion):
        print(f"  {t['tabla']:<18} {t['filas']:>6} filas")

    tipo = "incremental" if args.incremental_desde else "completo"
    marca = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    destino = pathlib.Path("respaldos") / f"{tipo}-{marca}.tar.gz"

    manifiesto = exportar(
        sesion, destino, desde_us=args.incremental_desde, etiqueta=args.etiqueta
    )

    print()
    print(f"paquete: {destino}  ({manifiesto['bytes_paquete']} bytes)")
    for d in manifiesto["tablas"]:
        print(f"  {d['tabla']:<18} guardadas={d['guardadas']:<6} leidas={d['leidas']:<6}")
    print(f"  hasta_writetime: {manifiesto['hasta_writetime']}")

    # Lo que el workflow necesita para crear la release
    salida = pathlib.Path("respaldos/ultimo.json")
    salida.write_text(json.dumps({
        "archivo": str(destino),
        "nombre": destino.name,
        "tipo": tipo,
        "momento": manifiesto["momento"],
        "hasta_writetime": manifiesto["hasta_writetime"],
        "filas": sum(d["guardadas"] for d in manifiesto["tablas"]),
    }, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
