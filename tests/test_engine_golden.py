"""El motor completo contra una referencia congelada (regresión), generada con `python tools/gen_engine_golden.py`.

El fixture nació del prototipo ejecutado con DOM falso (prototipo original, no incluido en el repo) y había paridad con él; desde que el motor
reparte las letras por sílaba, suaviza el wipe, enfatiza notas sostenidas y alinea el interludio con el LOOKAHEAD, se regenera desde
Python: un fallo aquí significa que cambió algo visible. La intención de esos cambios está en test_engine_sync.py.
Escenario (paso 1/64 s, exacto en binario): arranca en t=9 s reproduciendo, en el frame 1600
salta a 13.4 s y en el 2100 a 30 s (dentro del interludio). Se comparan, en los frames muestreados, todas las líneas (y, escala, blur, opacidad
objetivo) y las sílabas/letras/puntos de la línea activa y sus vecinas, igual que hace el prototipo.
"""
import json
from pathlib import Path

import pytest

from marea.demo_song import exact_lyrics
from marea.engine.engine import Engine, Params

G = json.loads((Path(__file__).parent / "fixtures" / "engine_golden.json").read_text())


def measure(line, active):  # las mismas alturas que el DOM falso del generador
    return 60.0 + (30.0 if active and line.bg else 0.0)


def flatten(L):
    out = []
    for w in L.words:
        if w.emph:
            out.append({"k": "word", "p": w.p})
        for u in w.parts:
            sh = [u.glow_r, u.glow_a] if u.glow_a else None
            out.append({"k": "ch", "x": u.x, "y": u.y, "s": u.scale, "sh": sh} if w.emph else
                       {"k": "u", "p": u.p, "x": u.x, "y": u.y, "s": u.scale, "sh": sh})
    return out


def check_unit(want, got, where):
    assert want["k"] == got["k"], where
    if "p" in want:
        assert got["p"] == pytest.approx(want["p"], abs=6e-4), where
    if want["k"] != "word":
        for k in "xys":
            assert got[k] == pytest.approx(want[k], abs=2e-4), (where, k)
        assert (want["sh"] is None) == (got["sh"] is None), where
        if want["sh"]:
            assert got["sh"][0] == pytest.approx(want["sh"][0], abs=0.06), where
            assert got["sh"][1] == pytest.approx(want["sh"][1], abs=1e-3), where


def check_gap(want, L, where):
    assert L.grp_op == pytest.approx(want["op"], abs=1e-3), where
    assert L.grp_sc == pytest.approx(want["sc"], abs=2e-4), where
    for i, (w, d) in enumerate(zip(want["dots"], L.dots)):
        assert d.op == pytest.approx(w["op"], abs=1e-3), (where, i)
        assert d.y == pytest.approx(w["y"], abs=2e-4), (where, i)
        assert d.scale == pytest.approx(w["s"], abs=2e-4), (where, i)
        assert (w["halo"] is None) == (d.halo_a == 0), (where, i)
        if w["halo"]:
            assert d.halo_r == pytest.approx(w["halo"][0], abs=2e-3), (where, i)
            assert d.halo_a == pytest.approx(w["halo"][1], abs=2e-3), (where, i)


def test_full_engine_matches_frozen_reference():
    dt = G["dt"]
    eng = Engine(exact_lyrics(), measure, stage_h=900, font_px=42, params=Params(vib=False), now=G["now0"])
    eng.start(G["events"][0]["t"] * 1000)
    seeks = {e["frame"]: e["t"] for e in G["events"] if e["type"] == "seek"}
    want = {f["frame"]: f for f in G["frames"]}
    checked = 0
    for frame in range(G["total"]):
        if frame in seeks:
            eng.seek(seeks[frame] * 1000)
        eng.advance(dt)
        if frame not in want:
            continue
        w, where = want[frame], f"frame {frame}"
        assert eng.active == w["active"], where
        for i, (wi, L) in enumerate(zip(w["items"], eng.items)):
            assert L.y.p == pytest.approx(wi["y"], abs=0.051), (where, i, "y")
            assert L.s.p == pytest.approx(wi["s"], abs=6e-5), (where, i, "scale")
            assert L.blur_tw.t == pytest.approx(wi["blur"], abs=6e-3), (where, i, "blur")
            assert L.op_tw.t == pytest.approx(wi["op"], abs=1e-9), (where, i, "opacity")
        for i, rec in w["near"].items():
            L = eng.items[int(i)]
            if rec["k"] == "gap":
                check_gap(rec, L, f"{where} item {i}")
            else:
                got = flatten(L)
                assert len(got) == len(rec["units"]), where
                for j, (a, b) in enumerate(zip(rec["units"], got)):
                    check_unit(a, b, f"{where} item {i} unit {j}")
        checked += 1
    assert checked == len(G["frames"]) > 100


def test_deterministic_and_warmup_settles():
    runs = []
    for _ in range(2):
        eng = Engine(exact_lyrics(), measure, 900, params=Params(vib=True))
        eng.warmup(21400)  # inicio de un dueto, con vibrato activo
        runs.append((eng.t, [(L.y.p, L.s.p) for L in eng.items], [flatten(L) for L in eng.items[:6]]))
    assert runs[0] == runs[1]
    # tras 3 s de pre-roll el scroll ya está asentado (springs del layout en reposo)
    eng = Engine(exact_lyrics(), measure, 900)
    eng.warmup(23000)  # a media línea (la anterior cambió hace 1.6 s)
    assert eng.t == pytest.approx(23.0, abs=1e-6)
    assert all(abs(L.y.p - L.y.t) < 0.5 for L in eng.items)
