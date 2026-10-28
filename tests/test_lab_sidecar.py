"""Worker por stdio + cliente Qt: protocolo, caché por etapa, cancelación y muerte del árbol de procesos (sin torch)."""
import hashlib
import threading
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from marea.audio.local import write_wav
from marea.export.ffmpeg import find_ffmpeg
from marea.lab.sidecar import SidecarClient, kill_tree, sidecar_root, worker_argv
from sidecar.stages import Cancelled, Ctx, StageError, decode, run_stage
from tests.test_spotify_ui import until

FFMPEG = find_ffmpeg()
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def wav(tmp_path) -> Path:
    t = np.arange(int(48000 * 1.5)) / 48000
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    write_wav(tmp_path / "tono.wav", np.stack([tone, tone], axis=1))
    return tmp_path / "tono.wav"


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


class Recorder:
    def __init__(self, client: SidecarClient):
        self.events = []
        client.ready.connect(lambda: self.events.append(("ready",)))
        client.stage.connect(lambda rid, name, state, s: self.events.append(("stage", rid, name, state)))
        client.result.connect(lambda rid, data: self.events.append(("result", rid, data)))
        client.failed.connect(lambda rid, msg, detail: self.events.append(("failed", rid, msg, detail)))
        client.cancelled.connect(lambda rid: self.events.append(("cancelled", rid)))
        client.exited.connect(lambda code: self.events.append(("exited", code)))

    def kinds(self):
        return [e[0] for e in self.events]

    def last(self, kind):
        return next(e for e in reversed(self.events) if e[0] == kind)


@pytest.fixture
def real(qapp, tmp_path):
    client = SidecarClient()
    rec = Recorder(client)
    client.start(worker_argv(sys.executable))
    assert until(lambda: "ready" in rec.kinds(), 15000), client.stderr_tail
    yield client, rec, tmp_path / "cache"
    client.stop()


def ask(client, rec, cache, wav):
    rid = client.request("decode", audio=str(wav), ffmpeg=FFMPEG, cache_dir=str(cache))
    assert until(lambda: any(e[0] in ("result", "failed", "cancelled") and e[1] == rid for e in rec.events), 15000), client.stderr_tail
    return rid


# ---------------- protocolo y caché ----------------

def test_decode_emits_real_stages_then_result_and_writes_16k_mono(real, wav):
    client, rec, cache = real
    rid = ask(client, rec, cache, wav)
    assert [e for e in rec.events if e[0] == "stage"] == [("stage", rid, "decode", "start"), ("stage", rid, "decode", "done")]
    data = rec.last("result")[2]
    assert data["audio_sha256"] == hashlib.sha256(wav.read_bytes()).hexdigest()
    assert data["rate"] == 16000 and abs(data["duration_ms"] - 1500) <= 5
    pcm = np.load(data["pcm_path"])
    assert pcm.dtype == np.float32 and pcm.ndim == 1 and len(pcm) == data["samples"]
    assert abs(np.abs(pcm).max() - 0.3 * 2 ** 0.5) < 0.02  # ffmpeg -ac 1 mezcla con +3 dB en material centrado; lo demás es relativo al nivel
    assert not list(Path(data["pcm_path"]).parent.glob("*.tmp"))


def test_second_request_for_the_same_audio_is_served_from_cache(real, wav):
    client, rec, cache = real
    first = ask(client, rec, cache, wav)
    second = ask(client, rec, cache, wav)
    states = [(e[1], e[3]) for e in rec.events if e[0] == "stage"]
    assert states == [(first, "start"), (first, "done"), (second, "cached")]  # sin start/done: no se recalculó nada
    results = [e[2] for e in rec.events if e[0] == "result"]
    assert results[0] == results[1]


def test_changed_audio_gets_its_own_cache_entry(real, wav, tmp_path):
    client, rec, cache = real
    ask(client, rec, cache, wav)
    other = tmp_path / "otro.wav"
    write_wav(other, np.zeros((24000, 2), np.float32) + 0.1)
    ask(client, rec, cache, other)
    assert len({p.name for p in cache.iterdir()}) == 2


def test_bad_inputs_become_friendly_errors_and_the_worker_survives(real, wav, tmp_path):
    client, rec, cache = real
    junk = tmp_path / "no_es_audio.mp3"
    junk.write_bytes(b"esto no es audio")
    ask(client, rec, cache, junk)
    assert "No pude leer ese archivo de audio" in rec.last("failed")[2]
    ask(client, rec, cache, tmp_path / "no_existe.wav")
    assert "No pude abrir el archivo" in rec.last("failed")[2]
    assert not [p for d in cache.glob("*") for p in d.iterdir()]  # ningún artefacto a medias
    rid = client.request("inventado", cache_dir=str(cache))
    assert until(lambda: any(e[0] == "failed" and e[1] == rid for e in rec.events))
    assert "Comando desconocido" in rec.last("failed")[2]
    ask(client, rec, cache, wav)  # sigue vivo
    assert rec.kinds()[-1] == "result"


