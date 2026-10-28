"""Instalador del entorno pesado y selección de dispositivo, sin descargar nada real (servidor local, procesos falsos, torch falso)."""
import hashlib
import io
import json
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from marea.lab import env
from marea.lab.env import InstallCancelled, InstallError
from sidecar.device import pick_device

ROOT = Path(__file__).resolve().parents[1]
assert env.REQ_DIR == ROOT / "sidecar"  # en desarrollo, sidecar_root() es la raíz del repo


# ---------------- fetch_uv ----------------

def make_zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def server():
    files, served = {}, []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            served.append(self.path)
            body = files.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", files, served
    srv.shutdown()


def test_fetch_uv_verifies_the_pinned_hash_then_extracts_only_uv_exe(tmp_path, server):
    base, files, _ = server
    data = make_zip({"uv.exe": b"MZ-falso", "uvx.exe": b"otro", "../../evil.txt": b"x"})
    files["/uv.zip"] = data
    seen = []
    exe = env.fetch_uv(tmp_path, lambda s, t: seen.append((s, t)), url=base + "/uv.zip", sha256=hashlib.sha256(data).hexdigest())
    assert exe == tmp_path / "bin" / "uv.exe" and exe.read_bytes() == b"MZ-falso"
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["uv.exe"]  # ni el zip ni otros miembros
    assert not (tmp_path.parent / "evil.txt").exists()
    assert seen and seen[-1][0] == "uv" and f"{len(data)} / {len(data)}" in seen[-1][1]  # bytes reales descargados


def test_fetch_uv_refuses_a_download_that_does_not_match_the_pinned_hash(tmp_path, server):
    base, files, _ = server
    files["/uv.zip"] = make_zip({"uv.exe": b"troyano"})
    with pytest.raises(InstallError, match="no coincide con el hash"):
        env.fetch_uv(tmp_path, lambda *a: None, url=base + "/uv.zip", sha256="0" * 64)
    assert not (tmp_path / "bin" / "uv.exe").exists() and not list((tmp_path / "bin").iterdir())  # nada ejecutable queda en disco


def test_fetch_uv_reports_network_and_archive_problems_in_plain_words(tmp_path, server):
    base, files, _ = server
    with pytest.raises(InstallError, match="No pude descargar uv"):
        env.fetch_uv(tmp_path, lambda *a: None, url=base + "/no-existe.zip")
    bad = make_zip({"leeme.txt": b"sin uv"})
    files["/sin_uv.zip"] = bad
    with pytest.raises(InstallError, match="no es válido"):
        env.fetch_uv(tmp_path, lambda *a: None, url=base + "/sin_uv.zip", sha256=hashlib.sha256(bad).hexdigest())


def test_fetch_uv_skips_the_download_when_already_present(tmp_path, server):
    base, _, served = server
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "uv.exe").write_bytes(b"ya estaba")
    assert env.fetch_uv(tmp_path, lambda *a: None, url=base + "/uv.zip").read_bytes() == b"ya estaba" and served == []


def test_the_pinned_uv_url_and_hash_are_consistent():
    assert env.UV_VERSION in env.UV_URL and len(env.UV_SHA256) == 64 and set(env.UV_SHA256) <= set("0123456789abcdef")


# ---------------- marcador de instalado ----------------

def test_is_installed_needs_the_marker_the_matching_requirements_and_the_python(tmp_path):
    req = tmp_path / "req"
    req.mkdir()
    (req / "requirements-torch-cu128.txt").write_text("torch==1\n")
    (req / "requirements.txt").write_text("numpy==1\n")
    root = tmp_path / "lab"
    assert not env.is_installed(root, req)
    root.mkdir()
    (root / "ready.json").write_text(json.dumps({"requirements": env.requirements_hash(req)}))
    assert not env.is_installed(root, req)  # marcador sin Python: instalación a medias
    (root / "env" / "Scripts").mkdir(parents=True)
    env.env_python(root).write_bytes(b"")
    assert env.is_installed(root, req)
    (req / "requirements.txt").write_text("numpy==2\n")  # esta versión de la app pide otra cosa: hay que reinstalar
    assert not env.is_installed(root, req)
    (root / "ready.json").write_text("{no es json")
    assert not env.is_installed(root, req)


