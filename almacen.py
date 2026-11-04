"""Almacenamiento local: configuración, médicos (usuarios) y estudios."""
import json
import os
import secrets
import shutil
import sys
import threading
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

CONGELADO = getattr(sys, "frozen", False)  # True cuando corre como .exe
# RECURSOS: plantillas e íconos (dentro del .exe). BASE: datos del usuario (escribible).
RECURSOS = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if os.environ.get("ENDO_DATOS"):
    BASE = os.environ["ENDO_DATOS"]
elif CONGELADO:
    _local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    BASE = os.path.join(_local, "EndoView")
    _viejo = os.path.join(_local, "EndoCaptura")  # nombre anterior del programa
    if not os.path.exists(BASE) and os.path.isdir(_viejo):
        try:
            shutil.move(_viejo, BASE)
        except OSError:
            shutil.copytree(_viejo, BASE, dirs_exist_ok=True)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
DATOS = os.path.join(BASE, "datos")
ESTUDIOS = os.path.join(DATOS, "estudios")
ARCH_CONFIG = os.path.join(BASE, "config.json")
ARCH_MEDICOS = os.path.join(DATOS, "medicos.json")
ARCH_CONTADOR = os.path.join(DATOS, "contador.json")
ARCH_SECRETO = os.path.join(DATOS, ".secreto")

_lock = threading.Lock()

PROCEDIMIENTOS = {
    "EGD": "Panendoscopia (endoscopia alta)",
    "COL": "Colonoscopia",
    "RSC": "Rectosigmoidoscopia",
    "CPRE": "CPRE",
    "USE": "Ultrasonido endoscópico",
    "ENT": "Enteroscopia",
    "BRO": "Broncoscopia",
    "CIS": "Cistoscopia",
    "REN": "Renoscopia",
    "URS": "Ureteroscopia",
    "OTR": "Otro procedimiento",
}

CONFIG_DEFECTO = {
    "camara": 0,
    "resolucion": [1920, 1080],
    "fps": 30,
    "puerto": 5050,
    "drive": {
        "habilitado": True,
        "carpeta_raiz": "EndoView",
        "compartir": "enlace",
        "dias_acceso": 90,
        "pagina_url": "",
    },
    "teclas": {"foto": ["F8", " "], "grabar": ["F9"]},
}


def _leer(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def _escribir(ruta, datos):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ruta)


def iniciar():
    os.makedirs(ESTUDIOS, exist_ok=True)
    os.makedirs(BASE, exist_ok=True)
    if not os.path.exists(ARCH_CONFIG):
        _escribir(ARCH_CONFIG, CONFIG_DEFECTO)


def config():
    cfg = dict(CONFIG_DEFECTO)
    cfg.update(_leer(ARCH_CONFIG, {}))
    # Configuración heredada de EndoCaptura sin Drive conectado: usar el nombre nuevo
    d = cfg.get("drive", {})
    if d.get("carpeta_raiz") == "EndoCaptura" and not os.path.exists(
            os.path.join(DATOS, "token_drive.json")):
        d["carpeta_raiz"] = "EndoView"
    cfg["drive"] = {**CONFIG_DEFECTO["drive"], **cfg.get("drive", {})}
    cfg["teclas"] = {**CONFIG_DEFECTO["teclas"], **cfg.get("teclas", {})}
    return cfg


def secreto():
    if not os.path.exists(ARCH_SECRETO):
        os.makedirs(DATOS, exist_ok=True)
        with open(ARCH_SECRETO, "w") as f:
            f.write(secrets.token_hex(32))
    with open(ARCH_SECRETO) as f:
        return f.read().strip()


# ---------------- Médicos ----------------
CAMPOS_PERFIL = ["nombre", "especialidad", "cedula", "cedula_esp", "clinica",
                 "direccion", "telefono", "correo"]


def medicos():
    return _leer(ARCH_MEDICOS, {})


def hay_medicos():
    return bool(medicos())


def crear_medico(usuario, clave, perfil):
    usuario = usuario.strip().lower()
    with _lock:
        todos = medicos()
        if usuario in todos:
            raise ValueError("Ese usuario ya existe.")
        todos[usuario] = {
            "clave": generate_password_hash(clave),
            "perfil": {k: perfil.get(k, "").strip() for k in CAMPOS_PERFIL},
            "creado": datetime.now().isoformat(timespec="seconds"),
        }
        _escribir(ARCH_MEDICOS, todos)


def verificar(usuario, clave):
    m = medicos().get(usuario.strip().lower())
    return bool(m) and check_password_hash(m["clave"], clave)


def perfil(usuario):
    m = medicos().get(usuario)
    return m["perfil"] if m else None


def actualizar_perfil(usuario, datos, clave_nueva=None):
    with _lock:
        todos = medicos()
        todos[usuario]["perfil"] = {k: datos.get(k, "").strip() for k in CAMPOS_PERFIL}
        if clave_nueva:
            todos[usuario]["clave"] = generate_password_hash(clave_nueva)
        _escribir(ARCH_MEDICOS, todos)


# ---------------- Estudios ----------------
def nuevo_codigo(proc):
    """Código sin datos del paciente, p. ej. 2026-11-03_EGD_0142."""
    with _lock:
        c = _leer(ARCH_CONTADOR, {"n": 0})
        c["n"] += 1
        _escribir(ARCH_CONTADOR, c)
    return f"{datetime.now():%Y-%m-%d}_{proc}_{c['n']:04d}"


def carpeta(codigo):
    return os.path.join(ESTUDIOS, codigo)


def guardar_estudio(est):
    _escribir(os.path.join(carpeta(est["codigo"]), "estudio.json"), est)


def leer_estudio(codigo):
    return _leer(os.path.join(carpeta(codigo), "estudio.json"), None)


def listar_estudios(usuario=None):
    res = []
    if not os.path.isdir(ESTUDIOS):
        return res
    for cod in sorted(os.listdir(ESTUDIOS), reverse=True):
        e = leer_estudio(cod)
        if e and (usuario is None or e.get("usuario") == usuario):
            res.append(e)
    return res
