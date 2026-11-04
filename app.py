"""EndoView: captura de procedimientos endoscópicos, subida a Drive y reporte con QR.

Ejecutar:  python app.py   (se abre el navegador en http://127.0.0.1:5050)
"""
import os
import platform
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)

import actualizador
import almacen as A
import nube
from version import VERSION, VISOR
import reporte
from captura import Capturador, convertir_h264, listar_dispositivos

A.iniciar()
CFG = A.config()
app = Flask(__name__, template_folder=os.path.join(A.RECURSOS, "templates"))
app.secret_key = A.secreto()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

cam = None
_trabajos = set()
_lock_trab = threading.Lock()


def camara():
    global cam
    if cam is None:
        cam = Capturador(CFG["camara"], CFG["resolucion"], CFG["fps"])
    return cam


# ---------------- sesión ----------------
def requiere_login(f):
    @wraps(f)
    def envoltura(*a, **k):
        u = session.get("usuario")
        if not u or not A.perfil(u):  # no existe o fue desactivado
            session.clear()
            return redirect(url_for("login"))
        if A.usuario_info(u)["debe_cambiar"] and request.endpoint not in ("clave", "salir"):
            return redirect(url_for("clave"))
        return f(*a, **k)
    return envoltura


def requiere_admin(f):
    @wraps(f)
    @requiere_login
    def envoltura(*a, **k):
        if not A.es_admin(session["usuario"]):
            abort(403)
        return f(*a, **k)
    return envoltura


def mi_estudio(codigo):
    e = A.leer_estudio(codigo)
    if not e or (e.get("usuario") != session.get("usuario") and not A.es_admin(session.get("usuario"))):
        abort(404)
    return e


@app.context_processor
def globales():
    u = session.get("usuario")
    return {"medico": A.perfil(u) if u else None, "usuario": u,
            "es_admin": A.es_admin(u) if u else False,
            "procedimientos": A.PROCEDIMIENTOS, "cfg": CFG,
            "drive_listo": nube.conectado(u) if u else False, "version": VERSION}


def _entrar(u):
    session.clear()
    session.permanent = True
    session["usuario"] = u


@app.route("/login", methods=["GET", "POST"])
def login():
    if not A.hay_medicos():
        return redirect(url_for("registro"))
    if request.method == "POST":
        u = request.form.get("usuario", "").strip().lower()
        r = A.verificar(u, request.form.get("clave", ""))
        if r == "ok":
            _entrar(u)
            return redirect(url_for("inicio"))
        flash("Este usuario está desactivado. Pida ayuda al administrador." if r == "inactivo"
              else "Usuario o clave incorrectos.")
    return render_template("login.html", google=nube.disponible())


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("login"))


# ---------------- Google: un flujo a la vez (abre el navegador) ----------------
_g = {"estado": None, "proposito": None, "datos": None, "cred": None, "error": None, "llave": None}
_g_lock = threading.Lock()


@app.route("/google/iniciar", methods=["POST"])
def google_iniciar():
    proposito = request.get_json(force=True).get("proposito")
    if proposito not in ("login", "registro", "conectar", "recuperar"):
        abort(400)
    if proposito == "conectar" and not session.get("usuario"):
        abort(403)
    if proposito == "registro" and A.hay_medicos() and not A.es_admin(session.get("usuario")):
        abort(403)
    with _g_lock:
        if _g["estado"] == "esperando":
            return jsonify(error="Ya hay una ventana de Google abierta. Termine ese paso primero."), 409
        llave = secrets.token_hex(16)
        _g.update(estado="esperando", proposito=proposito, datos=None, cred=None, error=None, llave=llave)
    session["google_llave"] = llave

    def tarea():
        try:
            datos, cred = nube.autorizar(con_drive=True)
            _g.update(estado="ok", datos=datos, cred=cred)
        except Exception as ex:
            _g.update(estado="error", error=str(ex)[:300])
    threading.Thread(target=tarea, daemon=True).start()
    return jsonify(ok=True)


@app.route("/google/estado")
def google_estado():
    if session.get("google_llave") != _g["llave"]:
        return jsonify(estado=None)
    return jsonify(estado=_g["estado"], error=_g["error"])


