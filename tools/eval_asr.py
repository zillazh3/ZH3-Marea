"""Mide el borrador de letra (Whisper) contra la letra de referencia de JamendoLyrics, sobre la mezcla y sobre la voz separada.

    python tools/eval_asr.py [--lab <carpeta lab>] [--langs en,es] [--model large-v3-turbo] [--separate <modelo>] [--out resultados.json]

WER = distancia de edición entre palabras (minúsculas, sin signos) / palabras de la referencia. Es el WER del BORRADOR: el producto no usa
esos textos tal cual (la persona los corrige y luego se alinean), pero dice cuánto trabajo de edición hay que esperar.
"""
import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from eval_alignment import Worker  # noqa: E402
from marea.export.ffmpeg import find_ffmpeg  # noqa: E402
from marea.lab import env  # noqa: E402

LANG_NAMES = {"en": "en", "es": "es"}


def words(text: str) -> list[str]:
    t = unicodedata.normalize("NFC", text.lower())
    return re.sub(r"[^\w\s]|_", " ", t).split()


def wer(ref: list[str], hyp: list[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(hyp) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
    return d[len(hyp)] / max(1, len(ref))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default=str(env.lab_dir()))
    ap.add_argument("--data", default=str(ROOT / "tests" / "local_data" / "jamendo"))
    ap.add_argument("--langs", default="en,es")
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--separate", default="model_bs_roformer_ep_317_sdr_12.9755.ckpt")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    lab, root, ffmpeg, rows = Path(a.lab), Path(a.data), find_ffmpeg(), []
    w = Worker(lab)
    for lang in a.langs.split(","):
        for mp3 in sorted((root / lang / "mp3").glob("*.mp3")):
            ref = words((root / lang / "lyrics" / f"{mp3.stem}.txt").read_text(encoding="utf-8"))
            row = {"song": mp3.stem, "lang": lang, "ref_words": len(ref)}
            for label, sep in (("mezcla", ""), ("voz", a.separate)):
                t0 = time.perf_counter()
                try:
                    data, stages = w.ask("asr", audio=str(mp3), ffmpeg=ffmpeg, cache_dir=str(lab / "cache"), model=a.model, separate=sep)
                except Exception as e:
                    row[label] = {"error": str(e)[:200]}
                    continue
                hyp = words(" ".join(l["text"] for l in data["lines"]))
                row[label] = {"wer": round(wer(ref, hyp), 3), "lang_ok": data["language"] == lang, "language": data["language"], "lines": len(data["lines"]),
                              "suspect": sum(l["suspect"] for l in data["lines"]), "asr_s": next((s["seconds"] for s in stages if s["name"] == "asr" and s["state"] == "done"), None),
                              "wall_s": round(time.perf_counter() - t0, 1), "sample": data["lines"][0]["text"][:70] if data["lines"] else ""}
            rows.append(row)
            m, v = row.get("mezcla", {}), row.get("voz", {})
            print(f"{lang} {mp3.stem[:30]:<30} ref {row['ref_words']:>4}p | mezcla WER {m.get('wer', '—')} idioma {m.get('language')} ({m.get('asr_s')}s, {m.get('suspect')}/{m.get('lines')} sospechosas)"
                  f" | voz WER {v.get('wer', '—')} idioma {v.get('language')} ({v.get('asr_s')}s, {v.get('suspect')}/{v.get('lines')})", flush=True)
    print(f"worker salió {w.close()} s después de cerrar stdin")
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