def test_requirements_are_fully_pinned_and_torch_is_installed_separately():
    rest = [l.strip() for l in (ROOT / "sidecar" / "requirements.txt").read_text(encoding="utf-8").splitlines() if l.strip() and l[0] != "#"]
    assert rest and all("==" in l for l in rest) and not any(l.lower().startswith("torch") for l in rest)
    assert {"ctranslate2", "faster-whisper", "transformers", "numpy"} <= {l.split("==")[0] for l in rest}
    torch = [l for l in (ROOT / "sidecar" / "requirements-torch-cu128.txt").read_text(encoding="utf-8").splitlines() if l[:1] != "#"]
    assert [l for l in torch if l.strip()] == ["torch==2.11.0"]


# ---------------- ejecutar uv: errores y cancelación ----------------

def test_run_streams_real_lines_and_reports_failures_with_the_tail(tmp_path):
    seen = []
    env._run(tmp_path, "paso", [sys.executable, "-c", "print('uno'); print('dos')"], lambda s, t: seen.append((s, t)), None, "falló")
    assert seen == [("paso", "uno"), ("paso", "dos")]
    with pytest.raises(InstallError, match=r"No pude instalar torch \(código 3\)[\s\S]*porque sí"):
        env._run(tmp_path, "torch", [sys.executable, "-c", "print('porque sí'); raise SystemExit(3)"], lambda *a: None, None,
                 "No pude instalar torch")


def test_cancelling_an_install_step_kills_the_process_tree_quickly(tmp_path):
    cancel = threading.Event()
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('listo', flush=True); time.sleep(60)"

    def progress(stage, text):
        cancel.set()  # el usuario pulsa cancelar en cuanto aparece la primera línea

    t0 = time.monotonic()
    with pytest.raises(InstallCancelled):
        env._run(tmp_path, "torch", [sys.executable, "-c", code], progress, cancel, "x")
    assert time.monotonic() - t0 < 5


# ---------------- desinstalar ----------------

def test_uninstall_only_removes_a_folder_named_lab(tmp_path):
    lab = tmp_path / "lab"
    (lab / "env").mkdir(parents=True)
    (lab / "env" / "x.bin").write_bytes(b"1")
    other = tmp_path / "mis_cosas"
    other.mkdir()
    with pytest.raises(InstallError):
        env.uninstall(other)
    assert other.exists()
    env.uninstall(lab)
    assert not lab.exists() and tmp_path.exists()


# ---------------- selección de dispositivo ----------------

def fake_torch(*, available=True, cap=(12, 0), arch=("sm_90", "sm_120"), product=64.0 ** 3, boom=None):
    class T:
        float16 = "fp16"

        def __init__(self, v):
            self.v = v

        def __matmul__(self, other):
            if boom:
                raise RuntimeError(boom)
            return T(product)

        def float(self):
            return self

        def sum(self):
            return self

        def item(self):
            return self.v

    cuda = SimpleNamespace(is_available=lambda: available, get_device_capability=lambda i: cap, get_device_name=lambda i: "GPU falsa",
                           get_arch_list=lambda: list(arch), get_device_properties=lambda i: SimpleNamespace(total_memory=8 * 2**30))
    return SimpleNamespace(cuda=cuda, float16="fp16", ones=lambda *a, **k: T(1))


def test_gpu_is_chosen_only_after_a_real_check_and_otherwise_the_reason_is_explained():
    ok = pick_device(fake_torch())
    assert ok["device"] == "cuda" and ok["capability"] == [12, 0] and ok["total_gb"] == 8.0 and ok["reason"] == ""
    none = pick_device(fake_torch(available=False))
    assert none["device"] == "cpu" and "no ve ninguna GPU" in none["reason"]
    old = pick_device(fake_torch(arch=("sm_90",)))
    assert old["device"] == "cpu" and "sm_120" in old["reason"] and old["name"] == "GPU falsa"
    bad = pick_device(fake_torch(product=7.0))
    assert bad["device"] == "cpu" and "resultado incorrecto" in bad["reason"]
    crash = pick_device(fake_torch(boom="CUDA error: out of memory"))
    assert crash["device"] == "cpu" and "out of memory" in crash["reason"]