@app.route("/google/completar", methods=["POST"])
def google_completar():
    if session.get("google_llave") != _g["llave"] or _g["estado"] != "ok":
        return jsonify(error="El inicio de sesión con Google no se completó."), 400
    datos, cred, proposito = _g["datos"], _g["cred"], _g["proposito"]
    _g.update(estado=None, cred=None, llave=None)
    session.pop("google_llave", None)
    correo = datos["correo"]

    if proposito in ("login", "recuperar"):
        try:
            u, traido = _usuario_desde_google(datos, cred)
        except Exception as ex:
            return jsonify(error=str(ex)), 400
        if not u:
            return jsonify(error=f"La cuenta {correo} no está vinculada a ningún usuario de EndoView "
                                 "(ni en esta computadora ni en su Drive). Entre con su usuario y clave, "
                                 "y vincúlela en «Mi Google Drive»."), 400
        if A.verificar_activo(u) is False:
            return jsonify(error="Este usuario está desactivado en esta computadora."), 400
        _entrar(u)
        if traido:
            flash("Se trajo su usuario desde su Google Drive: ya puede usar EndoView en esta computadora, "
                  "también sin internet, con su misma clave.")
        if proposito == "recuperar":
            session["puede_cambiar_sin_clave"] = True
            return jsonify(ir=url_for("clave"))
        return jsonify(ir=url_for("inicio"))

    if proposito == "conectar":
        u = session["usuario"]
        try:
            A.vincular_google(u, correo)
            nube.guardar_conexion(u, datos, cred)
        except Exception as ex:
            return jsonify(error=str(ex)), 400
        try:  # si su usuario en Drive es más reciente (cambió la clave en otra PC), se actualiza
            remoto = nube.leer_usuario_remoto(nube.servicio_de(cred))
            if remoto:
                A.importar_usuario(remoto, correo)
        except Exception:
            pass
        _marcar_para_subir(u)
        threading.Thread(target=lambda: _reservar(u), daemon=True).start()
        return jsonify(ir=url_for("drive"))

    # registro: si el médico ya tiene usuario en su Drive (otra computadora), se trae.
    try:
        remoto = nube.leer_usuario_remoto(nube.servicio_de(cred))
    except Exception:
        remoto = None
    if remoto:
        primero = not A.hay_medicos()
        u, _ = _usuario_desde_google(datos, cred, remoto)
        if primero:
            _entrar(u)
        flash(f"{remoto['perfil'].get('nombre') or correo} ya tenía usuario de EndoView en otra computadora: "
              f"se trajo desde su Google Drive (usuario «{u}», con su misma clave).")
        return jsonify(ir=url_for("inicio") if primero else url_for("usuarios"))

    # registro nuevo: se guarda para usarse al enviar el formulario
    session["google_registro"] = {"correo": correo, "nombre": datos["nombre"]}
    _pendientes_google[correo] = (datos, cred)
    return jsonify(correo=correo, nombre=datos["nombre"])


_pendientes_google = {}


def _usuario_desde_google(datos, cred, remoto=None):
    """Busca al médico por su cuenta de Google en esta computadora; si no está, lo trae de la
    copia en su Drive. Conecta su Drive en esta computadora. Devuelve (usuario, se_trajo)."""
    correo = datos["correo"]
    u = A.buscar_por_google(correo)
    if remoto is None:
        try:
            remoto = nube.leer_usuario_remoto(nube.servicio_de(cred))
        except Exception:
            remoto = None
    traido = False
    if remoto:
        nuevo = u is None
        u = A.importar_usuario(remoto, correo)  # crea o actualiza (gana lo más reciente)
        traido = nuevo
    if u and A.verificar_activo(u):
        try:
            nube.guardar_conexion(u, datos, cred)
            _marcar_para_subir(u)
            threading.Thread(target=lambda: _reservar(u), daemon=True).start()
        except Exception:
            pass
    return u, traido


def _reservar(u):
    try:
        nube.reservar_ids(u)
    except Exception:
        pass
    avisar_sincronizador()


def _marcar_para_subir(u):
    """Al conectar Drive, los estudios que se hicieron sin conexión también se suben."""
    for e in A.listar_estudios(u):
        if e["estado"] == "listo" and e.get("sync") == "sin_drive":
            e["sync"] = "pendiente"
            A.guardar_estudio(e)
    avisar_sincronizador()


# ---------------- usuarios ----------------
def _validar_clave(f, obligatoria=True):
    c = f.get("clave", "")
    if not c and not obligatoria:
        return None
    if len(c) < 6:
        raise ValueError("La clave debe tener al menos 6 caracteres.")
    if c != f.get("clave2"):
        raise ValueError("Las claves no coinciden.")
    return c


