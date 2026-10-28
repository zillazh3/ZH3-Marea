"""`ZH3 Marea.exe --selftest`: exporta un clip de 1 s por el mismo camino que la app (proceso aparte, GPU, ffmpeg) y deja el
resultado en data/selftest.txt. Sirve para comprobar el .exe en un equipo limpio sin abrir la ventana."""
import multiprocessing as mp
from pathlib import Path

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.export.job import ExportJob, worker_main
from marea.paths import app_data


def run() -> int:
    out = app_data() / "selftest.mp4"
    out.unlink(missing_ok=True)
    start = exact_lyrics().lines[0].start
    job = ExportJob(exact_lyrics(), bytes(demo_cover(256).encodeToData()), out, start, start + 1000)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=worker_main, args=(job, q, ctx.Event()))
    p.start()
    result = "sin respuesta"
    while True:
        msg = q.get()
        if msg[0] != "progress":
            result = msg[1] if msg[0] == "done" else f"{msg[0]}: {msg[1:]}"
            break
    p.join(10)
    ok = msg[0] == "done" and out.is_file() and out.stat().st_size > 10_000
    extra = []
    try:  # lo que el .exe necesita aparte del render: credenciales y captura de audio
        import keyring
        from marea.audio.loopback import list_capture_devices
        keyring.get_password("ZH3 Marea", "selftest")
        extra += [f"keyring: {type(keyring.get_keyring()).__name__}", f"dispositivos de captura: {len(list_capture_devices())}"]
    except Exception as e:
        ok = False
        extra.append(f"FALLO {type(e).__name__}: {e}")
    size = out.stat().st_size if out.is_file() else 0
    (app_data() / "selftest.txt").write_text("\n".join(["OK" if ok else "FALLO", result, f"{size} bytes", *extra]) + "\n",
                                             encoding="utf-8")
    return 0 if ok else 1
