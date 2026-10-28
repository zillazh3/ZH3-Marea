"""Regenera tests/fixtures/engine_golden.json con el motor Python (mismo escenario y mismos frames que el fixture actual).

    python tools/gen_engine_golden.py

El fixture original salió del prototipo HTML (no incluido en el repo). Desde que el motor se separó de él (letras repartidas por
sílaba, easing del wipe, énfasis de notas sostenidas, interludios que terminan en el LOOKAHEAD) ya no hay paridad con el prototipo:
esto fija el comportamiento actual para detectar regresiones. Revisa el diff antes de commitear: un cambio aquí es un cambio visible.
`engine_refs.json` (curvas, springs, política Y, interludio puro) salió del prototipo y no se regenera.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from marea.demo_song import exact_lyrics  # noqa: E402
from marea.engine.engine import Engine, Params  # noqa: E402
from tests.test_engine_golden import flatten, measure  # noqa: E402  (mismo escenario y mismo formato que el test)

FIXTURE = ROOT / "tests" / "fixtures" / "engine_golden.json"


def rounded(v):
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, list):
        return [rounded(x) for x in v]
    if isinstance(v, dict):
        return {k: rounded(x) for k, x in v.items()}
    return v


def near_record(L):
    if L.is_gap:
        return {"k": "gap", "op": L.grp_op, "sc": L.grp_sc,
                "dots": [{"op": d.op, "y": d.y, "s": d.scale, "halo": [d.halo_r, d.halo_a] if d.halo_a else None} for d in L.dots]}
    return {"k": "line", "units": flatten(L)}


def main():
    old = json.loads(FIXTURE.read_text())
    eng = Engine(exact_lyrics(), measure, stage_h=900, font_px=42, params=Params(vib=False), now=old["now0"])
    eng.start(old["events"][0]["t"] * 1000)
    seeks = {e["frame"]: e["t"] for e in old["events"] if e["type"] == "seek"}
    keep = {f["frame"]: list(f["near"]) for f in old["frames"]}  # mismos frames y mismos ítems vecinos que el fixture anterior
    frames = []
    for frame in range(old["total"]):
        if frame in seeks:
            eng.seek(seeks[frame] * 1000)
        eng.advance(old["dt"])
        if frame not in keep:
            continue
        frames.append({"frame": frame, "active": eng.active,
                       "items": [{"y": L.y.p, "s": L.s.p, "blur": L.blur_tw.t, "op": L.op_tw.t} for L in eng.items],
                       "near": {i: near_record(eng.items[int(i)]) for i in keep[frame]}})
    out = rounded({**{k: old[k] for k in ("dt", "now0", "events", "total")}, "frames": frames})
    FIXTURE.write_text(json.dumps(out, separators=(",", ":")))
    print(FIXTURE, FIXTURE.stat().st_size, "bytes,", len(frames), "frames")


if __name__ == "__main__":
    main()