def test_cancel_that_arrives_before_the_request_is_not_lost(real, wav):
    client, rec, cache = real
    client._send({"cmd": "cancel", "id": "r1"})  # el peor orden posible: el worker ve el cancel antes que la petición
    rid = client.request("decode", audio=str(wav), ffmpeg=FFMPEG, cache_dir=str(cache))
    assert rid == "r1" and until(lambda: ("cancelled", rid) in rec.events)
    assert "stage" not in rec.kinds() and "result" not in rec.kinds() and not list(cache.rglob("*"))
    ask(client, rec, cache, wav)  # y el worker queda en buen estado para la siguiente


# ---------------- cancelación en la etapa ----------------

def test_cancelled_stage_leaves_no_half_written_cache(wav, tmp_path):
    cancelled = {"r1"}
    ctx = Ctx("r1", lambda **ev: None, tmp_path / "cache", cancelled)
    with pytest.raises(Cancelled):
        decode(ctx, str(wav), FFMPEG)
    assert not list((tmp_path / "cache").rglob("*.npy")) and not list((tmp_path / "cache").rglob("*.tmp"))


def test_stage_error_is_not_cached(wav, tmp_path):
    ctx = Ctx("r1", lambda **ev: None, tmp_path / "cache", set())
    with pytest.raises(StageError):
        decode(ctx, str(wav), str(tmp_path / "ffmpeg_inexistente.exe"))
    assert not list((tmp_path / "cache").rglob("*.npy"))


def test_failure_after_writing_the_temp_file_publishes_nothing_and_cleans_up(tmp_path):
    ctx = Ctx("r1", lambda **ev: None, tmp_path / "cache", set())

    def explodes(tmp):
        tmp.write_bytes(b"a medias")
        raise RuntimeError("se cayó a la mitad")

    with pytest.raises(RuntimeError):
        run_stage(ctx, "abc", "etapa", {"k": 1}, "bin", explodes)
    assert list((tmp_path / "cache").rglob("*.*")) == []  # ni artefacto final ni .tmp huérfano
    ok = run_stage(ctx, "abc", "etapa", {"k": 1}, "bin", lambda tmp: tmp.write_bytes(b"completo"))
    assert ok.read_bytes() == b"completo"  # y un reintento con los mismos parámetros no choca con restos


# ---------------- cliente: cancelar por la fuerza y caídas ----------------

HANG = '''
import json, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid) + " " + str(__import__("os").getpid()))
print(json.dumps({"event": "ready"}), flush=True)
sys.stdin.readline()
time.sleep(120)  # una llamada larga a un modelo: no atiende el cancel
'''

CRASH = '''
import json, sys
print("ruido de una librería en stdout")
print(json.dumps({"event": "ready"}), flush=True)
sys.stdin.readline()
print("Traceback: algo explotó", file=sys.stderr, flush=True)
sys.exit(3)
'''


def fake(qapp, tmp_path, source, name):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    client = SidecarClient()
    rec = Recorder(client)
    client.start([sys.executable, str(script), str(tmp_path / "pids.txt")], cwd=tmp_path)
    assert until(lambda: "ready" in rec.kinds(), 15000)
    return client, rec


def test_unresponsive_worker_is_killed_with_its_children_after_the_grace_period(qapp, tmp_path):
    client, rec = fake(qapp, tmp_path, HANG, "hang.py")
    child, parent = map(int, (tmp_path / "pids.txt").read_text().split())
    rid = client.request("decode")
    t0 = time.monotonic()
    client.cancel(grace_ms=300)
    assert until(lambda: ("cancelled", rid) in rec.events, 3000)
    assert time.monotonic() - t0 < 3.0  # el requisito: menos de 3 s
    assert until(lambda: not client.running, 3000)
    assert until(lambda: not alive(parent) and not alive(child), 5000), "quedó vivo el worker o su proceso hijo"


def test_worker_that_answers_the_cancel_is_not_killed(qapp, tmp_path):
    answers = HANG.replace("time.sleep(120)  #", "print(json.dumps({'event': 'cancelled', 'id': 'r1'}), flush=True); time.sleep(120)  #")
    client, rec = fake(qapp, tmp_path, answers.replace("sys.stdin.readline()", "sys.stdin.readline(); sys.stdin.readline()"), "polite.py")
    rid = client.request("decode")
    client.cancel(grace_ms=300)
    assert until(lambda: ("cancelled", rid) in rec.events, 3000)
    assert rec.events.count(("cancelled", rid)) == 1
    time.sleep(0.6)  # pasada la gracia no hay segunda notificación ni muerte forzada
    assert rec.events.count(("cancelled", rid)) == 1 and client.running
    client.stop()