@app.route("/registro", methods=["GET", "POST"])
def registro():
    primero = not A.hay_medicos()
    if not primero and not A.es_admin(session.get("usuario")):
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        g = session.get("google_registro") if f.get("con_google") else None
        try:
            clave = _validar_clave(f)
            if not f.get("usuario", "").strip() or not f.get("nombre", "").strip():
                raise ValueError("Usuario y nombre son obligatorios.")
            rol = "admin" if primero else ("admin" if f.get("rol") == "admin" else "medico")
            codigo = A.crear_medico(f["usuario"], clave, f, rol=rol, google=g["correo"] if g else None)
        except ValueError as e:
            flash(str(e))
            return render_template("perfil.html", modo="registro", primero=primero, datos=f,
                                   google=nube.disponible(), g=g)
        u = f["usuario"].strip().lower()
        if g and g["correo"] in _pendientes_google:
            datos, cred = _pendientes_google.pop(g["correo"])
            try:
                nube.guardar_conexion(u, datos, cred)
                threading.Thread(target=lambda: _reservar(u), daemon=True).start()
            except Exception as ex:
                flash("La cuenta se creó, pero no se pudo conectar Google Drive: " + str(ex))
        session.pop("google_registro", None)
        if primero:
            _entrar(u)
        session["codigo_mostrar"] = {"usuario": u, "nombre": f["nombre"].strip(), "codigo": codigo}
        return redirect(url_for("codigo_recuperacion"))
    g = session.get("google_registro")
    if g and g["correo"] not in _pendientes_google:  # quedó de un intento anterior
        session.pop("google_registro", None)
        g = None
    return render_template("perfil.html", modo="registro", primero=primero, datos={},
                           google=nube.disponible(), g=g)


@app.route("/codigo")
@requiere_login
def codigo_recuperacion():
    c = session.pop("codigo_mostrar", None)
    if not c:
        return redirect(url_for("inicio"))
    return render_template("codigo.html", c=c)


@app.route("/perfil", methods=["GET", "POST"])
@requiere_login
def perfil():
    u = session["usuario"]
    if request.method == "POST":
        A.actualizar_perfil(u, request.form)
        avisar_sincronizador()
        flash("Perfil guardado.")
        return redirect(url_for("perfil"))
    return render_template("perfil.html", modo="perfil", datos=A.perfil(u),
                           info=A.usuario_info(u), cuenta_google=nube.cuenta(u))


@app.route("/perfil/codigo", methods=["POST"])
@requiere_login
def perfil_codigo():
    u = session["usuario"]
    if not A.verificar(u, request.form.get("clave_actual", "")) == "ok":
        flash("La clave actual no es correcta.")
        return redirect(url_for("perfil"))
    session["codigo_mostrar"] = {"usuario": u, "nombre": A.perfil(u)["nombre"],
                                 "codigo": A.nuevo_codigo_recuperacion(u)}
    return redirect(url_for("codigo_recuperacion"))


@app.route("/clave", methods=["GET", "POST"])
@requiere_login
def clave():
    u = session["usuario"]
    info = A.usuario_info(u)
    sin_actual = info["debe_cambiar"] or session.get("puede_cambiar_sin_clave")
    if request.method == "POST":
        f = request.form
        try:
            if not sin_actual and A.verificar(u, f.get("clave_actual", "")) != "ok":
                raise ValueError("La clave actual no es correcta.")
            A.cambiar_clave(u, _validar_clave(f))
            avisar_sincronizador()
        except ValueError as e:
            flash(str(e))
            return render_template("clave.html", sin_actual=sin_actual, info=info)
        session.pop("puede_cambiar_sin_clave", None)
        flash("Clave cambiada.")
        return redirect(url_for("inicio"))
    return render_template("clave.html", sin_actual=sin_actual, info=info)


@app.route("/recuperar", methods=["GET", "POST"])
def recuperar():
    if request.method == "POST":
        f = request.form
        try:
            nueva = _validar_clave(f)
        except ValueError as e:
            flash(str(e))
            return render_template("recuperar.html", google=nube.disponible())
        if A.recuperar_con_codigo(f.get("usuario", ""), f.get("codigo", ""), nueva):
            avisar_sincronizador()
            flash("Listo, su clave fue cambiada. Ya puede entrar.")
            return redirect(url_for("login"))
        flash("El usuario o el código de recuperación no coinciden.")
    return render_template("recuperar.html", google=nube.disponible())


@app.route("/usuarios")
@requiere_admin
def usuarios():
    lista = A.lista_medicos()
    for m in lista:
        m["drive"] = nube.cuenta(m["usuario"]) if nube.conectado(m["usuario"]) else None
    return render_template("usuarios.html", lista=lista)


@app.route("/usuarios/<u>/editar", methods=["GET", "POST"])
@requiere_admin
def usuario_editar(u):
    if not A.medicos().get(u):
        abort(404)
    if request.method == "POST":
        A.actualizar_perfil(u, request.form)
        flash("Datos guardados.")
        return redirect(url_for("usuarios"))
    return render_template("perfil.html", modo="editar", datos=A.medicos()[u]["perfil"],
                           info=A.usuario_info(u))


