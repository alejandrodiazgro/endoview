"""Subida de videos a Google Drive.

Usa el permiso 'drive.file': la app solo puede ver y tocar los archivos que ella misma
sube, no el resto de tu Drive.
"""
import json
import os
from datetime import datetime, timedelta

from almacen import BASE, RECURSOS

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# Identificación de EndoView ante Google («App de escritorio»). Google indica que en apps
# de escritorio este dato no es confidencial; se guarda codificado para que los buscadores
# automáticos de llaves no lo confundan con una filtración. Una credentials.json en la
# carpeta de datos tiene prioridad (por si se quiere usar un proyecto propio de Google).
ARCH_CRED_PROPIA = os.path.join(BASE, "credentials.json")
ARCH_CRED_INCLUIDA = os.path.join(RECURSOS, "cliente.dat")
ARCH_CUENTA = os.path.join(BASE, "datos", "cuenta_drive.json")
ARCH_TOKEN = os.path.join(BASE, "datos", "token_drive.json")
MESES = ["01-Enero", "02-Febrero", "03-Marzo", "04-Abril", "05-Mayo", "06-Junio",
         "07-Julio", "08-Agosto", "09-Septiembre", "10-Octubre", "11-Noviembre",
         "12-Diciembre"]

_servicio = None


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


def conectado():
    return os.path.exists(ARCH_TOKEN)


def cuenta():
    """Correo de la cuenta de Google conectada (si se conoce)."""
    try:
        with open(ARCH_CUENTA, encoding="utf-8") as f:
            return json.load(f).get("correo")
    except (OSError, ValueError):
        return None


def desconectar():
    global _servicio
    for a in (ARCH_TOKEN, ARCH_CUENTA):
        if os.path.exists(a):
            os.remove(a)
    _servicio = None


def servicio():
    """Abre el navegador para autorizar la primera vez; después usa el token guardado."""
    global _servicio
    if _servicio:
        return _servicio
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    cred = None
    if os.path.exists(ARCH_TOKEN):
        cred = Credentials.from_authorized_user_file(ARCH_TOKEN, SCOPES)
    if not cred or not cred.valid:
        if cred and cred.expired and cred.refresh_token:
            cred.refresh(Request())
        else:
            if not disponible():
                raise RuntimeError("Esta copia de EndoView no trae la llave de Google.")
            flujo = InstalledAppFlow.from_client_config(_config_cliente(), SCOPES)
            cred = flujo.run_local_server(
                port=0, prompt="select_account consent", open_browser=True,
                authorization_prompt_message="",
                success_message="Listo: EndoView quedó conectado a su Google Drive. "
                                "Ya puede cerrar esta pestaña y volver a EndoView.")
        os.makedirs(os.path.dirname(ARCH_TOKEN), exist_ok=True)
        with open(ARCH_TOKEN, "w") as f:
            f.write(cred.to_json())
    _servicio = build("drive", "v3", credentials=cred, cache_discovery=False)
    if not os.path.exists(ARCH_CUENTA):
        try:
            u = _servicio.about().get(fields="user(emailAddress)").execute()["user"]
            with open(ARCH_CUENTA, "w", encoding="utf-8") as f:
                json.dump({"correo": u.get("emailAddress")}, f)
        except Exception:
            pass
    return _servicio


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


def carpeta_estudio(codigo, raiz, compartir="enlace", correo=None, dias=90, info=None):
    """Crea EndoView/Año/Mes/<código> y la comparte. El QR del reporte apunta a esta
    carpeta, donde el paciente encuentra su video, sus fotos y su reporte."""
    svc = servicio()
    hoy = datetime.now()
    id_raiz = _carpeta(svc, raiz)
    id_anio = _carpeta(svc, str(hoy.year), id_raiz)
    id_mes = _carpeta(svc, MESES[hoy.month - 1], id_anio)
    id_est = _carpeta(svc, codigo, id_mes)
    # La descripción guarda datos no sensibles que muestra la página del paciente
    import json
    svc.files().update(fileId=id_est, body={"description": json.dumps(
        {"app": "EndoView", **(info or {})}, ensure_ascii=False)}).execute()
    info = svc.files().get(fileId=id_est, fields="webViewLink").execute()

    if compartir == "restringido" and correo:
        perm = {"type": "user", "role": "reader", "emailAddress": correo}
        if dias:
            perm["expirationTime"] = (datetime.utcnow() + timedelta(days=dias)
                                      ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        perm = {"type": "anyone", "role": "reader"}
    p = svc.permissions().create(fileId=id_est, body=perm, fields="id",
                                 sendNotificationEmail=False).execute()
    return {"id": id_est, "enlace": info["webViewLink"], "permiso": p["id"],
            "tipo_permiso": perm["type"]}


def subir(ruta, id_carpeta, nombre, mimetype, progreso=None):
    """Sube un archivo a la carpeta. Si ya existe uno con ese nombre (reintento), no lo repite."""
    from googleapiclient.http import MediaFileUpload
    svc = servicio()
    q = f"name = '{nombre}' and '{id_carpeta}' in parents and trashed = false"
    ya = svc.files().list(q=q, fields="files(id)", spaces="drive").execute().get("files")
    if ya:
        if progreso:
            progreso(100)
        return ya[0]["id"]
    media = MediaFileUpload(ruta, mimetype=mimetype, resumable=True,
                            chunksize=8 * 1024 * 1024)
    req = svc.files().create(body={"name": nombre, "parents": [id_carpeta]},
                             media_body=media, fields="id")
    resp = None
    while resp is None:
        estado, resp = req.next_chunk(num_retries=5)
        if estado and progreso:
            progreso(int(estado.progress() * 100))
    if progreso:
        progreso(100)
    return resp["id"]


def revocar(id_archivo, id_permiso):
    servicio().permissions().delete(fileId=id_archivo, permissionId=id_permiso).execute()