def test_worker_crash_fails_the_pending_request_with_stderr_and_ignores_stdout_noise(qapp, tmp_path):
    client, rec = fake(qapp, tmp_path, CRASH, "crash.py")
    rid = client.request("decode")
    assert until(lambda: any(e[0] == "failed" for e in rec.events), 5000)
    _, frid, message, detail = rec.last("failed")
    assert frid == rid and "se cerró inesperadamente" in message and "algo explotó" in detail
    assert until(lambda: ("exited", 3) in rec.events)
    assert rec.kinds().count("failed") == 1


def test_sidecar_root_contains_the_package():
    assert (sidecar_root() / "sidecar" / "worker.py").exists()


NOISY = (
    "from sidecar import worker, stages\n"
    "orig = stages.decode\n"
    "def noisy(ctx, **kw):\n"
    "    print('descargando modelo 50%', end='')  # una librería escribe en stdout sin salto de línea\n"
    "    return orig(ctx, **kw)\n"
    "worker.COMMANDS['decode'] = noisy\n"
    "worker.main()\n"
)


def test_library_prints_cannot_corrupt_the_protocol(qapp, tmp_path, wav):
    client = SidecarClient()
    rec = Recorder(client)
    client.start([sys.executable, "-u", "-X", "utf8", "-c", NOISY])
    assert until(lambda: "ready" in rec.kinds(), 15000), client.stderr_tail
    ask(client, rec, tmp_path / "cache", wav)
    assert [e[3] for e in rec.events if e[0] == "stage"] == ["start", "done"] and rec.kinds()[-1] == "result"
    assert "descargando modelo" in client.stderr_tail  # el ruido terminó en stderr, no en el protocolo
    client.stop()


# ---------------- lectura de stdin sin colgar la carga de DLL nativas ----------------

def run_child(code: str, *args, feed=None, timeout=15):
    """Hijo con stdin = pipe abierto (como el worker). `feed(p)` escribe en su stdin. Devuelve (salida, colgado)."""
    p = subprocess.Popen([sys.executable, "-c", code, *args], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    result = {}
    t = threading.Thread(target=lambda: result.update(out=p.stdout.readline().strip()), daemon=True)
    t.start()
    if feed:
        feed(p)
    t.join(timeout)
    hung = t.is_alive()
    kill_tree(p.pid)
    return result.get("out", ""), hung


def test_stdin_reader_yields_whole_lines_even_when_writes_are_split_and_ends_promptly_at_eof():
    code = ("import json, time\n"
            "from sidecar.stdin_reader import stdin_lines\n"
            "t = time.perf_counter()\n"
            "lines = [l.decode() for l in stdin_lines()]\n"
            "print(json.dumps({'lines': lines}), flush=True)\n")

    def feed(p):
        p.stdin.write('{"a":1}\n{"b"')
        p.stdin.flush()
        time.sleep(0.15)
        p.stdin.write(':2}\nsin salto final')
        p.stdin.flush()
        time.sleep(0.1)
        p.stdin.close()

    out, hung = run_child(code, feed=feed, timeout=5)  # si no detectara el EOF, el hijo nunca imprimiría
    assert not hung
    # el hijo recibe \r\n (Popen en modo texto en Windows): lo que importa son los límites de línea, aunque la escritura llegue partida
    assert [l.rstrip("\r\n") for l in json.loads(out)["lines"]] == ['{"a":1}', '{"b":2}', "sin salto final"]


NUMPY_AFTER_READER = (
    "import sys, threading, time\n"
    "if sys.argv[1] == 'polling':\n"
    "    from sidecar.stdin_reader import stdin_lines\n"
    "    reader = lambda: [None for _ in stdin_lines()]\n"
    "else:\n"
    "    reader = lambda: sys.stdin.buffer.readline()  # lectura síncrona bloqueada en el pipe\n"
    "threading.Thread(target=reader, daemon=True).start()\n"
    "time.sleep(0.3)\n"
    "import numpy  # una librería nativa que se carga DESPUÉS de arrancar el hilo lector\n"
    "print('OK', flush=True)\n"
)


def test_native_libraries_load_while_the_worker_waits_for_commands():
    """En Windows, un hilo bloqueado en ReadFile sobre el pipe de stdin cuelga la carga de DLL nativas (aquí `import numpy`; en el sidecar real
    fue SciPy vía audio-separator). El control demuestra que la plataforma lo reproduce; el lector del worker no debe sufrirlo."""
    _, control_hung = run_child(NUMPY_AFTER_READER, "blocking", timeout=6)
    if not control_hung:
        pytest.skip("Esta versión de Windows/numpy ya no reproduce el cuelgue: el test no demostraría nada.")
    out, hung = run_child(NUMPY_AFTER_READER, "polling", timeout=15)
    assert out == "OK" and not hung
