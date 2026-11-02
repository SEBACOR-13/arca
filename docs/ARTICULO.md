---
title: "Respaldar Cassandra cuando no tienes acceso a los nodos"
published: true
description: "Astra DB no te deja hacer nodetool snapshot. Construí la alternativa, la medí contra una base real y encontré tres límites que ningún tutorial menciona: el incremental que no ahorra lectura, los counter que no se pueden restaurar y lo borrado que resucita."
tags: cassandra, nosql, devops, backup
cover_image: ""
---

La documentación de Apache Cassandra es clara sobre cómo se respalda: entras
a cada nodo, ejecutas `nodetool snapshot`, y copias los SSTables que quedan
en `data/<keyspace>/<tabla>/snapshots/`. Es rápido, es consistente a nivel de
nodo y es la respuesta correcta.

También es la respuesta que no puedes usar si tu Cassandra está gestionado.

En DataStax Astra DB no hay nodos a los que entrar. No hay `nodetool`, no hay
sistema de archivos, no hay SSH. Tienes un endpoint CQL y un token. Y en el
plan gratuito tampoco tienes la función de respaldo del propio Astra. Así que
la pregunta deja de ser «¿cómo ejecuto el respaldo nativo?» y pasa a ser
**«¿qué puedo garantizar sin él?»**.

Para responderla construí una herramienta de respaldo lógico, la ejecuté
contra la base de datos real de un proyecto anterior y medí qué pasaba. Lo
que sigue son los números y las tres cosas que me sorprendieron.

- **Repositorio:** https://github.com/SEBACOR-13/arca
- **Panel en vivo:** https://arca-yw30.onrender.com
- **Base respaldada:** el keyspace de https://github.com/SEBACOR-13/astra-shortener

---

## La única vía: respaldo lógico

Si no puedes copiar los archivos, solo queda leer por el driver y escribir a
disco. Eso es un **respaldo lógico**: un `SELECT` por tabla, una línea JSON
por fila, un manifiesto con el `sha256` de cada archivo, todo empaquetado en
un `.tar.gz`.

```
respaldo-20251101-074412.tar.gz
├── manifiesto.json
├── links_by_code.jsonl
├── links_by_owner.jsonl
├── clicks_by_code.jsonl
└── click_counts.jsonl
```

Suena trivial. Lo interesante empieza cuando lo mides.

## Límite 1: no existe la foto de un instante

Un respaldo nativo de Cassandra fija el estado de un nodo en un momento. Un
respaldo lógico no puede: la base sigue aceptando escrituras mientras tu
`SELECT` recorre las particiones. Y Cassandra no tiene transacciones
multi-partición con las que puedas congelar nada.

Lo que acabas guardando no es «el estado de las 07:44». Es *todo lo que
existió en algún momento del barrido*. Si una fila se escribe después de que
pasaras por su partición, no está. Si se borra, puede que sí esté.

No hay arreglo a nivel de aplicación. Lo único honesto es escribirlo en el
manifiesto en vez de disimularlo:

```json
"aviso": "Respaldo logico sin snapshot consistente: la base acepto
          escrituras durante el barrido."
```

Un respaldo que miente sobre sus garantías es peor que no tener respaldo,
porque te hace tomar decisiones con una confianza que no corresponde.

## Límite 2: el incremental ahorra almacenamiento, no lectura

Aquí esperaba una victoria fácil y me llevé el golpe más instructivo.

Cassandra guarda, por cada columna, el momento exacto en que se escribió.
Se consulta con `WRITETIME` y es un microsegundo UNIX:

```sql
SELECT code, target_url, WRITETIME(target_url) AS escrito_en
FROM links_by_code
```

La idea del incremental es obvia: «dame solo lo escrito después del último
respaldo». El problema es que **`WRITETIME` no se puede usar en el `WHERE`**.
No es una limitación del driver ni una opción que falte activar: no es una
columna, es metadato de la celda, y no está indexado.

Así que el filtro tiene que ocurrir en el cliente. La consulta lee la tabla
entera, y el programa descarta. Medido contra la base real, un respaldo
completo seguido de un incremental inmediato:

```
RESPALDO COMPLETO     links_by_code    guardadas=4     leidas=4
                      links_by_owner   guardadas=4     leidas=4
                      clicks_by_code   guardadas=14    leidas=14

INCREMENTAL después   links_by_code    guardadas=0     leidas=4
                      links_by_owner   guardadas=0     leidas=4
                      clicks_by_code   guardadas=0     leidas=14
```

