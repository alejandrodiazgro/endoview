"""EndoView: captura de procedimientos endoscópicos, subida a Drive y reporte con QR.

Ejecutar:  python app.py   (se abre el navegador en http://127.0.0.1:5050)
"""
import os
import platform
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
        if "usuario" not in session or not A.perfil(session["usuario"]):
            session.clear()
            return redirect(url_for("login"))
        return f(*a, **k)
    return envoltura


def mi_estudio(codigo):
    e = A.leer_estudio(codigo)
    if not e or e.get("usuario") != session.get("usuario"):
        abort(404)
    return e


@app.context_processor
def globales():
    u = session.get("usuario")
    return {"medico": A.perfil(u) if u else None, "usuario": u,
            "procedimientos": A.PROCEDIMIENTOS, "cfg": CFG,
            "drive_listo": nube.conectado(), "version": VERSION}


@app.route("/login", methods=["GET", "POST"])
def login():
    if not A.hay_medicos():
        return redirect(url_for("registro"))
    if request.method == "POST":
        u = request.form.get("usuario", "")
        if A.verificar(u, request.form.get("clave", "")):
            session.clear()
            session.permanent = True
            session["usuario"] = u.strip().lower()
            return redirect(url_for("inicio"))
        flash("Usuario o clave incorrectos.")
    return render_template("login.html")


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("login"))


@app.route("/registro", methods=["GET", "POST"])
def registro():
    primero = not A.hay_medicos()
    if not primero and "usuario" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        try:
            if len(f.get("clave", "")) < 6:
                raise ValueError("La clave debe tener al menos 6 caracteres.")
            if f.get("clave") != f.get("clave2"):
                raise ValueError("Las claves no coinciden.")
            if not f.get("usuario", "").strip() or not f.get("nombre", "").strip():
                raise ValueError("Usuario y nombre son obligatorios.")
            A.crear_medico(f["usuario"], f["clave"], f)
        except ValueError as e:
            flash(str(e))
            return render_template("perfil.html", modo="registro", primero=primero, datos=f)
        if primero:
            session["usuario"] = f["usuario"].strip().lower()
            session.permanent = True
            return redirect(url_for("inicio"))
        flash("Médico agregado.")
        return redirect(url_for("inicio"))
    return render_template("perfil.html", modo="registro", primero=primero, datos={})


@app.route("/perfil", methods=["GET", "POST"])
@requiere_login
def perfil():
    u = session["usuario"]
    if request.method == "POST":
        f = request.form
        nueva = f.get("clave", "")
        if nueva and (len(nueva) < 6 or nueva != f.get("clave2")):
            flash("La clave nueva debe tener 6+ caracteres y coincidir.")
        else:
            A.actualizar_perfil(u, f, nueva or None)
            flash("Perfil guardado.")
            return redirect(url_for("inicio"))
    return render_template("perfil.html", modo="perfil", datos=A.perfil(u))


# ---------------- inicio / iniciar procedimiento ----------------
@app.route("/")
@requiere_login
def inicio():
    activo = session.get("estudio")
    if activo and A.leer_estudio(activo) and A.leer_estudio(activo)["estado"] == "capturando":
        return redirect(url_for("captura"))
    return render_template("inicio.html", estudios=A.listar_estudios(session["usuario"])[:30])


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


# ---------------- procesamiento en segundo plano ----------------
def lanzar(codigo):
    with _lock_trab:
        if codigo in _trabajos:
            return
        _trabajos.add(codigo)
    threading.Thread(target=procesar, args=(codigo,), daemon=True).start()


