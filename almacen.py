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


# ---------------- Médicos (usuarios) ----------------
CAMPOS_PERFIL = ["nombre", "especialidad", "cedula", "cedula_esp", "clinica",
                 "direccion", "telefono", "correo"]
_ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O ni 1/I para que se lea fácil


def _codigo_aleatorio(grupos=3, largo=4):
    return "-".join("".join(secrets.choice(_ALFABETO) for _ in range(largo))
                    for _ in range(grupos))


def _normalizar(todos):
    """Completa datos de usuarios creados con versiones anteriores. Garantiza un administrador."""
    cambio = False
    for m in todos.values():
        for k, v in (("rol", "medico"), ("activo", True), ("google", None)):
            if k not in m:
                m[k] = v
                cambio = True
    activos = [u for u, m in todos.items() if m["activo"]]
    if activos and not any(todos[u]["rol"] == "admin" for u in activos):
        primero = min(activos, key=lambda u: todos[u].get("creado", ""))
        todos[primero]["rol"] = "admin"
        cambio = True
    return cambio


def medicos():
    todos = _leer(ARCH_MEDICOS, {})
    if _normalizar(todos):
        with _lock:
            _escribir(ARCH_MEDICOS, todos)
    return todos


def hay_medicos():
    return bool(medicos())


def _guardar(todos):
    _escribir(ARCH_MEDICOS, todos)


def _ahora():
    return datetime.now().isoformat(timespec="seconds")


def _tocar(m):
    """Cambio que debe viajar a las otras computadoras del médico (vía su Drive)."""
    m["actualizado"] = _ahora()
    m["perfil_sync"] = True


def crear_medico(usuario, clave, perfil, rol=None, google=None):
    """Crea el usuario y devuelve su código de recuperación (se muestra una sola vez)."""
    usuario = usuario.strip().lower()
    if not usuario.replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise ValueError("El usuario solo puede tener letras, números, punto, guion o guion bajo.")
    codigo = _codigo_aleatorio()
    with _lock:
        todos = medicos()
        if usuario in todos:
            raise ValueError("Ese usuario ya existe.")
        if google and any(m.get("google") == google for m in todos.values()):
            raise ValueError("Esa cuenta de Google ya está vinculada a otro usuario.")
        todos[usuario] = {
            "clave": generate_password_hash(clave),
            "perfil": {k: (perfil.get(k) or "").strip() for k in CAMPOS_PERFIL},
            "creado": datetime.now().isoformat(timespec="seconds"),
            "rol": rol or ("admin" if not todos else "medico"),
            "activo": True,
            "google": google,
            "recuperacion": generate_password_hash(codigo),
            "debe_cambiar": False,
        }
        _tocar(todos[usuario])
        _guardar(todos)
    return codigo


def verificar(usuario, clave):
    """'ok', 'inactivo' o None si el usuario o la clave no coinciden."""
    m = medicos().get(usuario.strip().lower())
    if not m or not check_password_hash(m["clave"], clave):
        return None
    return "ok" if m["activo"] else "inactivo"


def verificar_activo(usuario):
    m = medicos().get(usuario)
    return None if not m else bool(m["activo"])


def usuario_info(usuario):
    m = medicos().get(usuario)
    if not m:
        return None
    return {"usuario": usuario, "rol": m["rol"], "activo": m["activo"],
            "google": m.get("google"), "debe_cambiar": m.get("debe_cambiar", False),
            "creado": m.get("creado", ""), **m["perfil"]}


def lista_medicos():
    return sorted((usuario_info(u) for u in medicos()), key=lambda x: (not x["activo"], x["nombre"].lower()))


def es_admin(usuario):
    m = medicos().get(usuario)
    return bool(m and m["activo"] and m["rol"] == "admin")


def perfil(usuario):
    m = medicos().get(usuario)
    return m["perfil"] if m and m["activo"] else None


def actualizar_perfil(usuario, datos, clave_nueva=None):
    with _lock:
        todos = medicos()
        todos[usuario]["perfil"] = {k: (datos.get(k) or "").strip() for k in CAMPOS_PERFIL}
        if clave_nueva:
            todos[usuario]["clave"] = generate_password_hash(clave_nueva)
            todos[usuario]["debe_cambiar"] = False
        _tocar(todos[usuario])
        _guardar(todos)


def cambiar_clave(usuario, nueva):
    with _lock:
        todos = medicos()
        todos[usuario]["clave"] = generate_password_hash(nueva)
        todos[usuario]["debe_cambiar"] = False
        _tocar(todos[usuario])
        _guardar(todos)


def restablecer_clave(usuario):
    """Clave temporal (la pide cambiar al entrar). Devuelve la clave temporal."""
    temporal = _codigo_aleatorio(2, 4)
    with _lock:
        todos = medicos()
        todos[usuario]["clave"] = generate_password_hash(temporal)
        todos[usuario]["debe_cambiar"] = True
        _guardar(todos)
    return temporal


