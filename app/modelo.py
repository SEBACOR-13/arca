"""Descripcion de lo que hay que respaldar.

En Cassandra no existe `pg_dump`: no hay un comando que te devuelva la base
entera. Hay que decirle al programa que tablas existen, cual es su clave
primaria y por que columna mirar el `WRITETIME` para saber si una fila cambio.

Esa declaracion es, en si misma, la primera leccion del respaldo lógico: el
esquema no lo descubre la herramienta, lo tienes que saber tú.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tabla:
    nombre: str
    clave: list[str]
    #: Columna normal cuyo WRITETIME representa "cuando se escribio esta fila".
    #: Las columnas de la clave primaria NO tienen WRITETIME en Cassandra.
    testigo: str | None = None
    #: Nombre de la columna counter, si la tabla es de tipo counter.
    contador: str | None = None
    #: Columnas que no se pueden volver a insertar tal cual.
    columnas: list[str] = field(default_factory=list)

    @property
    def es_contador(self) -> bool:
        return self.contador is not None


TABLAS: list[Tabla] = [
    Tabla(
        nombre="links_by_code",
        clave=["code"],
        testigo="target_url",
        columnas=["code", "target_url", "owner", "created_at"],
    ),
    Tabla(
        nombre="links_by_owner",
        clave=["owner", "created_at", "code"],
        testigo="target_url",
        columnas=["owner", "created_at", "code", "target_url"],
    ),
    Tabla(
        nombre="clicks_by_code",
        clave=["code", "clicked_at", "click_id"],
        testigo="referrer",
        columnas=["code", "clicked_at", "click_id", "referrer", "user_agent"],
    ),
    # Tabla counter: no admite INSERT, no admite WRITETIME y no se puede
    # restaurar de forma idempotente. Es el caso interesante.
    Tabla(
        nombre="click_counts",
        clave=["code"],
        contador="total",
        columnas=["code", "total"],
    ),
]

POR_NOMBRE = {t.nombre: t for t in TABLAS}