def procesar(codigo):
    carp = A.carpeta(codigo)
    e = A.leer_estudio(codigo)

    def paso(texto, **extra):
        e["paso"] = texto
        e.update(extra)
        A.guardar_estudio(e)

    try:
        e.pop("error", None)
        video = os.path.join(carp, "video.mp4")
        # 1. Video final H.264
        if e.get("video_crudo") and not os.path.exists(video):
            paso("Preparando el video…")
            convertir_h264(os.path.join(carp, e["video_crudo"]), video, e.get("fps_real") or 30)
        hay_video = os.path.exists(video)

        # 2. Carpeta del estudio en Drive (su enlace va en el QR)
        usar_drive = CFG["drive"]["habilitado"] and nube.conectado()
        if usar_drive and not e.get("enlace"):
            paso("Conectando con Google Drive…")
            d = CFG["drive"]
            correo = e.get("correo_paciente") if d["compartir"] == "restringido" else None
            med = A.perfil(e["usuario"]) or {}
            ini = datetime.fromisoformat(e["inicio"])
            vence = ini + timedelta(days=d["dias_acceso"]) if d["dias_acceso"] else None
            info = {"clinica": med.get("clinica") or med.get("nombre", ""),
                    "medico": med.get("nombre", ""), "especialidad": med.get("especialidad", ""),
                    "telefono": med.get("telefono", ""), "procedimiento": e["procedimiento"],
                    "fecha": f"{ini:%d/%m/%Y}", "codigo": codigo,
                    "vence": vence.isoformat() if vence else "",
                    "vence_texto": f"{vence:%d/%m/%Y}" if vence else ""}
            r = nube.carpeta_estudio(codigo, d["carpeta_raiz"], d["compartir"], correo,
                                     d["dias_acceso"], info)
            pagina = (d.get("pagina_url") or VISOR).strip()
            enlace_qr = f"{pagina}?e={r['id']}" if pagina else r["enlace"]
            paso("Carpeta creada", enlace=enlace_qr, carpeta_url=r["enlace"], drive_id=r["id"],
                 permiso=r["permiso"], tipo_permiso=r["tipo_permiso"],
                 dias_acceso=d["dias_acceso"])

        # 3. Reporte PDF
        paso("Generando el reporte…")
        elegidas = [f for f in sorted((f for f in e["fotos"] if f.get("orden")),
                                      key=lambda f: f["orden"])
                    if os.path.exists(os.path.join(carp, "fotos", f["archivo"]))][:8]
        fotos = [{"ruta": os.path.join(carp, "fotos", f["archivo"]), "nota": f["nota"]}
                 for f in elegidas]
        pdf = os.path.join(carp, "reporte.pdf")
        reporte.generar(pdf, e, A.perfil(e["usuario"]) or {}, fotos, e.get("enlace"))
        paso("Reporte listo", reporte=True)

        # 4. Subir reporte, fotos y video a la carpeta del estudio
        if usar_drive and not e.get("subido"):
            id_c = e["drive_id"]
            paso("Subiendo el reporte…", subida_pct=0)
            nube.subir(pdf, id_c, f"Reporte_{codigo}.pdf", "application/pdf")
            for i, f in enumerate(elegidas, 1):
                paso(f"Subiendo fotos {i} de {len(elegidas)}…",
                     subida_pct=int(10 * i / max(len(elegidas), 1)))
                nube.subir(fotos[i - 1]["ruta"], id_c, f"Foto_{i:02d}.jpg", "image/jpeg")
            if hay_video:
                ultimo = [-1]

                def prog(p):
                    if p != ultimo[0]:
                        ultimo[0] = p
                        paso(f"Subiendo el video… {p}%", subida_pct=10 + int(p * 0.9))
                nube.subir(video, id_c, f"Video_{codigo}.mp4", "video/mp4", prog)
            e["subido"] = True

        if usar_drive:
            final = "Listo: reporte, fotos" + (" y video" if hay_video else "") + " guardados en Drive"
        else:
            final = "Listo (guardado solo en esta computadora; Drive no está conectado)"
        paso(final, estado="listo", subida_pct=100 if usar_drive else None)
    except Exception as ex:  # se puede reintentar
        paso("Error", estado="error", error=str(ex)[:500])
    finally:
        with _lock_trab:
            _trabajos.discard(codigo)


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
_drive_estado = {"conectando": False, "error": None}


@app.route("/drive")
@requiere_login
def drive():
    return render_template("drive.html", disponible=nube.disponible(),
                           conectado=nube.conectado(), cuenta=nube.cuenta(),
                           estado=_drive_estado, carpeta_datos=A.BASE)


@app.route("/drive/conectar", methods=["POST"])
@requiere_login
def drive_conectar():
    if not _drive_estado["conectando"]:
        def tarea():
            _drive_estado.update(conectando=True, error=None)
            try:
                nube._servicio = None
                nube.servicio()  # abre el navegador para autorizar
            except Exception as ex:
                _drive_estado["error"] = str(ex)[:300]
            finally:
                _drive_estado["conectando"] = False
        threading.Thread(target=tarea, daemon=True).start()
    return jsonify(ok=True)


@app.route("/drive/estado")
@requiere_login
def drive_estado():
    return jsonify(conectado=nube.conectado(), **_drive_estado)


@app.route("/drive/desconectar", methods=["POST"])
@requiere_login
def drive_desconectar():
    nube.desconectar()
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
    return jsonify({k: e.get(k) for k in ("estado", "paso", "subida_pct", "enlace", "carpeta_url",
                                           "reporte", "subido", "error")})


@app.route("/api/reintentar/<codigo>", methods=["POST"])
@requiere_login
def api_reintentar(codigo):
    e = mi_estudio(codigo)
    if e["estado"] != "capturando":
        e["estado"] = "procesando"
        A.guardar_estudio(e)
        lanzar(codigo)
    return jsonify(ok=True)


@app.route("/archivo/<codigo>/<path:ruta>")
@requiere_login
def archivo(codigo, ruta):
    mi_estudio(codigo)
    return send_from_directory(A.carpeta(codigo), ruta)


# ---------------- mantenimiento al arrancar ----------------
def mantenimiento():
    time.sleep(3)
    threading.Thread(target=actualizador.buscar, daemon=True).start()
    for e in A.listar_estudios():
        # Retomar trabajos interrumpidos (p. ej. se apagó la computadora)
        if e["estado"] == "procesando":
            lanzar(e["codigo"])
        # Retirar acceso público a videos vencidos
        if (e.get("tipo_permiso") == "anyone" and not e.get("acceso_retirado")
                and e.get("dias_acceso") and nube.conectado()):
            vence = datetime.fromisoformat(e["inicio"]) + timedelta(days=e["dias_acceso"])
            if datetime.now() > vence:
                try:
                    nube.revocar(e["drive_id"], e["permiso"])
                    e["acceso_retirado"] = True
                    A.guardar_estudio(e)
                except Exception:
                    pass


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
