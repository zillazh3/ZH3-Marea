# PyInstaller, carpeta portable: dist/ZH3 Marea/ZH3 Marea.exe. Se construye con `python tools/build_exe.py`.
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo
from marea.version import VERSION

release_version = tuple(int(part) for part in VERSION.split(".")) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=release_version, prodvers=release_version),
    kids=[StringFileInfo([StringTable("040904B0", [
        StringStruct("CompanyName", "ZH3"),
        StringStruct("FileDescription", "ZH3 Marea"),
        StringStruct("FileVersion", VERSION),
        StringStruct("ProductName", "ZH3 Marea"),
        StringStruct("ProductVersion", VERSION),
        StringStruct("OriginalFilename", "ZH3 Marea.exe"),
    ])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])],
)

skia = collect_all("skia")
audio = collect_all("pyaudiowpatch")

EXCLUDE = ["pytest", "tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
           "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtPdf", "PySide6.QtMultimedia", "PySide6.QtCharts",
           "PySide6.QtDataVisualization", "PySide6.QtNetworkAuth", "PySide6.QtTest"]

a = Analysis(
    ["tools/launcher.py"],
    pathex=["."],
    binaries=skia[1] + audio[1],
    datas=[("assets/fonts", "assets/fonts"), ("assets/icons", "assets/icons"), ("assets/wordfx", "assets/wordfx"), ("assets/presets", "assets/presets"),
           ("assets/logo/marea.svg", "assets/logo"), ("assets/logo/marea-16.svg", "assets/logo"),
           ("sidecar", "sidecar")] + skia[0] + audio[0],  # sidecar/: solo scripts y requisitos (unos KB); torch y los modelos NO entran al .exe
    hiddenimports=["keyring.backends.Windows", "win32ctypes.pywin32"] + skia[2] + audio[2],
    excludes=EXCLUDE,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ZH3 Marea", console=False, icon="assets/logo/marea.ico", version=version_info)
coll = COLLECT(exe, a.binaries, a.datas, name="ZH3 Marea")