@app.route("/usuarios/<u>/accion", methods=["POST"])
@requiere_admin
def usuario_accion(u):
    if not A.medicos().get(u):
        abort(404)
    acc = request.form.get("accion")
    try:
        if acc == "restablecer":
            temporal = A.restablecer_clave(u)
            flash(f"Clave temporal de «{u}»: {temporal}  — désela al médico; al entrar se le pedirá cambiarla.")
        elif acc in ("activar", "desactivar"):
            if u == session["usuario"] and acc == "desactivar":
                raise ValueError("No puede desactivar su propio usuario.")
            A.poner_activo(u, acc == "activar")
            flash(f"Usuario «{u}» {'activado' if acc == 'activar' else 'desactivado'}.")
        elif acc in ("admin", "medico"):
            A.poner_rol(u, acc)
            flash(f"«{u}» ahora es {'administrador' if acc == 'admin' else 'médico'}.")
        elif acc == "desvincular":
            A.vincular_google(u, None)
            nube.desconectar(u)
            flash(f"Se desvinculó la cuenta de Google de «{u}».")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("usuarios"))


# ---------------- inicio / iniciar procedimiento ----------------
@app.route("/")
@requiere_login
def inicio():
    activo = session.get("estudio")
    if activo and A.leer_estudio(activo) and A.leer_estudio(activo)["estado"] == "capturando":
        return redirect(url_for("captura"))
    u = session["usuario"]
    estudios = A.listar_estudios(None if A.es_admin(u) else u)[:40]
    nombres = {x["usuario"]: x["nombre"] for x in A.lista_medicos()}
    for e in estudios:
        e["medico_nombre"] = nombres.get(e["usuario"], e["usuario"])
    return render_template("inicio.html", estudios=estudios, ver_todos=A.es_admin(u))


@app.route("/iniciar", methods=["POST"])
@requiere_login
def iniciar():
    f = request.form
    nombre, edad, proc = f.get("paciente", "").strip(), f.get("edad", "").strip(), f.get("proc")
    if not nombre or not edad.isdigit() or proc not in A.PROCEDIMIENTOS:
        flash("Escriba el nombre completo, la edad (en años) y el procedimiento.")
        return redirect(url_for("inicio"))
    nombre_proc = f.get("otro", "").strip() if proc == "OTR" and f.get("otro") else A.PROCEDIMIENTOS[proc]
    codigo = A.nuevo_codigo(proc)
    os.makedirs(os.path.join(A.carpeta(codigo), "fotos"), exist_ok=True)
    A.guardar_estudio({
        "codigo": codigo, "usuario": session["usuario"], "paciente": nombre,
        "edad": int(edad), "proc": proc, "procedimiento": nombre_proc,
        "correo_paciente": f.get("correo", "").strip(),
        "inicio": datetime.now().isoformat(timespec="seconds"),
        "estado": "capturando", "fotos": [],
    })
    session["estudio"] = codigo
    camara()
    return redirect(url_for("captura"))


@app.route("/captura")
@requiere_login
def captura():
    codigo = session.get("estudio")
    e = A.leer_estudio(codigo) if codigo else None
    if not e or e["estado"] != "capturando":
        session.pop("estudio", None)
        return redirect(url_for("inicio"))
    return render_template("captura.html", e=e)


@app.route("/cancelar", methods=["POST"])
@requiere_login
def cancelar():
    codigo = session.pop("estudio", None)
    if codigo:
        mi_estudio(codigo)
        camara().detener()
        shutil.rmtree(A.carpeta(codigo), ignore_errors=True)
    return redirect(url_for("inicio"))


# ---------------- video en vivo y controles ----------------
@app.route("/video_feed")
@requiere_login
def video_feed():
    c = camara()

    def gen():
        while True:
            j = c.jpeg()
            if j:
                yield b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + j + b"\r\n"
            time.sleep(1 / 20)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=f")


def _estudio_activo():
    codigo = session.get("estudio")
    e = A.leer_estudio(codigo) if codigo else None
    if not e or e["estado"] != "capturando":
        abort(409)
    return e


@app.route("/api/foto", methods=["POST"])
@requiere_login
def api_foto():
    e = _estudio_activo()
    nombre = f"foto_{len(e['fotos']) + 1:03d}_{datetime.now():%H%M%S}.jpg"
    if not camara().foto(os.path.join(A.carpeta(e["codigo"]), "fotos", nombre)):
        return jsonify(error="Sin señal de video"), 500
    usados = {f.get("orden") for f in e["fotos"]}
    libre = next((n for n in range(1, 9) if n not in usados), None)
    e["fotos"].append({"archivo": nombre, "incluir": libre is not None, "orden": libre, "nota": ""})
    A.guardar_estudio(e)
    return jsonify(archivo=nombre, orden=libre,
                   url=url_for("archivo", codigo=e["codigo"], ruta="fotos/" + nombre))


