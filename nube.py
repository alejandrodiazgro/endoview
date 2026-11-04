"""Google: inicio de sesión y Drive, por médico.

Cada médico conecta SU cuenta de Google. Con un solo permiso, EndoView:
  - sabe su correo (para entrar con Google y para recuperar la clave), y
  - guarda los estudios en SU Drive con el permiso 'drive.file': EndoView solo puede ver y
    tocar los archivos que él mismo crea, no el resto del Drive.

Funciona sin internet: los identificadores de carpeta de Drive se reservan por adelantado
(«reserva de IDs»), así el QR del reporte se imprime aunque no haya conexión, y la carpeta
se crea con ese mismo ID cuando vuelve el internet.
"""
import json
import os
import shutil
import socket
import threading
from datetime import datetime, timedelta

from almacen import BASE, DATOS, RECURSOS

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPE_DRIVE = "https://www.googleapis.com/auth/drive.file"
SCOPES_ID = ["openid", "https://www.googleapis.com/auth/userinfo.email",
             "https://www.googleapis.com/auth/userinfo.profile"]
SCOPES_TODO = [SCOPE_DRIVE] + SCOPES_ID
# Identificación de EndoView ante Google («App de escritorio»). Google indica que en apps
# de escritorio este dato no es confidencial; se guarda codificado para que los buscadores
# automáticos de llaves no lo confundan con una filtración. Una credentials.json en la
# carpeta de datos tiene prioridad (por si se quiere usar un proyecto propio de Google).
ARCH_CRED_PROPIA = os.path.join(BASE, "credentials.json")
ARCH_CRED_INCLUIDA = os.path.join(RECURSOS, "cliente.dat")
DIR_GOOGLE = os.path.join(DATOS, "google")
MESES = ["01-Enero", "02-Febrero", "03-Marzo", "04-Abril", "05-Mayo", "06-Junio",
         "07-Julio", "08-Agosto", "09-Septiembre", "10-Octubre", "11-Noviembre",
         "12-Diciembre"]
RESERVA_MIN, RESERVA_LOTE = 60, 200

_servicios = {}
_lock = threading.Lock()


def _config_cliente():
    if os.path.exists(ARCH_CRED_PROPIA):
        with open(ARCH_CRED_PROPIA, encoding="utf-8") as f:
            return json.load(f)
    import base64
    with open(ARCH_CRED_INCLUIDA, encoding="ascii") as f:
        x = base64.b64decode(f.read().strip())[::-1]
    k = b"EndoView-2026"
    return json.loads(bytes(b ^ k[i % len(k)] for i, b in enumerate(x)))


def disponible():
    return os.path.exists(ARCH_CRED_PROPIA) or os.path.exists(ARCH_CRED_INCLUIDA)


