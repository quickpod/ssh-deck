; Inno Setup — SSHDeck. Signed single-file installer, compiled in CI.
#define AppName "SSHDeck"
#define AppVersion "1.0.3"

[Setup]
AppMutex=QuickOpen.SSHDeck
AppId={{51A0F001-0004-4E5B-8C71-9B0E2F3A0004}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=QuickOpen (quickopen.ai)
AppPublisherURL=https://quickopen.ai/projects/ssh-deck
DefaultDirName={autopf}\SSHDeck
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\SSHDeck.exe
OutputDir=dist
OutputBaseFilename=SSHDeck-Setup
SetupIconFile=..\ssh-deck.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardImageFile=branding\wizard-large.bmp
WizardSmallImageFile=branding\wizard-small.bmp
AppCopyright=Apache-2.0. 100%% AI-built, published on QuickOpen (quickopen.ai).
VersionInfoCompany=QuickOpen
VersionInfoProductName=SSHDeck
VersionInfoVersion=1.0.3.0
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=SSHDeck is a 100%% AI-built, open-source offline tool, published on QuickOpen (quickopen.ai).%n%nThis will install it on your computer.
BeveledLabel=QuickOpen · quickopen.ai

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "trustca"; Description: "Trust the QuickOpen Root CA (lets Windows verify QuickOpen signatures)"; GroupDescription: "Security:"; Flags: unchecked

[Files]
Source: "staging\SSHDeck.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "staging\quickopen-root.crt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "staging\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme skipifsourcedoesntexist
Source: "staging\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\SSHDeck"; Filename: "{app}\SSHDeck.exe"; IconFilename: "{app}\SSHDeck.exe"
Name: "{group}\Uninstall SSHDeck"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SSHDeck"; Filename: "{app}\SSHDeck.exe"; IconFilename: "{app}\SSHDeck.exe"; Tasks: desktopicon

[Run]
Filename: "certutil.exe"; Parameters: "-addstore -user Root ""{app}\quickopen-root.crt"""; Tasks: trustca; Flags: runhidden; StatusMsg: "Trusting the QuickOpen Root CA..."
Filename: "{app}\SSHDeck.exe"; Description: "Launch SSHDeck now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\SSHDeck"

