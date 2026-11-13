"""Checkpoint 2 tests: fill-front coupling, carrier tint/softness, held-mode signature reshaping, and fill-front particles."""
import pytest

from marea.demo_cover import demo_cover
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.scene import Scene, Style
from marea.wordfx import library
from marea.wordfx.anim import letter_state, particles
from marea.wordfx.fxtypes import CarrierLetter, FxParams
from marea.wordfx.plan import build_plan
from marea.wordfx.signatures import eval_waveform, signature_for


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def test_per_letter_fill_front_activation():
    p = FxParams(
        seed=1, vector=(0.8, 0.45, -0.3, 0.6, 0.4, 0.2), primitives=("glow",),
        color_from=(255, 143, 163), color_to=(255, 197, 107),
        fill_tint=0.5, waveform="breath", sig_freq_hz=1.0, sig_axis="scale", sig_amp_em=0.05, is_hero=False,
    )
    # Word [1.0, 2.0]: letter 0 already filled (fill_p=1.0, onset=1.0), letter 4 not yet reached (fill_p=0.0, onset=1.8)
    c0 = CarrierLetter(fill_p=1.0, onset_s=1.0, y_em=-0.02, scale=1.05, glow_a=0.4, glow_r=8.0)
    c4 = CarrierLetter(fill_p=0.0, onset_s=1.8, y_em=0.0, scale=1.0, glow_a=0.0, glow_r=0.0)
    s0 = letter_state(p, 0, 5, 1.35, 1.0, 2.0, carrier=c0, line_act=1.0, line_end_s=4.0)
    s4 = letter_state(p, 4, 5, 1.35, 1.0, 2.0, carrier=c4, line_act=1.0, line_end_s=4.0)
    # Letter 0 carries tinted color and active motion; letter 4 stays white and unmoved until fill front arrives
    assert s0.color != (255, 255, 255)
    assert s4.color == (255, 255, 255)
    assert s0.scale > s4.scale


def test_fill_tint_stays_within_bounds(lib):
    for cat in ("time", "loss", "love"):
        sig = signature_for((cat,), (0.0,) * 6)
        assert 0.25 <= sig.fill_tint <= 0.60


def test_fill_edge_softness_differs_by_signature(lib):
    sig_time = signature_for(("time",), (0.0,) * 6)
    sig_loss = signature_for(("loss",), (0.0,) * 6)
    assert sig_time.fill_edge_softness < 1.0 < sig_loss.fill_edge_softness


def test_held_mode_reshapes_wave_by_signature(lib):
    # Held word (> 700 ms): love (breath -> scale), time (tick -> y), loss (erode -> y)
    syls = [Syllable("siem", 1000, 1700, word_end=False), Syllable("pre", 1700, 2500, word_end=True)]
    line = Line(1000, 2500, syllables=syls)
    lyrics = Lyrics([line])
    plan = build_plan(lyrics, lib, track_id="held", overrides=[])
    fx_love = plan.get(0, 0)
    assert fx_love is not None
    # Check signature waveform outputs differ across signatures
    w_breath = [eval_waveform("breath", t, 1.0, 0) for t in (1.1, 1.3, 1.5, 1.7)]
    w_tick = [eval_waveform("tick", t, 1.0, 0) for t in (1.1, 1.3, 1.5, 1.7)]
    w_sine = [eval_waveform("sine", t, 1.0, 0) for t in (1.1, 1.3, 1.5, 1.7)]
    assert w_breath != w_tick and w_tick != w_sine


def test_accent_mode_disables_particles(lib):
    # 180 ms accent word (< 250 ms)
    syls = [Syllable("perder", 1000, 1180, word_end=True), Syllable("algo", 1200, 1600, word_end=False)]
    plan = build_plan(Lyrics([Line(1000, 1600, syllables=syls)]), lib, track_id="acc", overrides=[])
    p = plan.get(0, 0).params
    assert p.duration_mode == "accent" and p.particles == 0 and p.sparkle == 0
    assert particles(p, [0.0, 0.5], [0.5, 0.5], 1.1, 1.0, 1.18) == []


