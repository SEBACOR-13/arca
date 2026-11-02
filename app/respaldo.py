"""Motor de respaldo y restauracion para Apache Cassandra.

Por que este archivo existe
---------------------------
Astra DB es Cassandra gestionado: no da acceso a los nodos, asi que no hay
`nodetool snapshot` ni copia de SSTables. La unica via es un **respaldo
logico**: leer por el driver y escribir a disco.

Eso trae tres consecuencias que no existen en un motor relacional y que son
el contenido del articulo:

1. **No hay snapshot consistente.** Cassandra no tiene transacciones
   multi-particion. Mientras exportas, la base sigue aceptando escrituras, y
   el resultado es "todo lo que existia en algun momento del barrido", no una
   foto de un instante.

2. **El incremental lee igual, solo escribe menos.** Se puede saber cuando
   se escribio una fila con `WRITETIME`, pero Cassandra **no deja filtrar por
   WRITETIME en el WHERE**. Hay que leer todo y descartar en el cliente.

3. **Los counter no se pueden restaurar.** No admiten INSERT, solo
   incrementos. Restaurar un counter exige leer el valor actual y aplicar la
   diferencia, lo que no es idempotente ni seguro con escrituras en curso.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.modelo import TABLAS, Tabla

VERSION_FORMATO = 1


# ----------------------------------------------------------------- conexion

def _ruta_bundle(s: Settings) -> str:
    if s.scb_path and os.path.exists(s.scb_path):
        return s.scb_path
    if s.scb_b64:
        crudo = base64.b64decode(s.scb_b64, validate=True)
        h = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        h.write(crudo)
        h.close()
        return h.name
    raise FileNotFoundError("falta ASTRA_SCB_PATH o ASTRA_SCB_B64")


def conectar(s: Settings):

    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster

    cluster = Cluster(
        cloud={"secure_connect_bundle": _ruta_bundle(s)},
        auth_provider=PlainTextAuthProvider("token", s.token),
    )
    sesion = cluster.connect(s.keyspace)
    sesion.default_timeout = 60
    return sesion


# ------------------------------------------------------------ serializacion

def _a_json(valor: Any) -> Any:
    """Convierte los tipos de Cassandra a algo que JSON entienda."""
    if isinstance(valor, datetime):
        return {"__tipo__": "timestamp", "v": valor.isoformat()}
    if isinstance(valor, uuid.UUID):
        return {"__tipo__": "uuid", "v": str(valor)}
    return valor


def _de_json(valor: Any) -> Any:
    if isinstance(valor, dict) and "__tipo__" in valor:
        if valor["__tipo__"] == "timestamp":
            return datetime.fromisoformat(valor["v"])
        if valor["__tipo__"] == "uuid":
            return uuid.UUID(valor["v"])
    return valor


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for trozo in iter(lambda: f.read(65536), b""):
            h.update(trozo)
    return h.hexdigest()


# -------------------------------------------------------------- exportacion

def _leer_tabla(
    sesion, tabla: Tabla, desde_us: int | None
) -> tuple[list[dict], int, int]:
    """Devuelve (filas, leidas, max_writetime).

    `desde_us` es un WRITETIME en microsegundos. Si viene, solo se guardan
    las filas escritas despues, pero **se leen todas igual**: Cassandra no
    permite WHERE sobre WRITETIME.
    """
    columnas = ", ".join(tabla.columnas)
    if tabla.testigo:
        consulta = (
            f"SELECT {columnas}, WRITETIME({tabla.testigo}) AS escrito_en "
            f"FROM {tabla.nombre}"
        )
    else:
        # Las tablas counter no admiten WRITETIME: se exportan siempre enteras.
        consulta = f"SELECT {columnas} FROM {tabla.nombre}"

    filas, leidas, maximo = [], 0, 0
    for fila in sesion.execute(consulta):
        leidas += 1
        d = dict(fila._asdict())
        wt = d.pop("escrito_en", None)
        if wt:
            maximo = max(maximo, int(wt))
            if desde_us is not None and int(wt) <= desde_us:
                continue
        filas.append({k: _a_json(v) for k, v in d.items()})
    return filas, leidas, maximo


def exportar(sesion, destino: Path, desde_us: int | None = None,
             etiqueta: str = "") -> dict:
    """Crea un paquete .tar.gz con un .jsonl por tabla y un manifiesto."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    momento = datetime.now(UTC)
    tipo = "incremental" if desde_us else "completo"

    with tempfile.TemporaryDirectory() as tmp:
        carpeta = Path(tmp)
        detalle, corte = [], desde_us or 0

        for tabla in TABLAS:
            filas, leidas, maximo = _leer_tabla(sesion, tabla, desde_us)
            archivo = carpeta / f"{tabla.nombre}.jsonl"
            with archivo.open("w", encoding="utf-8") as f:
                for fila in filas:
                    f.write(json.dumps(fila, ensure_ascii=False) + "\n")

            corte = max(corte, maximo)
            detalle.append({
                "tabla": tabla.nombre,
                "guardadas": len(filas),
                "leidas": leidas,
                "es_contador": tabla.es_contador,
                "sha256": _sha256(archivo),
                "bytes": archivo.stat().st_size,
            })

        manifiesto = {
            "formato": VERSION_FORMATO,
            "tipo": tipo,
            "etiqueta": etiqueta,
            "momento": momento.isoformat(),
            "desde_writetime": desde_us,
            "hasta_writetime": corte,
            "tablas": detalle,
            "aviso": (
                "Respaldo logico sin snapshot consistente: la base acepto "
                "escrituras durante el barrido."
            ),
        }
        (carpeta / "manifiesto.json").write_text(
            json.dumps(manifiesto, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        with tarfile.open(destino, "w:gz") as tar:
            for archivo in sorted(carpeta.iterdir()):
                tar.add(archivo, arcname=archivo.name)

    manifiesto["archivo"] = destino.name
    manifiesto["bytes_paquete"] = destino.stat().st_size
    return manifiesto


def leer_manifiesto(paquete: Path) -> dict:
    with tarfile.open(paquete, "r:gz") as tar:
        f = tar.extractfile("manifiesto.json")
        return json.loads(f.read().decode("utf-8"))


# ------------------------------------------------------------ restauracion

def _restaurar_normal(sesion, tabla: Tabla, filas: list[dict]) -> int:
    if not filas:
        return 0
    cols = tabla.columnas
    marcas = ", ".join(["?"] * len(cols))
    prep = sesion.prepare(
        f"INSERT INTO {tabla.nombre} ({', '.join(cols)}) VALUES ({marcas})"
    )
    for fila in filas:
        valores = tuple(_de_json(fila.get(c)) for c in cols)
        sesion.execute(prep, valores)
    return len(filas)


def _restaurar_contador(sesion, tabla: Tabla, filas: list[dict]) -> dict:
    """Restaurar un counter NO es idempotente.

    No se puede escribir el valor: solo sumar. Asi que hay que leer el actual
    y aplicar la diferencia. Si alguien incrementa entre la lectura y la
    escritura, el resultado queda mal. Es una limitacion del motor, no del
    programa, y por eso se informa en vez de disimularla.
    """
    clave = tabla.clave[0]
    contador = tabla.contador
    leer = sesion.prepare(f"SELECT {contador} FROM {tabla.nombre} WHERE {clave} = ?")
    sumar = sesion.prepare(
        f"UPDATE {tabla.nombre} SET {contador} = {contador} + ? WHERE {clave} = ?"
    )

    ajustadas, ya_iguales = 0, 0
    for fila in filas:
        k = _de_json(fila[clave])
        objetivo = int(fila[contador] or 0)
        actual_fila = sesion.execute(leer, (k,)).one()
        actual = int(getattr(actual_fila, contador) or 0) if actual_fila else 0
        delta = objetivo - actual
        if delta == 0:
            ya_iguales += 1
            continue
        sesion.execute(sumar, (delta, k))
        ajustadas += 1

    return {
        "ajustadas": ajustadas,
        "ya_iguales": ya_iguales,
        "nota": "los counter se restauran aplicando una diferencia, no un valor",
    }


def restaurar(sesion, paquete: Path, espejo: bool = False) -> dict:
    """Vuelca un paquete sobre la base.

    `espejo=True` ademas borra lo que esta en la base y no en el respaldo.
    Sin eso, restaurar nunca elimina nada: un respaldo logico no guarda
    tombstones, asi que lo borrado reaparece.
    """
    resumen = {"tablas": [], "espejo": espejo}

    with tarfile.open(paquete, "r:gz") as tar:
        manifiesto = json.loads(tar.extractfile("manifiesto.json").read().decode("utf-8"))

        for tabla in TABLAS:
            miembro = f"{tabla.nombre}.jsonl"
            if miembro not in tar.getnames():
                continue
            crudo = tar.extractfile(miembro).read().decode("utf-8")
            filas = [json.loads(linea) for linea in crudo.splitlines() if linea.strip()]

            if tabla.es_contador:
                detalle = _restaurar_contador(sesion, tabla, filas)
                detalle["tabla"] = tabla.nombre
            else:
                escritas = _restaurar_normal(sesion, tabla, filas)
                detalle = {"tabla": tabla.nombre, "escritas": escritas}

                if espejo:
                    detalle["borradas"] = _borrar_sobrantes(sesion, tabla, filas)

            resumen["tablas"].append(detalle)

    resumen["manifiesto"] = manifiesto
    return resumen


def _borrar_sobrantes(sesion, tabla: Tabla, filas: list[dict]) -> int:
    """Elimina de la base las claves que el respaldo no contiene."""
    def clave_de(d: dict) -> tuple:
        return tuple(str(_de_json(d.get(c))) for c in tabla.clave)

    en_respaldo = {clave_de(f) for f in filas}
    sobrantes = []
    for fila in sesion.execute(f"SELECT {', '.join(tabla.clave)} FROM {tabla.nombre}"):
        d = dict(fila._asdict())
        if clave_de(d) not in en_respaldo:
            sobrantes.append(tuple(d[c] for c in tabla.clave))

    if not sobrantes:
        return 0

    condicion = " AND ".join(f"{c} = ?" for c in tabla.clave)
    prep = sesion.prepare(f"DELETE FROM {tabla.nombre} WHERE {condicion}")
    for valores in sobrantes:
        sesion.execute(prep, valores)
    return len(sobrantes)


# ------------------------------------------------------------------ estado

def estado(sesion) -> list[dict]:
    """Conteo por tabla. En Cassandra COUNT(*) recorre todo: solo vale para
    una base de demostracion como esta."""
    filas = []
    for tabla in TABLAS:
        n = sesion.execute(f"SELECT COUNT(*) AS n FROM {tabla.nombre}").one().n
        filas.append({
            "tabla": tabla.nombre,
            "filas": int(n),
            "es_contador": tabla.es_contador,
        })
    return filas


def provocar_desastre(sesion, nombre_tabla: str, cuantas: int) -> dict:
    """Borra filas de verdad, para poder enseñar la restauracion."""
    from app.modelo import POR_NOMBRE

    tabla = POR_NOMBRE[nombre_tabla]
    claves = []
    for fila in sesion.execute(f"SELECT {', '.join(tabla.clave)} FROM {tabla.nombre}"):
        claves.append(tuple(dict(fila._asdict())[c] for c in tabla.clave))
        if len(claves) >= cuantas:
            break

    if not claves:
        return {"tabla": nombre_tabla, "borradas": 0}

    condicion = " AND ".join(f"{c} = ?" for c in tabla.clave)
    prep = sesion.prepare(f"DELETE FROM {tabla.nombre} WHERE {condicion}")
    for valores in claves:
        sesion.execute(prep, valores)
    return {"tabla": nombre_tabla, "borradas": len(claves)}