def nuevo_codigo_recuperacion(usuario):
    codigo = _codigo_aleatorio()
    with _lock:
        todos = medicos()
        todos[usuario]["recuperacion"] = generate_password_hash(codigo)
        _tocar(todos[usuario])
        _guardar(todos)
    return codigo


def recuperar_con_codigo(usuario, codigo, nueva):
    usuario = usuario.strip().lower()
    codigo = codigo.strip().upper().replace(" ", "")
    if len(codigo) == 12 and "-" not in codigo:
        codigo = f"{codigo[:4]}-{codigo[4:8]}-{codigo[8:]}"
    m = medicos().get(usuario)
    if not m or not m["activo"] or not m.get("recuperacion") or \
            not check_password_hash(m["recuperacion"], codigo):
        return False
    cambiar_clave(usuario, nueva)
    return True


def _admins_activos(todos, excepto=None):
    return [u for u, m in todos.items() if m["activo"] and m["rol"] == "admin" and u != excepto]


def poner_activo(usuario, activo):
    with _lock:
        todos = medicos()
        if not activo and todos[usuario]["rol"] == "admin" and not _admins_activos(todos, usuario):
            raise ValueError("Debe quedar al menos un administrador activo.")
        todos[usuario]["activo"] = bool(activo)
        _guardar(todos)


def poner_rol(usuario, rol):
    with _lock:
        todos = medicos()
        if rol != "admin" and todos[usuario]["rol"] == "admin" and not _admins_activos(todos, usuario):
            raise ValueError("Debe quedar al menos un administrador activo.")
        todos[usuario]["rol"] = "admin" if rol == "admin" else "medico"
        _guardar(todos)


def vincular_google(usuario, correo):
    with _lock:
        todos = medicos()
        if correo and any(m.get("google") == correo for u, m in todos.items() if u != usuario):
            raise ValueError("Esa cuenta de Google ya está vinculada a otro usuario.")
        todos[usuario]["google"] = correo
        if correo:
            todos[usuario]["perfil_sync"] = True
        _guardar(todos)


# ---- usuarios compartidos entre computadoras (copia en el Drive de cada médico) ----
def exportar_usuario(usuario):
    """Lo que se guarda en el Drive del médico. La clave y el código van cifrados (hash)."""
    m = medicos()[usuario]
    return {"app": "EndoView", "tipo": "usuario", "usuario": usuario, "google": m.get("google"),
            "clave": m["clave"], "recuperacion": m.get("recuperacion"), "perfil": m["perfil"],
            "actualizado": m.get("actualizado") or m.get("creado") or _ahora()}


def pendientes_perfil():
    return [u for u, m in medicos().items() if m.get("perfil_sync") and m.get("google") and m["activo"]]


def marcar_perfil_subido(usuario, actualizado):
    with _lock:
        todos = medicos()
        m = todos.get(usuario)
        if m and (m.get("actualizado") or "") <= actualizado:
            m["perfil_sync"] = False
            _guardar(todos)


def importar_usuario(d, google):
    """Trae (o actualiza) un médico desde la copia de su Drive.
    Devuelve el nombre de usuario local. Gana el cambio más reciente."""
    if not d or d.get("tipo") != "usuario" or not d.get("clave"):
        raise ValueError("La copia del usuario en Drive no es válida.")
    with _lock:
        todos = medicos()
        local = next((u for u, m in todos.items() if m.get("google") == google), None)
        if local:
            m = todos[local]
            if (d.get("actualizado") or "") > (m.get("actualizado") or ""):
                m.update(clave=d["clave"], recuperacion=d.get("recuperacion"), debe_cambiar=False,
                         perfil={k: (d["perfil"].get(k) or "") for k in CAMPOS_PERFIL},
                         actualizado=d["actualizado"], perfil_sync=False)
                _guardar(todos)
            return local
        u = d["usuario"]
        if u in todos:  # el nombre ya lo usa otro médico en esta computadora
            n = 2
            while f"{d['usuario']}{n}" in todos:
                n += 1
            u = f"{d['usuario']}{n}"
        todos[u] = {"clave": d["clave"], "recuperacion": d.get("recuperacion"),
                    "perfil": {k: (d["perfil"].get(k) or "") for k in CAMPOS_PERFIL},
                    "creado": _ahora(), "rol": "medico" if todos else "admin", "activo": True,
                    "google": google, "debe_cambiar": False,
                    "actualizado": d.get("actualizado") or _ahora(),
                    "perfil_sync": u != d["usuario"]}
        _guardar(todos)
        return u


def buscar_por_google(correo):
    for u, m in medicos().items():
        if correo and m.get("google") == correo:
            return u
    return None


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


def pendientes_de_subir():
    return [e for e in listar_estudios() if e.get("sync") in ("pendiente", "subiendo", "error")]


def listar_estudios(usuario=None):
    res = []
    if not os.path.isdir(ESTUDIOS):
        return res
    for cod in sorted(os.listdir(ESTUDIOS), reverse=True):
        e = leer_estudio(cod)
        if e and (usuario is None or e.get("usuario") == usuario):
            res.append(e)
    return res