def test_particles_spawn_at_fill_front_onsets():
    p = FxParams(seed=42, vector=(0.0,) * 6, primitives=("particles", "erode"), erode=0.5, particles=12, drift_em=0.2)
    xs = [0.0, 0.5, 1.0, 1.5]
    ws = [0.5] * 4
    onsets = [1.0, 1.25, 1.5, 1.75]
    # Eroded letters are indices 2 and 3 (onsets 1.5, 1.75). Before 1.5, zero particles; at 1.65, particles exist from letter 2!
    assert particles(p, xs, ws, 1.3, 1.0, 2.0, onsets=onsets) == []
    active = particles(p, xs, ws, 1.65, 1.0, 2.0, onsets=onsets)
    assert active and all(q.x_em >= 0.9 for q in active)


def test_scene_carrier_word_sync_matches_engine(lib):
    syls = [Syllable("Fue", 1000, 1300, word_end=True), Syllable("tiempo", 1300, 2500, word_end=False)]
    lyrics = Lyrics([Line(1000, 2500, syllables=syls)])
    plan = build_plan(lyrics, lib, track_id="sync", overrides=[])
    scene = Scene(lyrics, demo_cover(), (270, 480), style=Style(), wordfx=plan)
    scene.engine.warmup(1900)
    L = [item for item in scene.engine.items if not item.is_gap and not item.is_credit][0]
    w_tiempo = L.words[1]
    cw, front_u = scene._carrier_word(L, w_tiempo, scene.size)
    expected_u = sum(scene.fonts.text(u.text, scene.size)[1] * u.p for u in w_tiempo.parts) / sum(
        scene.fonts.text(u.text, scene.size)[1] for u in w_tiempo.parts
    )
    assert front_u == pytest.approx(expected_u, abs=1e-6)  # exact match to base Scene._word wipe front
    assert len(cw.letters) == len("tiempo")


def test_unsung_carrier_letters_stay_aligned_on_baseline(lib):
    # Words like "recordar" (memory -> mask_reveal/letter_offset) and "día" (time) must stay on baseline when unsung
    syls = [
        Syllable("recor", 3000, 3400, word_end=False),
        Syllable("dar", 3400, 3800, word_end=True),
        Syllable("aquel", 3850, 4200, word_end=True),
        Syllable("día", 4250, 4800, word_end=True),
    ]
    plan = build_plan(Lyrics([Line(3000, 4800, syllables=syls)]), lib, track_id="align", overrides=[])
    for wi in (0, 2):
        fx = plan.get(0, wi)
        assert fx is not None
        cl = CarrierLetter(fill_p=0.0, onset_s=3.5, y_em=0.0, scale=1.0, glow_a=0.0, glow_r=0.0)
        for li in range(len(fx.match.text)):
            st = letter_state(
                fx.params, li, len(fx.match.text), 1.5,
                fx.match.begin_ms / 1000.0, fx.match.end_ms / 1000.0,
                carrier=cl, line_act=0.0, line_end_s=4.8,
            )
            assert st.dy == 0.0 and st.dx == 0.0 and st.rot_deg == 0.0


def test_held_anguish_wobble_is_bounded(lib):
    # Held anguish phrase word like "sin salida" must not exaggerate wobble beyond gentle subtle bounds
    syls = [
        Syllable("sin", 800, 1000, word_end=True),
        Syllable("sa", 1000, 1200),
        Syllable("li", 1200, 1400),
        Syllable("da", 1400, 2200, word_end=True),
    ]
    plan = build_plan(Lyrics([Line(800, 2200, syllables=syls)]), lib, track_id="wobble", overrides=[])
    fx = plan.get(0, 1)
    assert fx is not None
    cl = CarrierLetter(fill_p=1.0, onset_s=1.2, y_em=-0.02, scale=1.02, glow_a=0.3, glow_r=8.0)
    for k in range(25):
        t = 1.1 + k * 0.05
        st = letter_state(fx.params, 2, 6, t, 1.0, 2.2, rms=0.8, carrier=cl, line_act=1.0, line_end_s=2.4)
        assert abs(st.dy - cl.y_em) <= 0.085
        assert abs(st.dx) <= 0.085
