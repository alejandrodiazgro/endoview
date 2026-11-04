; Instalador de EndoView (Inno Setup 6). La versión la pasa la compilación: iscc /DVersion=1.2.0
#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{5E0B7C2A-91D4-4F3B-A8E6-3C7D2B19F4E1}
AppName=EndoView
AppVersion={#Version}
AppVerName=EndoView {#Version}
AppPublisher=EndoView
AppPublisherURL=https://alejandrodiazgro.github.io/endoview/
; Se instala para el usuario (sin pedir permisos de administrador), así las
; actualizaciones automáticas no muestran ventanas de Windows.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\EndoView
DisableDirPage=yes
DefaultGroupName=EndoView
DisableProgramGroupPage=yes
OutputDir=salida
OutputBaseFilename=EndoView-Setup-{#Version}
SetupIconFile=icono.ico
UninstallDisplayIcon={app}\EndoView.exe
UninstallDisplayName=EndoView
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "escritorio"; Description: "Crear ícono en el escritorio"; GroupDescription: "Íconos:"

[InstallDelete]
; Limpia los archivos de la versión anterior del programa (no toca los datos).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\EndoView\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\EndoView"; Filename: "{app}\EndoView.exe"
Name: "{autodesktop}\EndoView"; Filename: "{app}\EndoView.exe"; Tasks: escritorio

[Run]
; Instalación normal: ofrece abrir el programa al terminar.
Filename: "{app}\EndoView.exe"; Description: "Abrir EndoView"; Flags: nowait postinstall skipifsilent
; Actualización automática (silenciosa): vuelve a abrir el programa.
Filename: "{app}\EndoView.exe"; Flags: nowait; Check: EsActualizacion

; Los datos (estudios, usuarios, conexión con Drive) viven en %LOCALAPPDATA%\EndoView
; y NO se borran al actualizar ni al desinstalar.

[Code]
function EsActualizacion: Boolean;
begin
  Result := ExpandConstant('{param:actualizar|0}') = '1';
end;

