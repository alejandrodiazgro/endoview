"""Captura de video desde la tarjeta de captura (o modo demo sin cámara)."""
import os
import platform
import re
import shutil
import subprocess
import threading
import time

import json
import logging

import cv2
import numpy as np

log = logging.getLogger("endoview.captura")


def ruta_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


class Capturador:
    def __init__(self, indice=0, resolucion=(1920, 1080), fps=30):
        self.indice = indice
        self.res = tuple(resolucion)
        self.fps = fps
        self.demo = False
        self._frame = None
        self._lock = threading.Lock()
        self._lock_cap = threading.Lock()
        self.cap = None
        self._activo = True
        # grabación
        self._writer = None
        self._grabando = False
        self._tam_writer = None
        self._carpeta = None
        self._t_previo = 0.0
        self._seg_inicio = 0.0
        self._seg_cuadros = 0
        self._seg_ruta = None
        self.alerta = None
        self._abrir()
        threading.Thread(target=self._bucle, daemon=True).start()

    # ---------- dispositivo ----------
    def _abrir(self):
        cap = abrir_dispositivo(self.indice, self.res, self.fps)
        self.cap = cap
        self.demo = cap is None

    def cambiar(self, indice):
        """Cambia de dispositivo (p. ej. de la webcam a la capturadora).
        Si el nuevo no da señal, regresa al anterior. Devuelve True si se cambió."""
        with self._lock_cap:
            anterior = None if self.demo else self.indice
            if self.cap is not None:
                self.cap.release()
            cap = abrir_dispositivo(indice, self.res, self.fps)
            if cap is None:
                self.cap = abrir_dispositivo(anterior, self.res, self.fps) if anterior is not None else None
                self.demo = self.cap is None
                return False
            self.cap, self.indice, self.demo = cap, indice, False
            return True

    def _cuadro_demo(self, t):
        w, h = 1280, 720
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = w / 2 + 120 * np.sin(t * 0.7), h / 2 + 80 * np.cos(t * 0.5)
        r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        pliegues = 0.5 + 0.5 * np.sin(r / 22 - t * 2)
        luz = np.clip(1.2 - r / 520, 0.15, 1)
        img = np.zeros((h, w, 3), np.uint8)
        img[..., 2] = np.clip(200 * luz * (0.7 + 0.3 * pliegues), 0, 255)
        img[..., 1] = np.clip(95 * luz * (0.6 + 0.4 * pliegues), 0, 255)
        img[..., 0] = np.clip(85 * luz, 0, 255)
        mascara = np.zeros((h, w), np.uint8)
        cv2.circle(mascara, (w // 2, h // 2), 340, 255, -1)
        img[mascara == 0] = 0
        cv2.putText(img, "MODO DEMO - sin camara", (30, h - 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        return img

    def _bucle(self):
        t0 = time.time()
        periodo = 1.0 / self.fps
        while self._activo:
            ini = time.time()
            with self._lock_cap:
                demo = self.demo
                if demo:
                    frame = self._cuadro_demo(ini - t0)
                else:
                    ok, frame = self.cap.read()
            if not demo and not ok:
                time.sleep(0.05)
                continue
            with self._lock:
                self._frame = frame
                if self._grabando and self._writer is not None:
                    if (frame.shape[1], frame.shape[0]) != self._tam_writer:
                        frame = cv2.resize(frame, self._tam_writer)
                    self._writer.write(frame)
                    self._seg_cuadros += 1
                    self._al_escribir()
            if demo:
                time.sleep(max(0, periodo - (time.time() - ini)))

    def esperar_cuadro(self, segundos=3):
        """Espera a que llegue la primera imagen (la capturadora tarda en arrancar)."""
        fin = time.time() + segundos
        while time.time() < fin:
            with self._lock:
                if self._frame is not None:
                    return True
            time.sleep(0.05)
        return False

    def cuadro(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def jpeg(self, calidad=80, ancho_max=1280):
        f = self.cuadro()
        if f is None:
            return None
        if f.shape[1] > ancho_max:
            esc = ancho_max / f.shape[1]
            f = cv2.resize(f, None, fx=esc, fy=esc)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, calidad])
        return buf.tobytes() if ok else None

    # ---------- fotos (se escriben, se sincronizan al disco y se vuelven a leer) ----------
    def foto(self, ruta):
        """Guarda la foto y comprueba que quedó bien en el disco. Devuelve True/False."""
        self.esperar_cuadro()
        f = self.cuadro()
        if f is None:
            return False
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            return False
        try:
            escribir_seguro(ruta, buf.tobytes())
            return verificar_imagen(ruta, f.shape)
        except OSError:
            log.exception("No se pudo guardar la foto %s", ruta)
            return False

    # ---------- grabación en segmentos de 30 s ----------
    # Cada segmento es un archivo independiente que se cierra y se sincroniza al disco: si se
    # va la luz o se cierra el programa, se pierden como máximo los últimos segundos. Nunca se
    # sobrescribe un segmento existente.
    SEGMENTO_S = 30
    MIN_LIBRE_GB = 1.0

    def _preparar_estudio(self, carpeta):
        if carpeta != getattr(self, "_carpeta", None):
            self._carpeta = carpeta
            self._t_previo = sum(p.get("segundos", 0) for p in leer_manifiesto(carpeta))
            self.alerta = None

    def _abrir_segmento(self):
        f = self._frame
        h, w = f.shape[:2]
        d = os.path.join(self._carpeta, DIR_PARTES)
        os.makedirs(d, exist_ok=True)
        n = siguiente_parte(d)
        ruta = os.path.join(d, f"parte_{n:04d}.avi")
        open(ruta, "wb").close()  # existe antes de abrirlo (permite ruta corta en Windows)
        wr = cv2.VideoWriter(ruta_para_opencv(ruta), cv2.VideoWriter_fourcc(*"MJPG"), self.fps, (w, h))
        if not wr.isOpened():
            log.error("No se pudo abrir el archivo de video %s", ruta)
            self.alerta = "No se pudo crear el archivo de video. NO se está grabando."
            return False
        self._writer, self._seg_ruta, self._tam_writer = wr, ruta, (w, h)
        self._seg_inicio, self._seg_cuadros = time.time(), 0
        return True

    def _cerrar_segmento(self):
        if self._writer is None:
            return
        self._writer.release()
        self._writer = None
        dur = time.time() - self._seg_inicio
        try:
            sincronizar_disco(self._seg_ruta)
            tam = os.path.getsize(self._seg_ruta)
        except OSError:
            tam = 0
        if tam == 0 or self._seg_cuadros == 0:
            log.error("Segmento vacío: %s", self._seg_ruta)
            if self._seg_cuadros:
                self.alerta = "Un segmento de video no se guardó correctamente."
        agregar_al_manifiesto(self._carpeta, {
            "archivo": os.path.basename(self._seg_ruta), "cuadros": self._seg_cuadros,
            "segundos": round(dur, 3), "fps": round(self._seg_cuadros / dur, 3) if dur > 0 else self.fps,
            "bytes": tam})
        self._t_previo += dur

    def iniciar_grabacion(self, carpeta):
        self.esperar_cuadro()
        with self._lock:
            if self._grabando and carpeta != self._carpeta:  # otro estudio: cerrar el anterior a salvo
                self._cerrar_segmento()
                self._grabando = False
            self._preparar_estudio(carpeta)
            if self._grabando:
                return True
            if self._frame is None:
                self.alerta = "No hay señal de video."
                return False
            if espacio_libre_gb(carpeta) < self.MIN_LIBRE_GB:
                self.alerta = "No hay espacio en el disco para grabar."
                return False
            self.alerta = None
            self._grabando = self._abrir_segmento()
            return self._grabando

    def detener_grabacion(self):
        with self._lock:
            if self._grabando:
                self._cerrar_segmento()
                self._grabando = False
            return False

    def alternar_grabacion(self, carpeta):
        with self._lock:
            grabando = self._grabando
        return self.detener_grabacion() if grabando else self.iniciar_grabacion(carpeta)

    def _al_escribir(self):
        """Se llama con el candado tomado después de escribir cada cuadro."""
        ahora = time.time()
        if ahora - self._seg_inicio >= self.SEGMENTO_S:
            self._cerrar_segmento()
            if espacio_libre_gb(self._carpeta) < self.MIN_LIBRE_GB:
                self._grabando = False
                self.alerta = "Se detuvo la grabación porque el disco está casi lleno. Lo grabado está a salvo."
                log.error("Grabación detenida por falta de espacio")
                return
            if not self._abrir_segmento():
                self._grabando = False

    def estado(self):
        with self._lock:
            carpeta = getattr(self, "_carpeta", None)
            previo = getattr(self, "_t_previo", 0.0)
            seg = previo + (time.time() - self._seg_inicio if self._grabando and self._writer else 0)
            return {"grabando": self._grabando, "segundos": round(seg),
                    "hay_video": seg > 0 or self._grabando, "demo": self.demo,
                    "camara": self.indice, "alerta": getattr(self, "alerta", None),
                    "libre_gb": round(espacio_libre_gb(carpeta), 1) if carpeta else None}

    def detener(self):
        """Termina la grabación del estudio actual (cierra el segmento abierto)."""
        with self._lock:
            if self._grabando:
                self._cerrar_segmento()
            self._grabando = False
            carpeta = getattr(self, "_carpeta", None)
            self._carpeta, self._t_previo, self.alerta = None, 0.0, None
            return carpeta

    def cerrar(self):
        self._activo = False
        if self.cap:
            self.cap.release()


def _backend():
    so = platform.system()
    return cv2.CAP_DSHOW if so == "Windows" else (
        cv2.CAP_AVFOUNDATION if so == "Darwin" else cv2.CAP_ANY)


def _backends():
    so = platform.system()
    if so == "Windows":
        return [cv2.CAP_DSHOW, cv2.CAP_MSMF]  # si DirectShow falla, Media Foundation
    return [_backend()]


def abrir_dispositivo(indice, res=(1920, 1080), fps=30):
    if indice is None:
        return None
    for be in _backends():
        try:
            cap = cv2.VideoCapture(indice, be)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, res[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, res[1])
                cap.set(cv2.CAP_PROP_FPS, fps)
                ok, _ = cap.read()
                if not ok:  # algunas webcams no aceptan la resolución pedida
                    cap.release()
                    cap = cv2.VideoCapture(indice, be)
                    ok, _ = cap.read() if cap.isOpened() else (False, None)
                if ok:
                    log.info("Dispositivo %s abierto con backend %s", indice, be)
                    return cap
            cap.release()
        except Exception:
            log.exception("Error al abrir el dispositivo %s", indice)
    log.warning("El dispositivo %s no dio imagen", indice)
    return None


def _sin_ventana():
    """Evita que se abra una ventana negra de consola en Windows al llamar a ffmpeg."""
    if platform.system() == "Windows":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _salida_ffmpeg(args):
    ff = ruta_ffmpeg()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-hide_banner"] + args, capture_output=True, timeout=10,
                           encoding="utf-8", errors="replace", **_sin_ventana())
        return (r.stderr or "") + (r.stdout or "")
    except Exception:
        return None


def leer_dshow(texto):
    """Nombres de cámaras en el orden de DirectShow (el mismo que usa OpenCV en Windows)."""
    nombres, en_video = [], False
    for linea in texto.splitlines():
        if "Alternative name" in linea:
            continue
        if "DirectShow video devices" in linea:
            en_video = True
            continue
        if "DirectShow audio devices" in linea:
            en_video = False
            continue
        m = re.search(r'"(.+)"\s*\((video|audio|none)', linea)
        if m:  # formato nuevo: "Nombre" (video)
            if m.group(2) == "video":
                nombres.append(m.group(1))
            continue
        m = re.search(r'\]\s+"(.+)"\s*$', linea)
        if m and en_video:  # formato antiguo, por secciones
            nombres.append(m.group(1))
    return nombres


def leer_avfoundation(texto):
    nombres, en_video = {}, False
    for linea in texto.splitlines():
        if "video devices" in linea:
            en_video = True
            continue
        if "audio devices" in linea:
            en_video = False
            continue
        m = re.search(r"\[(\d+)\]\s+(.+)$", linea)
        if m and en_video and "Capture screen" not in m.group(2):
            nombres[int(m.group(1))] = m.group(2).strip()
    return nombres


def _nombres_sistema():
    """{indice: nombre} según el sistema operativo, o None si no se pudo saber."""
    so = platform.system()
    if so == "Windows":
        t = _salida_ffmpeg(["-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        if t:
            lista = leer_dshow(t)
            if lista:
                return dict(enumerate(lista))
        log.warning("ffmpeg no listó dispositivos: %s", (t or "sin salida")[-500:])
    elif so == "Darwin":
        t = _salida_ffmpeg(["-f", "avfoundation", "-list_devices", "true", "-i", ""])
        if t:
            d = leer_avfoundation(t)
            if d:
                return d
    else:
        base = "/sys/class/video4linux"
        if os.path.isdir(base):
            d = {}
            for n in os.listdir(base):
                try:
                    with open(os.path.join(base, n, "index")) as f:
                        if f.read().strip() != "0":
                            continue  # nodos secundarios (metadatos), no dan imagen
                    with open(os.path.join(base, n, "name")) as f:
                        d[int(n.replace("video", ""))] = f.read().strip()
                except (OSError, ValueError):
                    pass
            if d:
                return d
    return None


def listar_dispositivos(actual=None, maximo=8):
    """Dispositivos de imagen conectados: [{indice, nombre}]."""
    nombres = _nombres_sistema()
    if nombres is None:  # sin nombres: probar uno por uno
        nombres = {}
        for i in range(maximo):
            if i == actual:
                nombres[i] = f"Dispositivo {i}"
                continue
            cap = cv2.VideoCapture(i, _backend())
            if cap.isOpened():
                ok, _ = cap.read()
                if ok:
                    nombres[i] = f"Dispositivo {i}"
            cap.release()
    vistos, res = {}, []
    for i in sorted(nombres):
        n = nombres[i]
        vistos[n] = vistos.get(n, 0) + 1
        res.append({"indice": i, "nombre": n if vistos[n] == 1 else f"{n} ({vistos[n]})"})
    return res


# ---------------- utilidades de guardado seguro ----------------
DIR_PARTES = "video_partes"
MANIFIESTO = "segmentos.json"


def sincronizar_disco(ruta):
    """Obliga a Windows a escribir el archivo en el disco (no dejarlo en memoria)."""
    with open(ruta, "rb+") as f:
        os.fsync(f.fileno())


def escribir_seguro(ruta, datos):
    tmp = ruta + ".tmp"
    with open(tmp, "wb") as f:
        f.write(datos)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ruta)


def verificar_imagen(ruta, forma=None):
    try:
        img = cv2.imdecode(np.fromfile(ruta, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return False
    return img is not None and (forma is None or img.shape[:2] == tuple(forma[:2]))


def espacio_libre_gb(ruta):
    try:
        return shutil.disk_usage(ruta).free / 1024 ** 3
    except OSError:
        return 999.0


def ruta_para_opencv(ruta):
    """OpenCV en Windows no abre rutas con acentos o ñ: se usa la ruta corta (8.3)."""
    if platform.system() != "Windows":
        return ruta
    try:
        ruta.encode("ascii")
        return ruta
    except UnicodeEncodeError:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(ruta, buf, 1024):
            return buf.value
        return ruta


def siguiente_parte(d):
    nums = [int(n[6:10]) for n in os.listdir(d) if re.match(r"parte_\d{4}\.avi$", n)]
    return max(nums, default=0) + 1


def leer_manifiesto(carpeta):
    try:
        with open(os.path.join(carpeta, DIR_PARTES, MANIFIESTO), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def agregar_al_manifiesto(carpeta, entrada):
    m = leer_manifiesto(carpeta) + [entrada]
    escribir_seguro(os.path.join(carpeta, DIR_PARTES, MANIFIESTO),
                    json.dumps(m, ensure_ascii=False, indent=1).encode("utf-8"))


def duracion_video(ruta):
    """Duración en segundos según ffmpeg (None si no se puede leer)."""
    t = _salida_ffmpeg(["-i", ruta]) or ""
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def hay_segmentos(carpeta):
    d = os.path.join(carpeta, DIR_PARTES)
    return os.path.isdir(d) and any(n.endswith(".avi") for n in os.listdir(d))


def unir_y_verificar(carpeta, destino, fps_defecto=30):
    """Convierte cada segmento a MP4, los une en un solo video y verifica que la duración final
    coincida con lo grabado. Solo si todo cuadra se borran los segmentos originales.
    Devuelve {ok, duracion, esperado, problemas}."""
    ff = ruta_ffmpeg()
    d = os.path.join(carpeta, DIR_PARTES)
    man = {p["archivo"]: p for p in leer_manifiesto(carpeta)}
    partes = sorted(n for n in os.listdir(d) if re.match(r"parte_\d{4}\.avi$", n))
    tmp = os.path.join(d, "mp4")
    os.makedirs(tmp, exist_ok=True)
    listos, problemas, esperado = [], [], 0.0
    for n in partes:
        info = man.get(n)
        if info and info.get("cuadros", 0) == 0:
            continue
        fps = (info or {}).get("fps") or fps_defecto
        esperado += (info or {}).get("segundos") or 0
        salida = os.path.join(tmp, n.replace(".avi", ".mp4"))
        if not (os.path.exists(salida) and duracion_video(salida)):
            r = subprocess.run([ff, "-y", "-loglevel", "error", "-err_detect", "ignore_err",
                                "-r", f"{fps:.3f}", "-i", os.path.join(d, n), "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", salida],
                               capture_output=True, text=True, **_sin_ventana())
            if r.returncode != 0 or not duracion_video(salida):
                problemas.append(f"El pedazo {int(n[6:10])} del video está dañado y no se pudo leer "
                                 f"(los demás se conservaron).")
                log.error("No se pudo convertir %s: %s", n, r.stderr[-400:])
                continue
        if not info:  # segmento de un cierre inesperado: se usa su duración real
            esperado += duracion_video(salida) or 0
        listos.append(salida)
    if not listos:
        return {"ok": False, "duracion": 0, "esperado": esperado,
                "problemas": problemas or ["No hay segmentos de video que se puedan leer."]}
    lista = os.path.join(tmp, "lista.txt")
    with open(lista, "w", encoding="utf-8") as f:
        for p in listos:
            f.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
    final_tmp = destino + ".tmp.mp4"
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lista,
                        "-c", "copy", "-movflags", "+faststart", final_tmp],
                       capture_output=True, text=True, **_sin_ventana())
    dur = duracion_video(final_tmp) if r.returncode == 0 and os.path.exists(final_tmp) else None
    suma = sum(duracion_video(p) or 0 for p in listos)
    ok = bool(dur) and abs(dur - suma) <= max(1.5, 0.02 * suma) and \
        (esperado == 0 or abs(dur - esperado) <= max(3.0, 0.10 * esperado)) and not problemas
    if not dur:
        problemas.append("No se pudo unir el video.")
        log.error("No se pudo unir el video: %s", r.stderr[-400:])
    elif not ok and not problemas:
        problemas.append(f"La duración no coincide (video {dur:.0f} s, grabado {esperado:.0f} s).")
    if dur:
        sincronizar_disco(final_tmp)
        os.replace(final_tmp, destino)
    if ok:  # todo verificado: ya se pueden borrar los segmentos originales
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": ok, "duracion": round(dur or 0, 1), "esperado": round(esperado, 1), "problemas": problemas}


def convertir_h264(crudo, destino, fps_real):
    """Convierte a MP4 H.264 con la velocidad real medida (se reproduce en Drive y celulares)."""
    ff = ruta_ffmpeg()
    if not ff:
        if crudo != destino:
            os.replace(crudo, destino)
        return destino
    cmd = [ff, "-y", "-loglevel", "error", "-r", f"{fps_real:.3f}", "-i", crudo,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", destino]
    r = subprocess.run(cmd, capture_output=True, text=True, **_sin_ventana())
    if r.returncode == 0 and os.path.exists(destino):
        os.remove(crudo)
        return destino
    raise RuntimeError("No se pudo convertir el video: " + r.stderr[-400:])
