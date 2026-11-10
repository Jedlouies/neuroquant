#ifndef AppVersion
#define AppVersion "6.0"
#endif

[Setup]
AppName=NeuroQuant
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}.0.0
DefaultDirName={autopf}\NeuroQuant
DefaultGroupName=NeuroQuant
OutputDir=.\Output
OutputBaseFilename=NeuroQuant_Installer_{#AppVersion}
SetupIconFile=assets\logo\colored.ico
UninstallDisplayIcon={app}\NeuroQuant.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64

[Files]
Source: "dist\NeuroQuant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\NeuroQuant"; Filename: "{app}\NeuroQuant.exe"; IconFilename: "{app}\NeuroQuant.exe"
Name: "{autodesktop}\NeuroQuant"; Filename: "{app}\NeuroQuant.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\NeuroQuant.exe"; Description: "Launch NeuroQuant"; Flags: postinstall nowait skipifsilent

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"