Veintidós filas leídas para guardar cero. El paquete resultante ocupa unos
pocos cientos de bytes en vez de decenas de kilobytes, y eso es una ganancia
real si lo que te preocupa es el almacenamiento de los respaldos. Pero el
coste en la base de datos —y en Astra, el consumo de operaciones de lectura
que te facturan— es **idéntico al de un respaldo completo**.

Es exactamente el tipo de detalle que decide una estrategia. Si tu motivo
para hacer incrementales es «no quiero castigar la base cada noche», en
Cassandra gestionado el incremental no te sirve para eso.

## Límite 3: los `counter` no se restauran, se ajustan

Mi base tenía una tabla con una columna `counter`:

```sql
CREATE TABLE click_counts (
    code text PRIMARY KEY,
    total counter
);
```

Las tablas con counter son una categoría aparte en Cassandra. **No admiten
`INSERT`.** La única escritura posible es un incremento:

```sql
UPDATE click_counts SET total = total + 7 WHERE code = 'abc1234';
```

No existe «escribe el valor 42». Existe «suma 7 a lo que haya». Eso convierte
la restauración en otra cosa: hay que leer el valor actual y aplicar la
diferencia hasta el valor objetivo.

```python
actual = leer(clave)
delta = objetivo - actual
if delta:
    sesion.execute(
        "UPDATE click_counts SET total = total + ? WHERE code = ?",
        (delta, clave),
    )
```

Esto tiene dos consecuencias que conviene decir en voz alta:

1. **No es idempotente de verdad.** Lo es solo si nadie toca el counter entre
   la lectura y la escritura. Si entre medias llega un clic, el valor queda
   desplazado. Es una condición de carrera inherente, no un bug del programa.
2. **El incremental no puede filtrarlos.** `WRITETIME` no está permitido sobre
   columnas counter, así que no hay forma de saber cuáles cambiaron. Se
   exportan enteras en cada respaldo, siempre.

La herramienta lo informa en vez de aparentar éxito:

```
{'tabla': 'click_counts', 'ajustadas': 0, 'ya_iguales': 4,
 'nota': 'los counter se restauran aplicando una diferencia, no un valor'}
```

`ya_iguales: 4` es el caso bueno: los cuatro contadores ya tenían el valor
del respaldo, así que no se escribió nada. Si hubiera dicho `ajustadas: 4`,
significaría que la restauración movió contadores en producción, que es una
operación mucho menos inocente de lo que parece.

## Límite 4: lo borrado resucita

Cassandra no borra: escribe un **tombstone**, un marcador que dice «esto ya
no está», y espera a la compactación para liberar el espacio.

Un `SELECT` nunca devuelve tombstones. Por tanto un respaldo lógico **no los
guarda**. Y si restauras un respaldo tomado antes de un borrado, las filas
eliminadas vuelven a la vida.

Esto no es teoría incómoda, es el modo de fallo más probable en la práctica:
alguien borra datos por error, tú restauras para recuperar otra cosa, y de
paso resucitas lo que se había borrado a propósito hace tres semanas.

Por eso la herramienta tiene dos modos de restauración:

| Modo | Qué hace |
|---|---|
| Normal | Reinserta lo que hay en el respaldo. Lo demás se queda. |
| Espejo | Reinserta, **y además borra** lo que está en la base y no en el respaldo. |

El modo espejo deja la base exactamente como el respaldo. También es
destructivo, y por eso no es el modo por defecto. La decisión de cuál usar
no es técnica: depende de si lo que quieres es *recuperar datos perdidos* o
*volver a un estado conocido*. No son lo mismo.

## La prueba: romper la base a propósito

Un respaldo que nunca se restauró no es un respaldo, es un archivo. Así que
la herramienta tiene un botón para provocar un desastre de verdad —borra
filas reales— y así poder demostrar el ciclo completo:

```
Estado inicial        14 clics, 4 enlaces
Respaldo completo     26 filas en el paquete
Desastre              6 filas borradas  ->  8 clics
Restauración          escritas=26, counters ya_iguales=4
Estado final          14 clics, 4 enlaces
```

El ciclo cierra. Y los contadores no se movieron, porque el borrado de filas
de clics no toca la tabla de counters: ese desfase entre «los datos» y «el
agregado» es, él mismo, otra cosa que un respaldo lógico no puede arreglar
sola.

## La estrategia es el workflow, no el panel

Hay un panel web para ver el estado y lanzar las operaciones a mano, y sirve
para enseñar el mecanismo. Pero **ningún respaldo real depende de que alguien
abra una página**.