@app.route("/api/foto/<archivo>", methods=["DELETE"])
@requiere_login
def api_borrar_foto(archivo):
    e = _estudio_activo()
    e["fotos"] = [f for f in e["fotos"] if f["archivo"] != archivo]
    A.guardar_estudio(e)
    try:
        os.remove(os.path.join(A.carpeta(e["codigo"]), "fotos", os.path.basename(archivo)))
    except OSError:
        pass
    return jsonify(ok=True)


@app.route("/api/grabar", methods=["POST"])
@requiere_login
def api_grabar():
    """Alterna (para el pedal). Los botones usan /iniciar y /detener."""
    e = _estudio_activo()
    camara().alternar_grabacion(A.carpeta(e["codigo"]))
    return jsonify(camara().estado())


@app.route("/api/grabar/iniciar", methods=["POST"])
@requiere_login
def api_grabar_iniciar():
    e = _estudio_activo()
    camara().iniciar_grabacion(A.carpeta(e["codigo"]))
    return jsonify(camara().estado())


@app.route("/api/grabar/detener", methods=["POST"])
@requiere_login
def api_grabar_detener():
    _estudio_activo()
    camara().detener_grabacion()
    return jsonify(camara().estado())


@app.route("/api/camaras")
@requiere_login
def api_camaras():
    c = camara()
    actual = None if c.demo else c.indice
    return jsonify(actual=actual, dispositivos=listar_dispositivos(actual))


@app.route("/api/camara", methods=["POST"])
@requiere_login
def api_camara():
    c = camara()
    if c.estado()["grabando"]:
        return jsonify(error="Detenga la grabación antes de cambiar de fuente."), 409
    indice = int(request.get_json(force=True)["indice"])
    ok = c.cambiar(indice)
    if ok:
        cfg = A._leer(A.ARCH_CONFIG, {})
        cfg["camara"] = indice
        A._escribir(A.ARCH_CONFIG, cfg)
        CFG["camara"] = indice
    return jsonify(ok=ok, **c.estado())


@app.route("/api/estado")
@requiere_login
def api_estado():
    return jsonify(camara().estado())


@app.route("/api/finalizar", methods=["POST"])
@requiere_login
def api_finalizar():
    e = _estudio_activo()
    d = request.get_json(force=True)
    for k in ("hallazgos", "diagnostico", "recomendaciones"):
        e[k] = d.get(k, "").strip()
    sel = {f["archivo"]: f for f in d.get("fotos", [])}
    for f in e["fotos"]:
        if f["archivo"] in sel:
            orden = sel[f["archivo"]].get("orden")
            f["orden"] = int(orden) if orden and 1 <= int(orden) <= 8 else None
            f["incluir"] = f["orden"] is not None
            f["nota"] = sel[f["archivo"]].get("nota", "").strip()[:120]
    crudo, fps = camara().detener()
    e["video_crudo"] = os.path.basename(crudo) if crudo else None
    e["fps_real"] = fps
    e["fin"] = datetime.now().isoformat(timespec="seconds")
    e["estado"] = "procesando"
    A.guardar_estudio(e)
    session.pop("estudio", None)
    lanzar(e["codigo"])
    return jsonify(url=url_for("estudio", codigo=e["codigo"]))


# ---------------- procesamiento (local, inmediato) ----------------
def lanzar(codigo):
    with _lock_trab:
        if codigo in _trabajos:
            return
        _trabajos.add(codigo)
    threading.Thread(target=procesar, args=(codigo,), daemon=True).start()


def _fotos_reporte(e):
    carp = A.carpeta(e["codigo"])
    elegidas = [f for f in sorted((f for f in e["fotos"] if f.get("orden")), key=lambda f: f["orden"])
                if os.path.exists(os.path.join(carp, "fotos", f["archivo"]))][:8]
    return [{"ruta": os.path.join(carp, "fotos", f["archivo"]), "nota": f["nota"]} for f in elegidas]


def _generar_pdf(e):
    reporte.generar(os.path.join(A.carpeta(e["codigo"]), "reporte.pdf"), e,
                    A.medicos().get(e["usuario"], {}).get("perfil", {}), _fotos_reporte(e),
                    e.get("enlace"))


def _enlace(id_drive):
    pagina = (CFG["drive"].get("pagina_url") or VISOR).strip()
    return f"{pagina}?e={id_drive}"


