"""Proceso aparte de la función experimental. Protocolo: una línea JSON por mensaje, UTF-8.

  entrada (stdin):  {"cmd": "decode", "id": "r1", ...parámetros}   |   {"cmd": "cancel", "id": "r1"}
  salida (stdout):  {"event": "ready"} · {"event": "stage", "id", "name", "state": "start|done|cached|error", "seconds"}
                    · {"event": "result", "id", "data"} · {"event": "error", "id", "message", "detail"} · {"event": "cancelled", "id"}

stdout es SOLO del protocolo: lo demás (prints de librerías) se desvía a stderr. Si stdin se cierra (la app murió), el worker sale.
Se lanza con `python -u -X utf8 -m sidecar.worker`, con la carpeta que contiene `sidecar/` como directorio de trabajo.
"""
import json
import os
import queue
import sys
import threading
import traceback

# Sin red por defecto, antes de que se importe huggingface_hub o transformers. No es solo cortesía: al cargar un pytorch_model.bin con red,
# transformers comprueba si hay versión safetensors y, si no la encuentra, lanza un hilo que consulta el Hub y puede pedir la conversión de
# un modelo AJENO (un PR en su repo), incluso con local_files_only. Solo el proceso de descarga se inicia con HF_HUB_OFFLINE=0.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from sidecar import stages  # noqa: E402
from sidecar.stages import Cancelled, Ctx, StageError  # noqa: E402
from sidecar.stdin_reader import stdin_lines  # noqa: E402

def probe(ctx: Ctx) -> dict:
    """Qué dispositivo usaría el sidecar y con qué versiones. Importa torch aquí: la primera vez puede tardar casi un minuto."""
    import ctranslate2
    import torch

    from sidecar import device
    try:  # componente opcional (separación de voz); arrastra librerías nativas, así que importarlo es además una prueba de que el worker carga DLL
        import audio_separator.separator  # noqa: F401
        separator = True
    except ImportError:
        separator = False
    return {**device.pick_device(torch), "torch": torch.__version__, "ctranslate2": ctranslate2.__version__, "separator": separator}


def fetch_model(ctx: Ctx, lang: str = "", model: str = "") -> dict:
    """Descarga explícita de un modelo de alineación (por idioma o por repo de HF). Es lo único que toca la red."""
    import time

    from sidecar import models
    repo = model or models.repo_for(lang)
    ctx.send(event="stage", id=ctx.id, name="download", state="start", detail={"model": repo})
    t0 = time.perf_counter()
    path = models.fetch(repo)
    ctx.send(event="stage", id=ctx.id, name="download", state="done", seconds=round(time.perf_counter() - t0, 3), detail={"model": repo})
    return {"model": repo, "path": path}


COMMANDS = {"decode": stages.decode, "probe": probe, "align": stages.align, "fetch_model": fetch_model,
            "fetch_separator": stages.fetch_separator, "asr": stages.asr, "fetch_asr": stages.fetch_asr}


def main():
    proto = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)
    sys.stdout = sys.stderr

    def send(**ev):
        proto.write(json.dumps(ev, ensure_ascii=False) + "\n")
        proto.flush()

    inbox, cancelled = queue.Queue(), set()  # cancelar por id: un cancel que llega antes de empezar la tarea tampoco se pierde

    def reader():
        for raw in stdin_lines():
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if msg.get("cmd") == "cancel":
                cancelled.add(msg.get("id"))
            else:
                inbox.put(msg)
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()
    send(event="ready", python=sys.version.split()[0])
    while (msg := inbox.get()) is not None:
        req_id, cmd = msg.get("id", ""), msg.pop("cmd", None)
        params = {k: v for k, v in msg.items() if k not in ("id", "cache_dir")}
        try:
            if cmd not in COMMANDS:
                raise StageError(f"Comando desconocido: {cmd!r}")
            ctx = Ctx(req_id, send, msg.get("cache_dir", "."), cancelled)
            ctx.check()
            send(event="result", id=req_id, data=COMMANDS[cmd](ctx, **params))
        except Cancelled:
            send(event="cancelled", id=req_id)
        except StageError as e:
            send(event="error", id=req_id, message=str(e), detail="")
        except Exception:
            send(event="error", id=req_id, message="Falló la sincronía automática por un error interno.", detail=traceback.format_exc())
        cancelled.discard(req_id)


if __name__ == "__main__":
    main()
