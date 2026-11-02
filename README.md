# arca — respaldo lógico de Apache Cassandra

Herramienta de respaldo y restauración para una base **Apache Cassandra**
gestionada en **DataStax Astra DB**, con respaldo completo e incremental,
simulación de desastre y restauración. El respaldo lo ejecuta un **cron de
GitHub Actions** y se publica como asset de una *release*.

> Actividad Grupal 3 — Base de Datos II
> Universidad Privada de Tacna · Ing. Patrick Jose Cuadros Quiroga
> Sebastián Alejandro Cortez Apaza

| | |
|---|---|
| **App en vivo** | https://arca-yw30.onrender.com |
| **Base respaldada** | Astra DB, keyspace `shortener` (la de [astra-shortener](https://github.com/SEBACOR-13/astra-shortener)) |
| **Catálogo de respaldos** | [Releases de este repositorio](../../releases) |
| **Automatización** | GitHub Actions: cron diario + despliegue en Render |

---

## El problema

Astra DB es Cassandra gestionado: **no da acceso a los nodos**. Eso descarta
el respaldo nativo, que en Cassandra consiste en `nodetool snapshot` sobre
cada nodo y copiar SSTables. La única vía es un **respaldo lógico**: leer por
el driver y escribir a disco.

Y eso trae tres consecuencias que no existen en un motor relacional.

### 1. No hay foto de un instante

Cassandra no tiene transacciones multi-partición. Mientras el respaldo
recorre las tablas, la base sigue aceptando escrituras. Lo que acabas
guardando es *todo lo que existía en algún momento del barrido*, no el estado
de un instante.

El manifiesto lo dice en vez de disimularlo:

```json
"aviso": "Respaldo logico sin snapshot consistente: la base acepto
          escrituras durante el barrido."
```

### 2. El incremental lee igual, solo escribe menos

Cassandra guarda cuándo se escribió cada columna, y se puede consultar:

```sql
SELECT code, target_url, WRITETIME(target_url) FROM links_by_code
```

Pero **no deja filtrar por él en el `WHERE`**. Así que un respaldo incremental
tiene que leer la tabla entera y descartar en el cliente. Medido sobre la base
real:

```
RESPALDO COMPLETO    links_by_code   guardadas=4    leidas=4
                     clicks_by_code  guardadas=14   leidas=14

INCREMENTAL después  links_by_code   guardadas=0    leidas=4
                     clicks_by_code  guardadas=0    leidas=14
```

Catorce filas leídas para guardar cero. **Ahorra almacenamiento, no lectura.**

### 3. Los `counter` no se pueden restaurar

Una tabla con columnas `counter` no admite `INSERT`: solo incrementos. No
existe «escribir el valor 42»; existe «suma 7 a lo que haya».

Restaurar un counter obliga a leer el valor actual y aplicar la diferencia:

```python
actual = leer(clave)
delta = objetivo - actual
if delta:
    sesion.execute("UPDATE click_counts SET total = total + ? WHERE code = ?",
                   (delta, clave))
```

Eso **no es idempotente** y tiene una condición de carrera: si alguien
incrementa entre la lectura y la escritura, el resultado queda mal. Es una
limitación del motor, no del programa, y la herramienta lo informa:

```
{'tabla': 'click_counts', 'ajustadas': 0, 'ya_iguales': 4,
 'nota': 'los counter se restauran aplicando una diferencia, no un valor'}
```

Además, `WRITETIME` no está permitido sobre columnas counter, así que **el
incremental no puede filtrarlas**: se exportan enteras siempre.

### 4. Lo borrado reaparece

Un export lógico no guarda *tombstones*. Restaurar sin más resucita las filas
eliminadas. Por eso existe el **modo espejo**, que además borra lo que está en
la base y no en el respaldo. Es destructivo y por eso no es el modo por
defecto.

---

## Qué hace la herramienta

| Acción | Qué ocurre |
|---|---|
| Respaldo completo | Exporta las 4 tablas a JSONL + manifiesto con `sha256` por archivo |
| Respaldo incremental | Igual, pero descarta las filas con `WRITETIME` anterior al corte |
| Provocar desastre | Borra filas de verdad, para poder enseñar la restauración |
| Restaurar | Reinserta las filas; los counter se ajustan por diferencia |
| Restaurar en espejo | Además elimina lo que el respaldo no contiene |

El paquete es un `.tar.gz` con un `.jsonl` por tabla y un `manifiesto.json`.

## La automatización

[`.github/workflows/respaldo.yml`](.github/workflows/respaldo.yml) **es** la
estrategia de respaldo:

```
cron 06:00 UTC
   ├─ reconstruye el Secure Connect Bundle desde un secret en base64
   ├─ ejecuta scripts/respaldar.py
   └─ publica el .tar.gz como asset de una release
```

Nadie abre un panel ni ejecuta nada a mano. El catálogo durable son las
*releases*, y el panel web las lee desde la API pública de GitHub.

[`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml) despliega: lint →
pruebas → `docker build` → deploy hook de Render → verificación de `/health`.
Render tiene `autoDeploy: false`, así que el único camino a producción es el
pipeline. Y la verificación no se conforma con un `200`: exige que `/health`
responda `"backend":"astra"`.

## Ejecutar en local

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements-dev.txt

cp .env.example .env     # token de Astra y ruta del Secure Connect Bundle
uvicorn app.main:app --reload
```

Respaldo desatendido desde la línea de comandos:

```bash
python scripts/respaldar.py
python scripts/respaldar.py --incremental-desde 1792914280398452
```

Las 10 pruebas corren **sin credenciales**: una sesión falsa imita lo justo
del driver, incluido el comportamiento de los counter.

```bash
pytest -v
ruff check app tests scripts
```

## Variables de entorno

| Variable | Dónde | Descripción |
|---|---|---|
| `ASTRA_DB_TOKEN` | local, Render, Actions | Token `AstraCS:...` |
| `ASTRA_SCB_PATH` | local | Ruta al Secure Connect Bundle `.zip` |
| `ASTRA_SCB_B64` | Render, Actions | El mismo bundle en base64 |
| `ASTRA_KEYSPACE` | todas | Por defecto `shortener` |
| `GH_REPO` | todas | Repositorio cuyas releases forman el catálogo |

## Estructura

```
app/
  modelo.py     qué tablas hay, su clave y por qué columna mirar el WRITETIME
  respaldo.py   exportar, restaurar, espejo, desastre
  main.py       panel web
scripts/
  respaldar.py  respaldo desatendido, el que ejecuta el cron
tests/          10 pruebas con sesión falsa, sin red
.github/workflows/
  ci-cd.yml     lint, pruebas, docker build, deploy
  respaldo.yml  cron diario -> release
```

## Licencia

MIT.