def procesar(codigo):
    """Todo lo que se hace sin internet: video, QR y PDF. La subida la hace el sincronizador."""
    carp = A.carpeta(codigo)
    e = A.leer_estudio(codigo)

    def paso(texto, **extra):
        e["paso"] = texto
        e.update(extra)
        A.guardar_estudio(e)

    try:
        e.pop("error", None)
        video = os.path.join(carp, "video.mp4")
        if e.get("video_crudo") and not os.path.exists(video):
            paso("Preparando el video…")
            convertir_h264(os.path.join(carp, e["video_crudo"]), video, e.get("fps_real") or 30)

        dueno = e["usuario"]
        usar_drive = CFG["drive"]["habilitado"] and nube.conectado(dueno)
        if usar_drive and not e.get("drive_id"):
            reservado = nube.tomar_id(dueno)  # funciona sin internet
            if reservado:
                e.update(drive_id=reservado, enlace=_enlace(reservado))

        paso("Generando el reporte…")
        _generar_pdf(e)
        if usar_drive:
            e["sync"] = e.get("sync") if e.get("sync") == "subido" else "pendiente"
            texto = "Reporte listo. Se subirá a Google Drive en cuanto haya internet."
        else:
            e["sync"] = "sin_drive"
            texto = "Reporte listo (guardado en esta computadora; su Google Drive no está conectado)."
        paso(texto, estado="listo", reporte=True)
        avisar_sincronizador()
    except Exception as ex:
        paso("Error", estado="error", error=str(ex)[:500])
    finally:
        with _lock_trab:
            _trabajos.discard(codigo)


# ---------------- sincronizador: sube todo en cuanto hay internet ----------------
_sync_evento = threading.Event()
_sync = {"en_linea": None, "actual": None, "pct": 0, "ultima": None}


def avisar_sincronizador():
    _sync_evento.set()


def _info_carpeta(e):
    med = A.medicos().get(e["usuario"], {}).get("perfil", {})
    d = CFG["drive"]
    ini = datetime.fromisoformat(e["inicio"])
    vence = ini + timedelta(days=d["dias_acceso"]) if d["dias_acceso"] else None
    return {"clinica": med.get("clinica") or med.get("nombre", ""), "medico": med.get("nombre", ""),
            "especialidad": med.get("especialidad", ""), "telefono": med.get("telefono", ""),
            "procedimiento": e["procedimiento"], "fecha": f"{ini:%d/%m/%Y}", "codigo": e["codigo"],
            "vence": vence.isoformat() if vence else "",
            "vence_texto": f"{vence:%d/%m/%Y}" if vence else ""}


def subir_estudio(codigo):
    e = A.leer_estudio(codigo)
    dueno, carp = e["usuario"], A.carpeta(codigo)
    d = CFG["drive"]

    def guardar(**extra):
        e.update(extra)
        A.guardar_estudio(e)

    _sync.update(actual=codigo, pct=0)
    guardar(sync="subiendo", sync_error=None)
    if not e.get("carpeta_ok"):
        correo = e.get("correo_paciente") if d["compartir"] == "restringido" else None
        r = nube.carpeta_estudio(dueno, codigo, d["carpeta_raiz"], datetime.fromisoformat(e["inicio"]),
                                 d["compartir"], correo, d["dias_acceso"], _info_carpeta(e),
                                 id_fijo=e.get("drive_id"))
        if r["id"] != e.get("drive_id"):
            # Sin ID reservado (o ya no válido): el QR cambia y el reporte se rehace.
            e.update(drive_id=r["id"], enlace=_enlace(r["id"]), qr_actualizado=True)
            _generar_pdf(e)
        guardar(carpeta_ok=True, carpeta_url=r["enlace"], permiso=r["permiso"],
                tipo_permiso=r["tipo_permiso"], dias_acceso=d["dias_acceso"])
    id_c = e["drive_id"]
    nube.subir(dueno, os.path.join(carp, "reporte.pdf"), id_c, f"Reporte_{codigo}.pdf",
               "application/pdf", reemplazar=True)
    fotos = _fotos_reporte(e)
    for i, f in enumerate(fotos, 1):
        _sync["pct"] = int(10 * i / len(fotos))
        nube.subir(dueno, f["ruta"], id_c, f"Foto_{i:02d}.jpg", "image/jpeg")
    video = os.path.join(carp, "video.mp4")
    if os.path.exists(video):
        nube.subir(dueno, video, id_c, f"Video_{codigo}.mp4", "video/mp4",
                   lambda p: _sync.__setitem__("pct", 10 + int(p * 0.9)))
    guardar(sync="subido", sync_error=None, subido=True,
            subido_en=datetime.now().isoformat(timespec="seconds"))
    _sync.update(actual=None, pct=100)


