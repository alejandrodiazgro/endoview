# Configuración de PyInstaller para generar EndoView.exe (carpeta dist\EndoView)
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [("templates", "templates"), ("cliente.dat", "."), ("icono.ico", ".")]
binaries, hiddenimports = [], []
for paquete in ("webview", "imageio_ffmpeg", "reportlab"):
    d, b, h = collect_all(paquete)
    datas += d; binaries += b; hiddenimports += h
datas += collect_data_files("googleapiclient")  # documentos de la API de Drive
hiddenimports += ["googleapiclient.discovery", "google_auth_oauthlib.flow", "clr", "actualizador", "version"]

a = Analysis(["escritorio.py"], pathex=["."], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, excludes=["tkinter", "matplotlib", "IPython"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="EndoView",
          console=False, icon="icono.ico", version=None)
coll = COLLECT(exe, a.binaries, a.datas, name="EndoView")
