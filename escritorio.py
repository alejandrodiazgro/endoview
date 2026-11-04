"""Punto de entrada del .exe: abre EndoView en su propia ventana de Windows.

Si la ventana integrada no está disponible (falta WebView2), abre el navegador.
"""
import os
import sys
import time
import webbrowser


def main():
    import app  # arranca configuración y datos
    url = app.arrancar_servidor()
    try:
        import webview
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.settings["ALLOW_DOWNLOADS"] = True
        ventana = webview.create_window(
            "EndoView", url, width=1400, height=900, min_size=(1000, 700),
            maximized=True, text_select=True)

        def al_cerrar():
            # Evita cerrar a media grabación sin aviso
            try:
                if app.cam is not None and app.cam.estado()["grabando"]:
                    return ventana.create_confirmation_dialog(
                        "EndoView", "Se está grabando. ¿Cerrar de todos modos?")
            except Exception:
                pass
            return True
        ventana.events.closing += al_cerrar
        webview.start(private_mode=False,
                      storage_path=os.path.join(app.A.BASE, "ventana"))
    except Exception as ex:  # sin WebView2: usar el navegador
        print("Ventana integrada no disponible:", ex)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    # Cierra la grabación de forma ordenada
    try:
        if app.cam is not None:
            app.cam.detener()
            app.cam.cerrar()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    if getattr(sys, "frozen", False) and sys.stdout is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")
    main()