def _revocar_vencidos():
    for e in A.listar_estudios():
        if (e.get("tipo_permiso") == "anyone" and not e.get("acceso_retirado") and e.get("dias_acceso")
                and nube.conectado(e["usuario"])):
            vence = datetime.fromisoformat(e["inicio"]) + timedelta(days=e["dias_acceso"])
            if datetime.now() > vence:
                try:
                    nube.revocar(e["usuario"], e["drive_id"], e["permiso"])
                    e["acceso_retirado"] = True
                    A.guardar_estudio(e)
                except Exception:
                    pass


_ultima_bajada = [0.0]


def _sincronizar_usuarios(usuarios):
    """Sube los cambios de clave/perfil al Drive de cada médico y trae los hechos en otras PCs."""
    raiz = CFG["drive"]["carpeta_raiz"]
    for u in A.pendientes_perfil():
        if nube.conectado(u):
            try:
                d = A.exportar_usuario(u)
                nube.guardar_usuario_remoto(nube.servicio(u), raiz, d)
                A.marcar_perfil_subido(u, d["actualizado"])
            except Exception:
                pass
    if time.time() - _ultima_bajada[0] > 600:
        _ultima_bajada[0] = time.time()
        for u in usuarios:
            info = A.usuario_info(u)
            if info and info["activo"] and info["google"]:
                try:
                    remoto = nube.leer_usuario_remoto(nube.servicio(u))
                    if remoto:
                        A.importar_usuario(remoto, info["google"])
                except Exception:
                    pass


def sincronizador():
    ultima_revision = 0.0
    while True:
        _sync_evento.wait(timeout=30)
        _sync_evento.clear()
        try:
            pendientes = [e for e in A.pendientes_de_subir() if e["estado"] == "listo"
                          and nube.conectado(e["usuario"])]
            usuarios = [u for u in A.medicos() if nube.conectado(u)]
            if not usuarios:
                continue
            _sync["en_linea"] = nube.en_linea()
            if not _sync["en_linea"]:
                continue
            for u in usuarios:  # mantener la reserva de IDs para QR sin internet
                try:
                    nube.reservar_ids(u)
                except Exception:
                    pass
            _sincronizar_usuarios(usuarios)
            for e in sorted(pendientes, key=lambda x: x["codigo"]):
                try:
                    subir_estudio(e["codigo"])
                except Exception as ex:
                    est = A.leer_estudio(e["codigo"])
                    est.update(sync="error", sync_error=str(ex)[:300])
                    A.guardar_estudio(est)
                    _sync["actual"] = None
                    if not nube.en_linea():
                        _sync["en_linea"] = False
                        break
            _sync["ultima"] = datetime.now().isoformat(timespec="seconds")
            if time.time() - ultima_revision > 12 * 3600:
                ultima_revision = time.time()
                _revocar_vencidos()
        except Exception:
            pass


@app.route("/api/sync")
@requiere_login
def api_sync():
    u = session["usuario"]
    pend = [e for e in A.pendientes_de_subir() if e["usuario"] == u]
    return jsonify(en_linea=_sync["en_linea"], pendientes=len(pend), actual=_sync["actual"],
                   pct=_sync["pct"], conectado=nube.conectado(u),
                   errores=sum(1 for e in pend if e.get("sync") == "error"))


@app.route("/api/sync/ahora", methods=["POST"])
@requiere_login
def api_sync_ahora():
    avisar_sincronizador()
    return jsonify(ok=True)


@app.route("/estudio/<codigo>")
@requiere_login
def estudio(codigo):
    return render_template("estudio.html", e=mi_estudio(codigo))


def abrir_con_sistema(ruta):
    """Abre un archivo o carpeta con el programa predeterminado de Windows/Mac/Linux."""
    if platform.system() == "Windows":
        os.startfile(ruta)  # noqa
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", ruta])
    else:
        subprocess.Popen(["xdg-open", ruta])


@app.route("/api/abrir/<codigo>", methods=["POST"])
@requiere_login
def api_abrir(codigo):
    """Abre el PDF en el visor del sistema (para imprimir) o la carpeta del estudio."""
    mi_estudio(codigo)
    que = request.args.get("que", "pdf")
    ruta = os.path.join(A.carpeta(codigo), "reporte.pdf") if que == "pdf" else A.carpeta(codigo)
    if not os.path.exists(ruta):
        abort(404)
    abrir_con_sistema(ruta)
    return jsonify(ok=True)


# ---------------- Google Drive ----------------


