"""Actualizaciones automáticas desde GitHub Releases.

1. Al abrir EndoView revisa si hay una versión publicada más nueva.
2. Si el médico pulsa «Actualizar», descarga el instalador, comprueba su huella SHA-256
   (publicada junto al instalador) y lo ejecuta en modo silencioso.
3. El instalador reemplaza el programa y vuelve a abrir EndoView. Los estudios, usuarios y
   la conexión con Drive viven en otra carpeta y no se tocan.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from version import REPO, VERSION

API = f"https://api.github.com/repos/{REPO}/releases/latest"
CONGELADO = getattr(sys, "frozen", False)
_lock = threading.Lock()
_est = {"actual": VERSION, "nueva": None, "notas": "", "estado": "sin_revisar",
        "progreso": 0, "error": None, "puede_instalar": CONGELADO and os.name == "nt"}
_ultimo = [0.0]
_info = {}


def _ver(t):
    return tuple(int(x) for x in re.findall(r"\d+", t)[:3])


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": f"EndoView/{VERSION}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def estado():
    with _lock:
        return dict(_est)


def buscar(forzar=False):
    """Consulta GitHub (como máximo una vez cada 6 horas, salvo que se fuerce)."""
    if not forzar and time.time() - _ultimo[0] < 6 * 3600:
        return estado()
    _ultimo[0] = time.time()
    with _lock:
        if _est["estado"] in ("descargando", "instalando"):
            return dict(_est)
        _est.update(estado="buscando", error=None)
    try:
        with _get(API) as r:
            rel = json.load(r)
        tag = rel.get("tag_name", "")
        setup = next((a for a in rel.get("assets", [])
                      if a["name"].lower().endswith(".exe") and "setup" in a["name"].lower()), None)
        sumas = next((a for a in rel.get("assets", []) if a["name"].upper() == "SHA256SUMS.TXT"), None)
        if tag and setup and _ver(tag) > _ver(VERSION):
            _info.update(setup=setup, sumas=sumas)
            with _lock:
                _est.update(nueva=tag.lstrip("v"), notas=(rel.get("body") or "")[:1500],
                            estado="disponible")
        else:
            with _lock:
                _est.update(nueva=None, estado="al_dia")
    except Exception as ex:
        with _lock:
            _est.update(estado="sin_conexion", error=str(ex)[:200])
    return estado()


def _sha256_esperado():
    s = _info.get("sumas")
    if not s:
        return None
    with _get(s["browser_download_url"]) as r:
        texto = r.read().decode("utf-8", "replace")
    nombre = _info["setup"]["name"]
    for linea in texto.splitlines():
        partes = linea.strip().split()
        if len(partes) >= 2 and partes[-1].lstrip("*") == nombre:
            return partes[0].lower()
    return None


def _descargar_e_instalar(antes_de_salir):
    try:
        setup = _info["setup"]
        esperado = _sha256_esperado()
        if not esperado:
            raise RuntimeError("La versión publicada no trae su huella de seguridad.")
        destino = os.path.join(tempfile.gettempdir(), setup["name"])
        h = hashlib.sha256()
        with _get(setup["browser_download_url"], timeout=60) as r, open(destino, "wb") as f:
            total = int(r.headers.get("Content-Length") or setup.get("size") or 0)
            leido = 0
            while True:
                bloque = r.read(1024 * 256)
                if not bloque:
                    break
                f.write(bloque)
                h.update(bloque)
                leido += len(bloque)
                if total:
                    with _lock:
                        _est["progreso"] = int(leido * 100 / total)
        if h.hexdigest().lower() != esperado:
            os.remove(destino)
            raise RuntimeError("El archivo descargado no coincide con la versión publicada.")
        with _lock:
            _est.update(estado="instalando", progreso=100)
        if not _est["puede_instalar"]:
            with _lock:
                _est.update(estado="descargado")
            return
        # Instalación silenciosa; el instalador vuelve a abrir EndoView al terminar.
        subprocess.Popen([destino, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                          "/CLOSEAPPLICATIONS", "/actualizar=1"],
                         creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_PROCESS_GROUP
                         close_fds=True)
        time.sleep(1)
        if antes_de_salir:
            antes_de_salir()
        os._exit(0)
    except Exception as ex:
        with _lock:
            _est.update(estado="error", error=str(ex)[:300])


def instalar(antes_de_salir=None):
    with _lock:
        if _est["estado"] not in ("disponible", "error") or not _info.get("setup"):
            return dict(_est)
        _est.update(estado="descargando", progreso=0, error=None)
    threading.Thread(target=_descargar_e_instalar, args=(antes_de_salir,), daemon=True).start()
    return estado()
