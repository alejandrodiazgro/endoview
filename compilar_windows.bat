@echo off
REM Genera el instalador EndoView-Setup.exe. Ejecutar en Windows 10/11 con Python 3.11+ instalado.
cd /d "%~dp0"
echo === 1/3 Preparando entorno ===
if not exist .venv-build ( python -m venv .venv-build || (echo Falta Python: instalelo desde python.org & pause & exit /b 1) )
call .venv-build\Scripts\activate
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || (echo Error instalando dependencias & pause & exit /b 1)

echo === 2/3 Creando EndoView.exe ===
pyinstaller --noconfirm --clean EndoView.spec || (echo Error de PyInstaller & pause & exit /b 1)

echo === 3/3 Creando instalador ===
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
  echo Instalando Inno Setup...
  winget install -e --id JRSoftware.InnoSetup --accept-source-agreements --accept-package-agreements
)
if exist %ISCC% (
  %ISCC% instalador.iss && echo. && echo LISTO: el instalador esta en la carpeta "salida".
) else (
  echo No se encontro Inno Setup. El programa ya funciona en dist\EndoView\EndoView.exe
)
explorer salida 2>nul
pause