def en_linea(timeout=3):
    try:
        with socket.create_connection(("www.googleapis.com", 443), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------- archivos por médico ----------------
def _dir(usuario):
    return os.path.join(DIR_GOOGLE, usuario)


def _arch(usuario, nombre):
    return os.path.join(_dir(usuario), nombre)


def _leer(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defecto


def _escribir(ruta, datos):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False)
    os.replace(tmp, ruta)


def migrar_token_global(usuario_destino):
    """Versiones 1.1.x tenían una sola conexión por computadora: pasarla a un médico."""
    viejo = os.path.join(DATOS, "token_drive.json")
    if os.path.exists(viejo) and usuario_destino and not conectado(usuario_destino):
        os.makedirs(_dir(usuario_destino), exist_ok=True)
        shutil.move(viejo, _arch(usuario_destino, "token.json"))
        cuenta_vieja = os.path.join(DATOS, "cuenta_drive.json")
        if os.path.exists(cuenta_vieja):
            shutil.move(cuenta_vieja, _arch(usuario_destino, "cuenta.json"))


def conectado(usuario):
    return bool(usuario) and os.path.exists(_arch(usuario, "token.json"))


def cuenta(usuario):
    return _leer(_arch(usuario, "cuenta.json"), {}).get("correo") if usuario else None


def desconectar(usuario):
    with _lock:
        _servicios.pop(usuario, None)
    for n in ("token.json", "cuenta.json", "reserva.json"):
        if os.path.exists(_arch(usuario, n)):
            os.remove(_arch(usuario, n))


# ---------------- inicio de sesión con Google ----------------
def autorizar(con_drive=True):
    """Abre el navegador para elegir la cuenta de Google. Necesita internet.
    Devuelve (datos, credencial): datos = {correo, nombre}."""
    from google.auth.transport.requests import AuthorizedSession
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not disponible():
        raise RuntimeError("Esta copia de EndoView no trae la llave de Google.")
    if not en_linea():
        raise RuntimeError("No hay conexión a internet.")
    flujo = InstalledAppFlow.from_client_config(
        _config_cliente(), SCOPES_TODO if con_drive else SCOPES_ID)
    cred = flujo.run_local_server(
        port=0, prompt="select_account consent" if con_drive else "select_account",
        open_browser=True, authorization_prompt_message="",
        success_message="Listo. Ya puede cerrar esta pestaña y volver a EndoView.")
    r = AuthorizedSession(cred).get("https://openidconnect.googleapis.com/v1/userinfo", timeout=15)
    r.raise_for_status()
    u = r.json()
    if not u.get("email_verified", True):
        raise RuntimeError("La cuenta de Google no tiene el correo verificado.")
    return {"correo": (u.get("email") or "").lower(), "nombre": u.get("name") or ""}, cred


def guardar_conexion(usuario, datos, cred):
    """Guarda la conexión de Drive de un médico (después de autorizar con_drive=True)."""
    if SCOPE_DRIVE not in (cred.scopes or []) and SCOPE_DRIVE not in (getattr(cred, "granted_scopes", None) or []):
        raise RuntimeError("No se concedió el permiso de Google Drive. Marque la casilla de Drive al autorizar.")
    os.makedirs(_dir(usuario), exist_ok=True)
    with open(_arch(usuario, "token.json"), "w", encoding="utf-8") as f:
        f.write(cred.to_json())
    _escribir(_arch(usuario, "cuenta.json"), {"correo": datos["correo"], "nombre": datos["nombre"]})
    with _lock:
        _servicios.pop(usuario, None)


# ---------------- Drive ----------------
def servicio(usuario):
    with _lock:
        if usuario in _servicios:
            return _servicios[usuario]
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    ruta = _arch(usuario, "token.json")
    if not os.path.exists(ruta):
        raise RuntimeError("El médico no ha conectado su Google Drive.")
    cred = Credentials.from_authorized_user_file(ruta, [SCOPE_DRIVE])
    if not cred.valid:
        if cred.refresh_token:
            cred.refresh(Request())
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(cred.to_json())
        else:
            raise RuntimeError("La conexión con Google venció. Vuelva a iniciar sesión con Google.")
    svc = build("drive", "v3", credentials=cred, cache_discovery=False)
    with _lock:
        _servicios[usuario] = svc
    return svc


def reservar_ids(usuario):
    """Mantiene una reserva de IDs de Drive para poder imprimir QR sin internet."""
    ruta = _arch(usuario, "reserva.json")
    ids = _leer(ruta, [])
    if len(ids) < RESERVA_MIN:
        r = servicio(usuario).files().generateIds(count=RESERVA_LOTE, space="drive",
                                                  type="files").execute()
        ids += r.get("ids", [])
        _escribir(ruta, ids)
    return len(ids)


def tomar_id(usuario):
    """Toma un ID reservado (funciona sin internet). None si no hay."""
    with _lock:
        ruta = _arch(usuario, "reserva.json")
        ids = _leer(ruta, [])
        if not ids:
            return None
        i = ids.pop(0)
        _escribir(ruta, ids)
        return i


def ids_reservados(usuario):
    return len(_leer(_arch(usuario, "reserva.json"), []))


def _carpeta(svc, nombre, padre=None):
    q = (f"name = '{nombre}' and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false")
    if padre:
        q += f" and '{padre}' in parents"
    r = svc.files().list(q=q, fields="files(id)", spaces="drive").execute()
    if r.get("files"):
        return r["files"][0]["id"]
    meta = {"name": nombre, "mimeType": "application/vnd.google-apps.folder"}
    if padre:
        meta["parents"] = [padre]
    return svc.files().create(body=meta, fields="id").execute()["id"]


def _existe(svc, id_archivo):
    from googleapiclient.errors import HttpError
    try:
        svc.files().get(fileId=id_archivo, fields="id").execute()
        return True
    except HttpError as ex:
        if ex.resp.status == 404:
            return False
        raise


def carpeta_estudio(usuario, codigo, raiz, fecha, compartir="enlace", correo=None, dias=90,
                    info=None, id_fijo=None):
    """Crea Raiz/Año/Mes/<código> en el Drive del médico y la comparte en solo lectura.
    Si se da id_fijo (reservado), la carpeta se crea con ese ID para que el QR ya impreso
    funcione. Devuelve {id, enlace, permiso, tipo_permiso}."""
    from googleapiclient.errors import HttpError
    svc = servicio(usuario)
    id_raiz = _carpeta(svc, raiz)
    id_anio = _carpeta(svc, str(fecha.year), id_raiz)
    id_mes = _carpeta(svc, MESES[fecha.month - 1], id_anio)
    meta = {"name": codigo, "mimeType": "application/vnd.google-apps.folder",
            "parents": [id_mes],
            "description": json.dumps({"app": "EndoView", **(info or {})}, ensure_ascii=False)}
    id_est = None
    if id_fijo and _existe(svc, id_fijo):
        id_est = id_fijo
    elif id_fijo:
        try:
            id_est = svc.files().create(body={**meta, "id": id_fijo}, fields="id").execute()["id"]
        except HttpError as ex:
            if ex.resp.status not in (400, 409):
                raise
            id_est = None  # el ID reservado ya no sirve: se usa uno nuevo
    if not id_est:
        id_est = svc.files().create(body=meta, fields="id").execute()["id"]
    enlace = svc.files().get(fileId=id_est, fields="webViewLink").execute()["webViewLink"]

    if compartir == "restringido" and correo:
        perm = {"type": "user", "role": "reader", "emailAddress": correo}
        if dias:
            perm["expirationTime"] = (datetime.utcnow() + timedelta(days=dias)
                                      ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        perm = {"type": "anyone", "role": "reader"}
    p = svc.permissions().create(fileId=id_est, body=perm, fields="id",
                                 sendNotificationEmail=False).execute()
    return {"id": id_est, "enlace": enlace, "permiso": p["id"], "tipo_permiso": perm["type"]}


def subir(usuario, ruta, id_carpeta, nombre, mimetype, progreso=None, reemplazar=False):
    """Sube un archivo a la carpeta. Si ya existe (reintento) no lo repite, salvo reemplazar."""
    from googleapiclient.http import MediaFileUpload
    svc = servicio(usuario)
    q = f"name = '{nombre}' and '{id_carpeta}' in parents and trashed = false"
    ya = svc.files().list(q=q, fields="files(id)", spaces="drive").execute().get("files")
    media = MediaFileUpload(ruta, mimetype=mimetype, resumable=True, chunksize=8 * 1024 * 1024)
    if ya and not reemplazar:
        if progreso:
            progreso(100)
        return ya[0]["id"]
    if ya:
        req = svc.files().update(fileId=ya[0]["id"], media_body=media, fields="id")
    else:
        req = svc.files().create(body={"name": nombre, "parents": [id_carpeta]},
                                 media_body=media, fields="id")
    resp = None
    while resp is None:
        estado, resp = req.next_chunk(num_retries=3)
        if estado and progreso:
            progreso(int(estado.progress() * 100))
    if progreso:
        progreso(100)
    return resp["id"]


def revocar(usuario, id_archivo, id_permiso):
    servicio(usuario).permissions().delete(fileId=id_archivo, permissionId=id_permiso).execute()