La estrategia de respaldo de este proyecto es un archivo YAML:

```yaml
on:
  schedule:
    - cron: "0 6 * * *"   # 06:00 UTC = 01:00 en Perú
```

Cada noche, GitHub Actions pide el Secure Connect Bundle a la API de Astra,
ejecuta el respaldo, y publica el `.tar.gz` **como asset de una release**.

Ese primer paso empezó siendo otra cosa, y merece un párrafo. El driver no
se conecta sin el bundle, que es un `.zip` de certificados, así que lo obvio
es guardarlo como secret en base64. Son **16 416 caracteres** para pegar en
un formulario web. Al ejecutarse, el job dijo esto:

```
base64 recibido: 16417 caracteres
base64: error: invalid input
```

Un carácter de más. El pegado se había corrompido, y el error no apunta a
nada útil. La solución no fue volver a pegarlo con más cuidado: fue **borrar
el secret**. La API de Astra entrega el bundle a quien presente el token que
ya hace falta para conectarse, así que el workflow lo pide fresco en cada
ejecución. Un secret menos y un modo de fallo menos.

Y todavía quedaba otro. Con el bundle ya correcto, la conexión seguía
fallando:

```
AuthenticationFailed: code=0100 [Bad credentials]
message="Provided username token and/or password are incorrect"
```

El token era válido —la llamada a la API acababa de funcionar con él—, pero
esa llamada hacía `.strip()` y el driver no. El valor pegado arrastraba un
salto de línea invisible. Un `\n` al final de una contraseña produce un error
que no se parece en nada a su causa, así que ahora toda variable de entorno
se lee limpia:

```python
def _var(nombre: str, por_defecto: str = "") -> str:
    return os.getenv(nombre, por_defecto).strip()
```

Dos horas de depuración para dos líneas. Las pongo aquí porque ninguna de
las dos aparece en un tutorial, y las dos son consecuencia de lo mismo:
credenciales que viajan por un formulario web antes de llegar al código.

Esa última decisión es la que más me gusta. Las releases de GitHub son
durables, versionadas, descargables por URL y están fuera del proveedor de
base de datos y fuera del proveedor de hosting. Si mañana desaparecen Astra
y Render, los respaldos siguen ahí. El panel web no los guarda: los *lee*,
desde la API pública de GitHub.

El despliegue sigue la misma lógica: `autoDeploy: false` en Render, de modo
que el único camino a producción es el pipeline, y la verificación final no
se conforma con un `200`:

```bash
if echo "$respuesta" | grep -q '"backend":"astra"'; then
  echo "Conectado a Astra DB."
  exit 0
fi
echo "ERROR: responde, pero sin conexion a Astra."
exit 1
```

Esto lo aprendí a golpes. La aplicación tiene un backend en memoria para que
las pruebas corran sin credenciales, y un día desplegó perfectamente... con
ese backend. HTTP 200, página verde, cero conexión a la base de datos. Un
health check que solo mira el código de estado te deja exactamente así:
convencido de que todo funciona.

## Las pruebas corren sin credenciales

Las 10 pruebas usan una sesión falsa que imita lo justo del driver, incluido
el comportamiento de los counter: aceptan sumas, rechazan `INSERT`. Eso
permite comprobar en CI la parte difícil —que la restauración de un counter
aplica una diferencia y no un valor— sin que el pipeline necesite tocar
Astra ni tener un token.

```bash
pytest -v
ruff check app tests scripts
```

## Lo que me llevo

Si tengo que resumir el ejercicio en una frase: **el respaldo no es una
funcionalidad, es un conjunto de garantías, y hay que saber cuáles tienes.**

En Cassandra gestionado, sin acceso a los nodos, las garantías que puedo dar
son estas:

- Tengo una copia verificable por `sha256` de todas las filas visibles.
- **No** tengo consistencia de un instante.
- El incremental me ahorra espacio, **no** carga de lectura.
- Los counter se restauran por diferencia, con una carrera posible.
- Los borrados no están en el respaldo; recuperarlos exige el modo espejo.

Ninguna de esas cinco líneas aparece en un tutorial de «cómo hacer backup de
Cassandra». Todas aparecen en cuanto lo construyes y lo mides. Y escribirlas
en el manifiesto, donde quien restaure las vea, me parece la parte más útil
de todo el proyecto.

---

*Actividad Grupal 3 — Base de Datos II · Universidad Privada de Tacna ·
Ing. Patrick Jose Cuadros Quiroga · Sebastián Alejandro Cortez Apaza*
