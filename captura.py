"""Captura de video desde la tarjeta de captura (o modo demo sin cámara)."""
import os
import platform
import re
import shutil
import subprocess
import threading
import time

import cv2
import numpy as np


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
        self._archivo_crudo = None
        self._cuadros = 0
        self._t_grabado = 0.0
        self._t_inicio_tramo = None
        self._tam_writer = None
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
                    self._cuadros += 1
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

    # ---------- fotos ----------
    def foto(self, ruta):
        self.esperar_cuadro()
        f = self.cuadro()
        if f is None:
            return False
        return cv2.imwrite(ruta, f, [cv2.IMWRITE_JPEG_QUALITY, 95])

    # ---------- grabación (un solo archivo, con pausas) ----------
    def iniciar_grabacion(self, carpeta):
        self.esperar_cuadro()
        with self._lock:
            grabando = self._grabando
        return True if grabando else self.alternar_grabacion(carpeta)

    def detener_grabacion(self):
        """Detiene (pausa) la grabación. Si se vuelve a iniciar, continúa en el mismo video."""
        with self._lock:
            if self._grabando:
                self._t_grabado += time.time() - self._t_inicio_tramo
                self._grabando = False
            return False

    def alternar_grabacion(self, carpeta):
        self.esperar_cuadro()
        with self._lock:
            if self._writer is None:
                f = self._frame
                if f is None:
                    return False
                h, w = f.shape[:2]
                usar_mjpg = ruta_ffmpeg() is not None
                ext, cc = (".avi", "MJPG") if usar_mjpg else (".mp4", "mp4v")
                self._archivo_crudo = os.path.join(carpeta, "video_crudo" + ext)
                self._writer = cv2.VideoWriter(
                    self._archivo_crudo, cv2.VideoWriter_fourcc(*cc), self.fps, (w, h))
                self._tam_writer = (w, h)
                self._cuadros = 0
                self._t_grabado = 0.0
            if self._grabando:  # pausar
                self._t_grabado += time.time() - self._t_inicio_tramo
                self._grabando = False
            else:  # grabar / reanudar
                self._t_inicio_tramo = time.time()
                self._grabando = True
            return self._grabando

    def estado(self):
        with self._lock:
            seg = self._t_grabado + (
                time.time() - self._t_inicio_tramo if self._grabando else 0)
            return {"grabando": self._grabando, "segundos": round(seg),
                    "hay_video": self._writer is not None, "demo": self.demo,
                    "camara": self.indice}

    def detener(self):
        """Cierra el archivo. Devuelve (ruta_cruda, fps_real) o (None, None)."""
        with self._lock:
            if self._writer is None:
                return None, None
            if self._grabando:
                self._t_grabado += time.time() - self._t_inicio_tramo
            self._grabando = False
            self._writer.release()
            self._writer = None
            fps_real = self._cuadros / self._t_grabado if self._t_grabado > 0 else self.fps
            return self._archivo_crudo, fps_real

    def cerrar(self):
        self._activo = False
        if self.cap:
            self.cap.release()


def _backend():
    so = platform.system()
    return cv2.CAP_DSHOW if so == "Windows" else (
        cv2.CAP_AVFOUNDATION if so == "Darwin" else cv2.CAP_ANY)


def abrir_dispositivo(indice, res=(1920, 1080), fps=30):
    if indice is None:
        return None
    cap = cv2.VideoCapture(indice, _backend())
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, res[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, res[1])
        cap.set(cv2.CAP_PROP_FPS, fps)
        ok, _ = cap.read()
        if ok:
            return cap
    cap.release()
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
