"""Panel web de arca: respaldo y restauracion de Apache Cassandra."""

from __future__ import annotations

import pathlib
import tempfile
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.modelo import TABLAS
from app.respaldo import (
    conectar,
    estado,
    exportar,
    leer_manifiesto,
    provocar_desastre,
    restaurar,
)

settings = get_settings()
app = FastAPI(
    title="arca — respaldo de Apache Cassandra",
    description=(
        "Respaldo logico completo e incremental de una base Cassandra "
        "gestionada en Astra DB, con restauracion y simulacion de desastre."
    ),
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
plantillas = Jinja2Templates(directory="app/templates")

CARPETA = pathlib.Path("respaldos")
CARPETA.mkdir(exist_ok=True)

_sesion = None
_error = ""


def sesion():
    """Conexion perezosa: el panel debe cargar aunque la base no responda."""
    global _sesion, _error
    if _sesion is None and settings.configured:
        try:
            _sesion = conectar(settings)
            _error = ""
        except Exception as exc:  # noqa: BLE001
            _error = str(exc)
    return _sesion


def _locales() -> list[dict]:
    paquetes = []
    for p in sorted(CARPETA.glob("*.tar.gz"), reverse=True):
        try:
            m = leer_manifiesto(p)
        except Exception:  # noqa: BLE001
            continue
        paquetes.append({
            "archivo": p.name,
            "tipo": m.get("tipo"),
            "momento": m.get("momento", "")[:19].replace("T", " "),
            "etiqueta": m.get("etiqueta", ""),
            "bytes": p.stat().st_size,
            "filas": sum(t["guardadas"] for t in m.get("tablas", [])),
            "hasta": m.get("hasta_writetime"),
        })
    return paquetes


def _releases() -> list[dict]:
    """Catalogo durable: los respaldos que subio el cron a GitHub Releases."""
    url = f"https://api.github.com/repos/{settings.gh_repo}/releases"
    try:
        r = httpx.get(url, timeout=12, headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return []
        salida = []
        for rel in r.json()[:12]:
            for asset in rel.get("assets", []):
                if asset["name"].endswith(".tar.gz"):
                    salida.append({
                        "nombre": asset["name"],
                        "url": asset["browser_download_url"],
                        "bytes": asset["size"],
                        "momento": rel.get("published_at", "")[:19].replace("T", " "),
                        "etiqueta": rel.get("tag_name", ""),
                    })
        return salida
    except Exception:  # noqa: BLE001
        return []


@app.get("/health")
def health() -> JSONResponse:
    s = sesion()
    return JSONResponse({
        "status": "ok",
        "backend": "astra" if s else "sin conexion",
        "keyspace": settings.keyspace,
        "error": _error,
    })


@app.get("/", response_class=HTMLResponse)
def panel(request: Request):
    s = sesion()
    return plantillas.TemplateResponse(
        request=request,
        name="panel.html",
        context={
            "tablas": estado(s) if s else [],
            "conectado": bool(s),
            "error": _error,
            "locales": _locales(),
            "releases": _releases(),
            "nombres": [t.nombre for t in TABLAS],
            "repo": settings.gh_repo,
            "aviso": request.query_params.get("aviso", ""),
        },
    )


@app.post("/respaldo")
def crear_respaldo(tipo: str = Form("completo"), desde: str = Form("")):
    s = sesion()
    if not s:
        return RedirectResponse("/?aviso=sin-conexion", status_code=303)

    marca = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    desde_us = int(desde) if tipo == "incremental" and desde.strip().isdigit() else None
    destino = CARPETA / f"{tipo}-{marca}.tar.gz"
    exportar(s, destino, desde_us=desde_us, etiqueta="desde el panel")
    return RedirectResponse(f"/?aviso=respaldo-{tipo}", status_code=303)


@app.post("/desastre")
def desastre(tabla: str = Form(...), cuantas: int = Form(5)):
    s = sesion()
    if not s:
        return RedirectResponse("/?aviso=sin-conexion", status_code=303)
    d = provocar_desastre(s, tabla, max(1, min(cuantas, 50)))
    return RedirectResponse(f"/?aviso=borradas-{d['borradas']}", status_code=303)


@app.post("/restaurar")
def restaurar_local(archivo: str = Form(...), espejo: str = Form("")):
    s = sesion()
    if not s:
        return RedirectResponse("/?aviso=sin-conexion", status_code=303)
    ruta = CARPETA / archivo
    if not ruta.exists():
        return RedirectResponse("/?aviso=no-existe", status_code=303)
    restaurar(s, ruta, espejo=bool(espejo))
    return RedirectResponse("/?aviso=restaurado", status_code=303)


@app.post("/restaurar-release")
def restaurar_release(url: str = Form(...)):
    """Descarga un respaldo de GitHub Releases y lo restaura."""
    s = sesion()
    if not s:
        return RedirectResponse("/?aviso=sin-conexion", status_code=303)
    if not url.startswith(f"https://github.com/{settings.gh_repo}/"):
        return RedirectResponse("/?aviso=url-no-permitida", status_code=303)

    with httpx.Client(follow_redirects=True, timeout=60) as cliente:
        r = cliente.get(url)
        r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        f.write(r.content)
        temporal = pathlib.Path(f.name)

    restaurar(s, temporal, espejo=False)
    temporal.unlink(missing_ok=True)
    return RedirectResponse("/?aviso=restaurado-desde-release", status_code=303)


@app.get("/descargar/{archivo}")
def descargar(archivo: str):
    ruta = (CARPETA / archivo).resolve()
    if not ruta.is_file() or CARPETA.resolve() not in ruta.parents:
        return JSONResponse({"detail": "no encontrado"}, status_code=404)
    return FileResponse(ruta, filename=archivo, media_type="application/gzip")


@app.get("/api/estado")
def api_estado() -> JSONResponse:
    s = sesion()
    return JSONResponse({
        "conectado": bool(s),
        "tablas": estado(s) if s else [],
        "respaldos_locales": len(_locales()),
        "respaldos_en_releases": len(_releases()),
    })