@app.route("/drive")
@requiere_login
def drive():
    u = session["usuario"]
    pend = [e for e in A.pendientes_de_subir() if e["usuario"] == u]
    return render_template("drive.html", disponible=nube.disponible(), conectado=nube.conectado(u),
                           cuenta=nube.cuenta(u), carpeta_datos=A.BASE, pendientes=len(pend),
                           reserva=nube.ids_reservados(u) if nube.conectado(u) else 0)


@app.route("/drive/desconectar", methods=["POST"])
@requiere_login
def drive_desconectar():
    u = session["usuario"]
    nube.desconectar(u)
    A.vincular_google(u, None)
    return redirect(url_for("drive"))


# ---------------- actualizaciones ----------------
@app.route("/api/actualizacion")
@requiere_login
def api_actualizacion():
    return jsonify(actualizador.estado())


@app.route("/api/actualizacion/buscar", methods=["POST"])
@requiere_login
def api_actualizacion_buscar():
    actualizador.buscar(forzar=True)
    return jsonify(actualizador.estado())


@app.route("/api/actualizacion/instalar", methods=["POST"])
@requiere_login
def api_actualizacion_instalar():
    if any(e["estado"] == "capturando" for e in A.listar_estudios()) or (
            cam is not None and cam.estado()["grabando"]):
        return jsonify(error="Termine o cancele el estudio en curso antes de actualizar."), 409
    actualizador.instalar(antes_de_salir=cerrar_ordenado)
    return jsonify(actualizador.estado())


def cerrar_ordenado():
    try:
        if cam is not None:
            cam.detener()
            cam.cerrar()
    except Exception:
        pass


@app.route("/datos/abrir", methods=["POST"])
@requiere_login
def datos_abrir():
    abrir_con_sistema(A.BASE)
    return jsonify(ok=True)


@app.route("/api/trabajo/<codigo>")
@requiere_login
def api_trabajo(codigo):
    e = mi_estudio(codigo)
    r = {k: e.get(k) for k in ("estado", "paso", "enlace", "carpeta_url", "reporte", "subido",
                               "error", "sync", "sync_error", "qr_actualizado")}
    r["pct"] = _sync["pct"] if _sync["actual"] == codigo else None
    r["en_linea"] = _sync["en_linea"]
    return jsonify(r)


@app.route("/api/reintentar/<codigo>", methods=["POST"])
@requiere_login
def api_reintentar(codigo):
    e = mi_estudio(codigo)
    if e["estado"] == "error":
        e["estado"] = "procesando"
        A.guardar_estudio(e)
        lanzar(codigo)
    elif e.get("sync") in ("error", "pendiente"):
        avisar_sincronizador()
    return jsonify(ok=True)


@app.route("/archivo/<codigo>/<path:ruta>")
@requiere_login
def archivo(codigo, ruta):
    mi_estudio(codigo)
    return send_from_directory(A.carpeta(codigo), ruta)


# ---------------- mantenimiento al arrancar ----------------
def mantenimiento():
    time.sleep(2)
    threading.Thread(target=actualizador.buscar, daemon=True).start()
    admins = [u for u in A.medicos() if A.es_admin(u)]
    nube.migrar_token_global(admins[0] if admins else None)
    for e in A.listar_estudios():
        if e["estado"] in ("procesando", "error"):  # interrumpidos o de versiones anteriores
            lanzar(e["codigo"])
        elif e["estado"] == "listo" and "sync" not in e:  # estudios de la versión 1.1
            if e.get("subido"):
                e["sync"] = "subido"
            else:
                e["sync"] = "pendiente" if nube.conectado(e["usuario"]) else "sin_drive"
            if e.get("drive_id"):
                e["carpeta_ok"] = True
            A.guardar_estudio(e)
    threading.Thread(target=sincronizador, daemon=True).start()
    avisar_sincronizador()


def puerto_libre(puerto):
    import socket
    with socket.socket() as so:
        return so.connect_ex(("127.0.0.1", puerto)) != 0


def arrancar_servidor():
    """Arranca el servidor en segundo plano. Devuelve la URL."""
    puerto = CFG["puerto"]
    url = f"http://127.0.0.1:{puerto}"
    if puerto_libre(puerto):
        threading.Thread(target=mantenimiento, daemon=True).start()
        threading.Thread(target=lambda: app.run(host="127.0.0.1", port=puerto, threaded=True,
                                                use_reloader=False),
                         daemon=True).start()
        for _ in range(50):
            if not puerto_libre(puerto):
                break
            time.sleep(0.1)
    return url


if __name__ == "__main__":
    url = arrancar_servidor()
    print(f"EndoView en {url}  (Ctrl+C para cerrar)")
    if not os.environ.get("ENDO_SIN_NAVEGADOR"):
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
