"""Mide la alineación con el worker real contra las anotaciones de palabra de JamendoLyrics (tests/local_data/jamendo, sin commitear).

    python tools/eval_alignment.py [--lab <carpeta lab>] [--langs en,es] [--songs sub1,sub2] [--out resultados.json]

Por canción corre dos variantes con las mismas emisiones (la segunda sale de la caché): con energía (alarga notas y marca pausas) y
sin energía (el fin de cada palabra es el frame de su última letra). Reporta error por palabra, % dentro de ±50/±100 ms, % marcado
como aproximado, tiempo por etapa y VRAM pico.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from marea.export.ffmpeg import find_ffmpeg  # noqa: E402
from marea.lab import env  # noqa: E402
from marea.lab.metrics import word_errors  # noqa: E402


class Worker:
    def __init__(self, lab: Path):
        e = {**os.environ, "PYTHONUTF8": "1", "HF_HOME": str(lab / "hf")}
        self.p = subprocess.Popen([str(env.env_python(lab)), "-u", "-X", "utf8", "-m", "sidecar.worker"], cwd=ROOT, env=e, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        assert json.loads(self.p.stdout.readline())["event"] == "ready"
        self.n = 0

    def ask(self, cmd: str, **params) -> tuple[dict, list[dict]]:
        self.n += 1
        rid = f"e{self.n}"
        self.p.stdin.write(json.dumps({"cmd": cmd, "id": rid, **params}) + "\n")
        self.p.stdin.flush()
        stages = []
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("el worker murió")
            ev = json.loads(line)
            if ev.get("event") == "stage":
                stages.append(ev)
            elif ev.get("event") == "result":
                return ev["data"], stages
            elif ev.get("event") in ("error", "cancelled"):
                raise RuntimeError("\n".join([ev.get("message") or ev["event"], ev.get("detail", "")]))

    def close(self) -> float | None:
        """Segundos que tardó en salir tras cerrar stdin; None si hubo que matarlo (no debería: dejaría la GPU tomada)."""
        self.p.stdin.close()
        t0 = time.perf_counter()
        try:
            self.p.wait(timeout=30)
            return round(time.perf_counter() - t0, 1)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(self.p.pid), "/T", "/F"], capture_output=True)
            return None


def load_song(root: Path, lang: str, stem: str):
    lines = [l.split() for l in (root / lang / "lyrics" / f"{stem}.txt").read_text(encoding="utf-8").splitlines() if l.split()]
    words = (root / lang / "lyrics" / f"{stem}.words.txt").read_text(encoding="utf-8").split("\n")
    words = [w for w in words if w.strip()]
    truth = [(round(float(r["word_start"]) * 1000), round(float(r["word_end"]) * 1000))
             for r in csv.DictReader(open(root / lang / "annotations" / "words" / f"{stem}.csv", encoding="utf-8"))]
    flat = [w for l in lines for w in l]
    if not (len(flat) == len(words) == len(truth)):
        raise ValueError(f"no cuadran palabras: letra {len(flat)}, words.txt {len(words)}, anotación {len(truth)}")
    return [{"id": f"L{i}", "words": [[w] for w in l]} for i, l in enumerate(lines)], truth


def run_song(w: Worker, root: Path, lang: str, stem: str, ffmpeg: str, cache: Path, model: str = "", separate: str = "") -> dict:
    lines, truth = load_song(root, lang, stem)
    out = {"song": stem, "lang": lang, "words": len(truth)}
    for use_energy in (True, False):
        t0 = time.perf_counter()
        data, stages = w.ask("align", audio=str(root / lang / "mp3" / f"{stem}.mp3"), ffmpeg=ffmpeg, cache_dir=str(cache), lang=lang, lines=lines,
                             use_energy=use_energy, syllables=False, model=model, separate=separate)
        wall = time.perf_counter() - t0
        words = [x for l in data["lines"] for x in l["words"]]
        pred = [(x["start"], x["end"]) for x in words]
        res = word_errors(pred, truth)
        res.update(approx=round(sum(x["approximate"] for x in words) / len(words), 3), lowconf=round(sum(x["confidence"] < 0.5 for x in words) / len(words), 3),
                   zero_len=sum(x["end"] <= x["start"] for x in words), wall_s=round(wall, 1), device=data["device"]["device"],
                   stages={f'{s["name"]}:{s["state"]}': s.get("seconds") for s in stages if s["state"] != "start"},
                   peak_vram_mb=next((s.get("detail", {}).get("peak_vram_mb") for s in stages if s["name"] == "emissions" and s["state"] == "done"), None))
        out["energy" if use_energy else "no_energy"] = res
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default=str(env.lab_dir()))
    ap.add_argument("--data", default=str(ROOT / "tests" / "local_data" / "jamendo"))
    ap.add_argument("--langs", default="en,es")
    ap.add_argument("--songs", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--separate", default="", help="archivo del modelo de separación de voz (p. ej. model_bs_roformer_ep_317_sdr_12.9755.ckpt)")
    ap.add_argument("--model", default="", help="repo de HF que reemplaza al modelo por defecto del idioma")
    a = ap.parse_args()
    lab, root = Path(a.lab), Path(a.data)
    w, ffmpeg, results = Worker(lab), find_ffmpeg(), []
    for lang in a.langs.split(","):
        for mp3 in sorted((root / lang / "mp3").glob("*.mp3")):
            if a.songs and not any(s.lower() in mp3.stem.lower() for s in a.songs.split(",")):
                continue
            try:
                r = run_song(w, root, lang, mp3.stem, ffmpeg, lab / "cache", a.model, a.separate)
            except Exception as e:  # una canción rota no tumba el resto
                r = {"song": mp3.stem, "lang": lang, "error": str(e)}
            results.append(r)
            if "error" in r:
                print(f"{lang} {mp3.stem}: ERROR {r['error']}", flush=True)
                continue
            for v in ("energy", "no_energy"):
                m = r[v]
                print(f"{lang} {mp3.stem[:34]:<34} {v:<9} n={r['words']:<4} start MAE {m['start']['mae']:>6} med {m['start']['median_abs']:>6} bias {m['start']['bias']:>6} "
                      f"±50 {m['start']['within50']:.0%} ±100 {m['start']['within100']:.0%} | end MAE {m['end']['mae']:>6} ±100 {m['end']['within100']:.0%} | "
                      f"aprox {m['approx']:.0%} baja-conf {m['lowconf']:.0%} cero {m['zero_len']} | {m['wall_s']}s vram {m['peak_vram_mb']}", flush=True)
    print(f"worker salió {w.close()} s después de cerrar stdin", flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
