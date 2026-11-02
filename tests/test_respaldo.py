"""Pruebas del motor de respaldo.

Corren sin tocar Astra: una sesion falsa imita lo justo del driver para
ejercitar la exportacion, la restauracion y el caso de los counter. Asi el
pipeline valida la logica sin credenciales ni red.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.modelo import POR_NOMBRE, TABLAS
from app.respaldo import (
    _de_json,
    exportar,
    leer_manifiesto,
    provocar_desastre,
    restaurar,
)


class FilaFalsa:
    def __init__(self, datos: dict):
        self._datos = datos
        for k, v in datos.items():
            setattr(self, k, v)

    def _asdict(self):
        return dict(self._datos)


class SesionFalsa:
    """Imita lo minimo del driver: SELECT, INSERT, DELETE y counters."""

    def __init__(self):
        self.datos = {t.nombre: [] for t in TABLAS}
        self.writetimes = {}

    # -- utilidades de la prueba
    def sembrar(self, tabla: str, filas: list[dict], wt: int = 1000):
        for f in filas:
            self.datos[tabla].append(dict(f))
            self.writetimes[(tabla, self._clave(tabla, f))] = wt

    def _clave(self, tabla: str, fila: dict):
        return tuple(str(fila[c]) for c in POR_NOMBRE[tabla].clave)

    # -- API que usa el motor
    def prepare(self, consulta):
        return consulta

    def execute(self, consulta, parametros=None):
        consulta = consulta.strip()
        if consulta.upper().startswith("SELECT"):
            return self._select(consulta)
        if consulta.upper().startswith("INSERT"):
            return self._insert(consulta, parametros)
        if consulta.upper().startswith("DELETE"):
            return self._delete(consulta, parametros)
        if consulta.upper().startswith("UPDATE"):
            return self._update(consulta, parametros)
        raise AssertionError(f"consulta no soportada: {consulta}")

    def _tabla_de(self, consulta):
        for nombre in self.datos:
            if f" {nombre}" in consulta:
                return nombre
        raise AssertionError(consulta)

    def _select(self, consulta):
        tabla = self._tabla_de(consulta)
        if "COUNT(*)" in consulta:
            return Resultado([FilaFalsa({"n": len(self.datos[tabla])})])

        filas = []
        for fila in self.datos[tabla]:
            d = dict(fila)
            if "WRITETIME(" in consulta:
                d["escrito_en"] = self.writetimes[(tabla, self._clave(tabla, fila))]
            filas.append(FilaFalsa(d))
        return Resultado(filas)

    def _insert(self, consulta, parametros):
        tabla = self._tabla_de(consulta)
        cols = consulta.split("(")[1].split(")")[0].split(",")
        cols = [c.strip() for c in cols]
        nueva = dict(zip(cols, parametros, strict=False))
        clave = self._clave(tabla, nueva)
        self.datos[tabla] = [f for f in self.datos[tabla]
                             if self._clave(tabla, f) != clave]
        self.datos[tabla].append(nueva)
        self.writetimes[(tabla, clave)] = 9999
        return Resultado([])

    def _delete(self, consulta, parametros):
        tabla = self._tabla_de(consulta)
        claves = POR_NOMBRE[tabla].clave
        objetivo = tuple(str(v) for v in parametros)
        self.datos[tabla] = [
            f for f in self.datos[tabla]
            if tuple(str(f[c]) for c in claves) != objetivo
        ]
        return Resultado([])

    def _update(self, consulta, parametros):
        tabla = self._tabla_de(consulta)
        contador = POR_NOMBRE[tabla].contador
        delta, clave = parametros
        for fila in self.datos[tabla]:
            if str(fila[POR_NOMBRE[tabla].clave[0]]) == str(clave):
                fila[contador] = int(fila[contador] or 0) + delta
                return Resultado([])
        self.datos[tabla].append({POR_NOMBRE[tabla].clave[0]: clave, contador: delta})
        return Resultado([])


class Resultado(list):
    def one(self):
        return self[0] if self else None


@pytest.fixture
def sesion():
    s = SesionFalsa()
    s.sembrar("links_by_code", [
        {"code": "AAA1111", "target_url": "https://a.com", "owner": "demo",
         "created_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"code": "BBB2222", "target_url": "https://b.com", "owner": "demo",
         "created_at": datetime(2026, 1, 2, tzinfo=UTC)},
    ])
    s.sembrar("links_by_owner", [
        {"owner": "demo", "created_at": datetime(2026, 1, 1, tzinfo=UTC),
         "code": "AAA1111", "target_url": "https://a.com"},
    ])
    s.sembrar("clicks_by_code", [
        {"code": "AAA1111", "clicked_at": datetime(2026, 1, 3, tzinfo=UTC),
         "click_id": uuid.uuid4(), "referrer": "", "user_agent": "pytest"},
    ])
    s.sembrar("click_counts", [{"code": "AAA1111", "total": 7}])
    return s


def test_respaldo_completo_guarda_todas_las_filas(sesion, tmp_path):
    m = exportar(sesion, tmp_path / "c.tar.gz", etiqueta="prueba")
    por_tabla = {t["tabla"]: t for t in m["tablas"]}

    assert m["tipo"] == "completo"
    assert por_tabla["links_by_code"]["guardadas"] == 2
    assert por_tabla["clicks_by_code"]["guardadas"] == 1
    assert all(t["sha256"] for t in m["tablas"])


def test_el_incremental_lee_todo_pero_guarda_solo_lo_nuevo(sesion, tmp_path):
    """La leccion central: ahorra espacio, no lectura."""
    m = exportar(sesion, tmp_path / "i.tar.gz", desde_us=5000)
    por_tabla = {t["tabla"]: t for t in m["tablas"]}

    assert por_tabla["links_by_code"]["leidas"] == 2
    assert por_tabla["links_by_code"]["guardadas"] == 0


def test_los_counter_se_exportan_siempre_enteros(sesion, tmp_path):
    """No tienen WRITETIME, asi que el incremental no puede filtrarlos."""
    m = exportar(sesion, tmp_path / "i.tar.gz", desde_us=5000)
    por_tabla = {t["tabla"]: t for t in m["tablas"]}

    assert por_tabla["click_counts"]["es_contador"] is True
    assert por_tabla["click_counts"]["guardadas"] == 1


def test_restaurar_devuelve_las_filas_borradas(sesion, tmp_path):
    paquete = tmp_path / "c.tar.gz"
    exportar(sesion, paquete)

    provocar_desastre(sesion, "links_by_code", 2)
    assert len(sesion.datos["links_by_code"]) == 0

    restaurar(sesion, paquete)
    assert len(sesion.datos["links_by_code"]) == 2


def test_restaurar_un_counter_aplica_la_diferencia(sesion, tmp_path):
    paquete = tmp_path / "c.tar.gz"
    exportar(sesion, paquete)

    # alguien siguio incrementando despues del respaldo
    sesion.datos["click_counts"][0]["total"] = 10

    r = restaurar(sesion, paquete)
    detalle = next(t for t in r["tablas"] if t["tabla"] == "click_counts")

    assert detalle["ajustadas"] == 1
    assert sesion.datos["click_counts"][0]["total"] == 7


def test_sin_espejo_lo_borrado_reaparece(sesion, tmp_path):
    """Un export logico no guarda tombstones."""
    paquete = tmp_path / "c.tar.gz"
    exportar(sesion, paquete)

    sesion.datos["links_by_code"].append(
        {"code": "CCC3333", "target_url": "https://c.com", "owner": "demo",
         "created_at": datetime(2026, 1, 9, tzinfo=UTC)}
    )

    restaurar(sesion, paquete, espejo=False)
    assert len(sesion.datos["links_by_code"]) == 3

    restaurar(sesion, paquete, espejo=True)
    assert len(sesion.datos["links_by_code"]) == 2


def test_el_manifiesto_avisa_de_la_falta_de_snapshot(sesion, tmp_path):
    paquete = tmp_path / "c.tar.gz"
    exportar(sesion, paquete)
    m = leer_manifiesto(paquete)
    assert "consistente" in m["aviso"]


def test_los_tipos_sobreviven_al_viaje_por_json():
    momento = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    ident = uuid.uuid4()
    assert _de_json({"__tipo__": "timestamp", "v": momento.isoformat()}) == momento
    assert _de_json({"__tipo__": "uuid", "v": str(ident)}) == ident


def test_el_paquete_contiene_un_archivo_por_tabla(sesion, tmp_path):
    import tarfile

    paquete = tmp_path / "c.tar.gz"
    exportar(sesion, paquete)
    with tarfile.open(paquete) as tar:
        nombres = set(tar.getnames())

    assert "manifiesto.json" in nombres
    for tabla in TABLAS:
        assert f"{tabla.nombre}.jsonl" in nombres


def test_el_desastre_borra_solo_lo_pedido(sesion):
    sesion.sembrar("links_by_code", [
        {"code": f"Z{i:06d}", "target_url": "https://z.com", "owner": "demo",
         "created_at": datetime(2026, 1, 5, tzinfo=UTC)} for i in range(5)
    ])
    antes = len(sesion.datos["links_by_code"])

    d = provocar_desastre(sesion, "links_by_code", 3)

    assert d["borradas"] == 3
    assert len(sesion.datos["links_by_code"]) == antes - 3
