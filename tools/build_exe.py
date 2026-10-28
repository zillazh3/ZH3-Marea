"""Construye la carpeta portable: PyInstaller + ffmpeg dentro. Uso: python tools/build_exe.py [--zip]"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "ZH3 Marea"


def clean_data():
    """Borra data/ del build. Con reintentos porque el .exe recién cerrado puede tardar en soltar marea.log; si no se logra, se avisa (nunca en silencio)."""
    for _ in range(20):
        try:
            shutil.rmtree(OUT / "data")
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)
    sys.exit(f"No pude borrar {OUT / 'data'}: algo la tiene abierta. Ciérralo y repite.")


def write_zip() -> Path:
    zip_path = ROOT / "dist" / "ZH3-Marea-portable.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in OUT.rglob("*"):
            if f.is_file():
                z.write(f, Path("ZH3 Marea") / f.relative_to(OUT))
    print(f"Zip: {zip_path} ({zip_path.stat().st_size / 1e6:.0f} MB)")
    return zip_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="store_true", help="además crea dist/ZH3-Marea-portable.zip")
    ap.add_argument("--no-verify", action="store_true", help="omite la verificación del ícono (abre la app un momento)")
    args = ap.parse_args()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "marea.spec"], cwd=ROOT, check=True)
    ffmpeg = os.environ.get("MAREA_FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("No encontré ffmpeg para empaquetar: ponlo en el PATH o define MAREA_FFMPEG.")
    (OUT / "ffmpeg").mkdir(exist_ok=True)
    shutil.copy2(ffmpeg, OUT / "ffmpeg" / "ffmpeg.exe")
    shutil.copy2(ROOT / "README.md", OUT / "LEEME.md")
    for doc in ("LICENSE", "THIRD_PARTY.md"):
        shutil.copy2(ROOT / doc, OUT / doc)
    print(f"Listo: {OUT}")
    if not args.no_verify:  # el ícono del taskbar solo se puede comprobar con el .exe ya hecho
        if subprocess.run([sys.executable, str(ROOT / "tools" / "check_exe_icon.py"), str(OUT / "ZH3 Marea.exe")]).returncode:
            sys.exit("El build no pasó la verificación del ícono (ver arriba).")
    clean_data()  # la verificación abrió la app y creó data/ (log, ajustes): no debe viajar en el zip
    if args.zip:
        write_zip()


if __name__ == "__main__":
    main()