# ---------------- componentes opcionales y modelos ----------------

def test_model_presence_is_decided_from_the_disk_only(tmp_path):
    root = tmp_path / "lab"
    assert not env.hf_model_ready(root, "org/modelo") and not env.separator_model_ready(root) and not env.separation_installed(root)
    snap = root / "hf" / "hub" / "models--org--modelo" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    assert not env.hf_model_ready(root, "org/modelo")  # solo configuración, sin pesos: no cuenta
    (snap / "pytorch_model.bin").write_bytes(b"1")
    assert env.hf_model_ready(root, "org/modelo") and not env.hf_model_ready(root, "org/otro")
    (root / "separator-models").mkdir()
    (root / "separator-models" / env.SEPARATION_MODEL).write_bytes(b"1")
    (root / "env" / "Lib" / "site-packages" / "audio_separator").mkdir(parents=True)
    assert env.separator_model_ready(root) and env.separation_installed(root)


def test_installing_separation_without_the_base_environment_is_a_clear_error(tmp_path):
    with pytest.raises(InstallError, match="Primero instala"):
        env.install_separation(tmp_path / "lab", lambda *a: None)


# ---------------- carpeta movida de lugar ----------------

def _fake_env(root, home):
    env.env_python(root).parent.mkdir(parents=True)
    env.env_python(root).write_bytes(b"")
    (root / "env" / "pyvenv.cfg").write_text(f"home = {home}\nimplementation = CPython\nversion_info = 3.12.16\n", encoding="utf-8")


def test_a_moved_folder_is_detected_because_the_venv_keeps_absolute_paths(tmp_path):
    req = tmp_path / "req"
    req.mkdir()
    (req / "requirements-torch-cu128.txt").write_text("torch==1\n")
    (req / "requirements.txt").write_text("numpy==1\n")
    root = tmp_path / "lab"
    base = root / "python"
    base.mkdir(parents=True)
    _fake_env(root, base)
    (root / "ready.json").write_text('{"requirements": "%s"}' % env.requirements_hash(req))
    assert not env.env_relocated(root) and env.is_installed(root, req)
    base.rmdir()  # el Python base ya no está donde el venv cree: así queda una carpeta movida
    assert env.env_relocated(root) and not env.is_installed(root, req)
    assert not env.env_relocated(tmp_path / "otra")  # sin venv no hay nada que reubicar


def test_install_rebuilds_a_relocated_env_instead_of_trusting_it(tmp_path, monkeypatch):
    root = tmp_path / "lab"
    _fake_env(root, tmp_path / "ya_no_existe")
    (root / "bin").mkdir()
    (root / "bin" / "uv.exe").write_bytes(b"")
    called, said = [], []
    monkeypatch.setattr(env, "_run", lambda r, stage, args, progress, cancel, msg: called.append((stage, args[1:3])))
    env.install(root, lambda s, t: said.append(t), req_dir=ROOT / "sidecar")
    assert any("se movió" in t for t in said)
    assert ("python", ["venv", str(root / "env")]) in called  # se vuelve a crear


def test_an_env_whose_python_does_not_run_is_rebuilt_even_if_pyvenv_cfg_looks_fine(tmp_path, monkeypatch):
    """El caso real: pyvenv.cfg arreglado a mano pero el trampolín de python.exe sigue apuntando a la ruta vieja."""
    root = tmp_path / "lab"
    base = root / "python"
    base.mkdir(parents=True)
    _fake_env(root, base)  # home existe...
    (root / "bin").mkdir()
    (root / "bin" / "uv.exe").write_bytes(b"")
    assert not env.env_relocated(root) and not env.env_runs(root)  # ...pero python.exe (vacío) no arranca
    said = []
    monkeypatch.setattr(env, "_run", lambda *a, **k: None)
    env.install(root, lambda s, t: said.append(t), req_dir=ROOT / "sidecar")
    assert any("no arranca" in t for t in said)
