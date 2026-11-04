# EndoView

Captura de procedimientos endoscópicos para Windows: imagen en vivo de la capturadora, fotos,
grabación, reporte en PDF con QR y respaldo en Google Drive. Se actualiza solo.

## Instalar

Descargue **EndoView-Setup.exe** de
[Releases](https://github.com/alejandrodiazgro/endoview/releases/latest) y ábralo
(Siguiente → Instalar). No pide permisos de administrador ni necesita Python.

## Usar

1. **Perfil del médico**: usuario, clave y datos del reporte (la primera vez).
2. **Iniciar procedimiento**: elija la *Fuente de video* (capturadora), nombre, edad y procedimiento.
3. **Captura**: *Iniciar grabación*, *Tomar foto*, *Detener grabación*
   (teclas: foto F8/Espacio, grabar F9 — sirven para pedal USB).
4. **Revisión de fotos**: hasta 8 fotos (2 × 4) y el reporte escrito.
5. **Finalizar**: sale el PDF y se suben a Drive el reporte, las fotos y el video.

**Google Drive (por médico)**: cada médico entra a *Mi Google Drive* → **Iniciar sesión con Google**.
Sus estudios se guardan en *su* Drive, y con esa cuenta también puede entrar a EndoView y recuperar su clave.
El QR del reporte abre la página del paciente (*Ver video · Ver fotos · Ver reporte*), disponible 90 días.

**Sin internet**: todo funciona. El reporte sale al instante con su QR (EndoView reserva por adelantado
los lugares en Drive) y el reporte, las fotos y el video se suben solos en cuanto regresa la conexión.

**Usuarios**: el primer usuario es administrador (*Usuarios*): agrega médicos, restablece claves con una
clave temporal, activa/desactiva y da permisos de administrador. Cada médico recibe un **código de
recuperación** para cambiar su clave sin internet. Usuarios y claves (cifradas) se guardan solo en
la computadora.

## Actualizaciones

EndoView revisa al abrirse si hay versión nueva y muestra **Actualizar ahora**: se descarga,
se verifica, se instala y se vuelve a abrir solo. Sus estudios, usuarios y la conexión con Drive
están en `%LOCALAPPDATA%\EndoView` y no se tocan.

## Para publicar una versión nueva (desarrollo)

Cambie `VERSION` en `version.py` y suba el cambio a `main`. GitHub Actions compila el
instalador en Windows, lo prueba y publica el Release con su `SHA256SUMS.txt`.

Estructura: `app.py` (servidor y flujo) · `captura.py` (video) · `nube.py` (Drive) ·
`reporte.py` (PDF) · `almacen.py` (datos) · `actualizador.py` · `escritorio.py` (ventana) ·
`docs/` (sitio y página del paciente en GitHub Pages) · `EndoView.spec` + `instalador.iss`.
