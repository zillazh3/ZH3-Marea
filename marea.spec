# PyInstaller, carpeta portable: dist/ZH3 Marea/ZH3 Marea.exe. Se construye con `python tools/build_exe.py`.
from PyInstaller.utils.hooks import collect_all

skia = collect_all("skia")
audio = collect_all("pyaudiowpatch")

EXCLUDE = ["pytest", "tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
           "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtPdf", "PySide6.QtMultimedia", "PySide6.QtCharts",
           "PySide6.QtDataVisualization", "PySide6.QtNetworkAuth", "PySide6.QtTest"]

a = Analysis(
    ["tools/launcher.py"],
    pathex=["."],
    binaries=skia[1] + audio[1],
    datas=[("assets/fonts", "assets/fonts"), ("assets/icons", "assets/icons"),
           ("assets/logo/marea.svg", "assets/logo"), ("assets/logo/marea-16.svg", "assets/logo"),
           ("sidecar", "sidecar")] + skia[0] + audio[0],  # sidecar/: solo scripts y requisitos (unos KB); torch y los modelos NO entran al .exe
    hiddenimports=["keyring.backends.Windows", "win32ctypes.pywin32"] + skia[2] + audio[2],
    excludes=EXCLUDE,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ZH3 Marea", console=False, icon="assets/logo/marea.ico")
coll = COLLECT(exe, a.binaries, a.datas, name="ZH3 Marea")
