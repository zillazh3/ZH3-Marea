"""Perfil de ánimo + tensión vocal de punta a punta: audio sintético -> AudioStats -> perfil -> plan -> píxeles; guardado y panel."""
import json

import numpy as np
import pytest
import skia

from marea.demo_cover import demo_cover
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.scene import Scene, Style
from marea.wordfx import audiofeat, library, songs
from marea.wordfx.fxtypes import AudioStats
from marea.wordfx.plan import build_plan
from marea.wordfx.profile import ArcSegment, SongProfile, classify


@pytest.fixture(scope="module")
def lib():
    return library.load(library.BUNDLED)


def line_of(text: str, start=1000, per_word=500, last_ms=1200) -> Line:
    ws = text.split()
    syls = [Syllable(w, start + i * per_word, start + (i + 1) * per_word, word_end=i < len(ws) - 1) for i, w in enumerate(ws)]
    syls[-1].end = syls[-1].begin + last_ms
    return Line(syls[0].begin, syls[-1].end, syllables=syls)


LYRICS = Lyrics([line_of("no puedo respirar", 1000), line_of("me ahogo en la tormenta", 6000), line_of("lloro sin ti", 11000)])


def strained_audio(seconds=14, rate=48000):
    """Voz cruda sintética: armónicos agudos, ráfagas de ruido y picos fuertes."""
    rng = np.random.default_rng(3)
    t = np.arange(int(seconds * rate)) / rate
    sig = sum(np.sin(2 * np.pi * 330 * k * t) / k ** 0.4 for k in range(1, 14))
    sig = sig * (0.6 + 0.5 * np.abs(np.sin(2 * np.pi * 1.3 * t))) + 0.8 * rng.standard_normal(len(t)) * (np.sin(2 * np.pi * 7 * t) > 0.2)
    return (0.5 * sig / np.abs(sig).max()).astype(np.float32)[:, None].repeat(2, axis=1)


def test_profile_baseline_and_raw_vocals_reach_the_composer(lib):
    sad = SongProfile("t", ["sad"], [ArcSegment(0, 20, "sad")], 0.5)
    joy = SongProfile("t", ["joyful"], [ArcSegment(0, 20, "joyful")], 0.5)
    raw = SongProfile("t", ["anguished", "raw_vocals"], [ArcSegment(0, 20, "anguished")], 0.9)
    plain = build_plan(LYRICS, lib, track_id="t", overrides=[])
    a, b = (build_plan(LYRICS, lib, track_id="t", overrides=[], profile=p).get(0, 2).params for p in (sad, joy))
    assert a.vector[0] < b.vector[0] and a.vector[3] < b.vector[3]  # triste más frío y apagado que alegre
    assert plain.get(0, 2).params.strain_gain == 0
    assert build_plan(LYRICS, lib, track_id="t", overrides=[], profile=raw).get(0, 2).params.strain_gain > 0


def test_strain_curve_travels_in_the_plan(lib):
    stats = AudioStats(strain=0.8, strain_curve=np.array([0.0, 0.5, 1.0], np.float32), strain_rate=1.0)
    plan = build_plan(LYRICS, lib, track_id="t", overrides=[], audio_stats=stats)
    assert (plan.strain_at(0.2), plan.strain_at(1.5), plan.strain_at(99)) == (0.0, 0.5, 1.0)
    assert build_plan(LYRICS, lib, track_id="t", overrides=[]).strain_at(1.0) == 0.0


def test_end_to_end_strained_audio_becomes_raw_vocals_and_changes_the_frame(lib):
    stats = audiofeat.analyze(strained_audio(), 48000)
    assert stats.strain > 0.4 and stats.strain_curve is not None
    profile = classify(LYRICS, lib, stats, track_id="t")
    assert "raw_vocals" in profile.tags or stats.strain < 0.6  # el umbral es del perfil: no se fuerza aquí
    profile = SongProfile("t", profile.tags + ([] if "raw_vocals" in profile.tags else ["raw_vocals"]), profile.arc, profile.intensity)
    hot = AudioStats(strain=1.0, strain_curve=np.ones(2000, np.float32))
    cold = AudioStats(strain=0.0, strain_curve=np.zeros(2000, np.float32))

    def frame(audio):
        plan = build_plan(LYRICS, lib, track_id="t", overrides=[], profile=profile, audio_stats=audio)
        s = Scene(LYRICS, demo_cover(), (270, 480), style=Style(), wordfx=plan)
        return s.snapshot(2400).toarray(colorType=skia.kRGBA_8888_ColorType)

    assert np.array_equal(frame(hot), frame(hot))  # determinista
    assert not np.array_equal(frame(hot), frame(cold))  # la tensión alta se ve (aberración y temblor)


def test_profile_is_saved_next_to_the_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(songs, "path_for", lambda tid: tmp_path / f"{tid}.json")
    (tmp_path / "t1.json").write_text(json.dumps({"overrides": [{"lemma": "tiempo", "disable": True}]}), encoding="utf-8")
    prof = SongProfile("t1", ["sad", "tribute"], [ArcSegment(0, 30, "sad")], 0.6, "notas")
    songs.save_profile("t1", prof)
    raw = json.loads((tmp_path / "t1.json").read_text(encoding="utf-8"))
    assert raw["overrides"] and raw["profile"]["tags"] == ["sad", "tribute"]  # los overrides se conservan
    assert songs.load_profile("t1").to_dict() == prof.to_dict() and len(songs.load("t1")) == 1
    assert songs.load_profile("otra") is None
    raw["profile"]["tags"] = ["nope"]
    (tmp_path / "t1.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(songs.SongError, match="perfil"):
        songs.load_profile("t1")


def test_profile_dialog_edits_tags_and_keeps_the_arc_contiguous(qapp):
    from marea.ui.wordfx_profile import ProfileDialog
    prof = SongProfile("t", ["sad"], [ArcSegment(0, 40, "sad"), ArcSegment(40, 90, "joyful")], 0.5, "n")
    d = ProfileDialog(prof, 90, lambda notes: SongProfile("t", ["calm"], [ArcSegment(0, 90, "calm")], 0.2, notes), "Demo")
    d.tag_buttons["tribute"].setChecked(True)
    d.tag_buttons["sad"].setChecked(False)
    d.timeline.sel = 0
    d._load_selected()
    d.sp_end.setValue(50)
    d._bounds_edited()  # mover el fin arrastra el inicio del siguiente
    d._split()
    d.cmb_mood.setCurrentIndex(d.cmb_mood.findData("calm"))
    d._mood_edited()
    out = d.result_profile()
    assert out.tags == ["tribute"] and [(s.start, s.end) for s in out.arc] == [(0, 25), (25, 50), (50, 90)]
    assert [s.mood for s in out.arc] == ["calm", "sad", "joyful"]  # el tramo elegido (el primero) pasó a calma
    assert SongProfile.from_dict(out.to_dict()).arc[-1].end == 90  # lo editado sigue siendo un perfil válido
    d._merge()
    d.notes.setPlainText("nota nueva")
    d._recalculate()
    assert d.result_profile().tags == ["calm"] and d.notes.toPlainText() == "nota nueva"


def test_editor_builds_a_profile_and_saves_edits(make_window, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from marea.ui import wordfx_profile
    from marea.wordfx import rules
    monkeypatch.setattr(library, "user_path", lambda: tmp_path / "library.json")
    monkeypatch.setattr(rules, "user_path", lambda: tmp_path / "rules.json")
    monkeypatch.setattr(songs, "path_for", lambda tid: tmp_path / f"{tid}.json")
    w = make_window()
    w.show()
    w.load_demo()
    qapp.processEvents()
    e = w.editor
    e.settings.setValue("experimental/wordfx", True)
    e._wordfx_sync()
    assert e._fx_profile is not None and e._fx_plan is not None and e._fx_profile.arc  # calculado automáticamente
    assert not list(tmp_path.glob("*.json"))  # lo calculado no se guarda hasta que la persona lo acepta

    def fake_exec(self):
        self.tag_buttons["tribute"].setChecked(True)
        return QDialog.Accepted

    monkeypatch.setattr(wordfx_profile.ProfileDialog, "exec", fake_exec)
    e._open_fx_profile()
    saved = list(tmp_path.glob("*.json"))
    assert len(saved) == 1 and "tribute" in json.loads(saved[0].read_text(encoding="utf-8"))["profile"]["tags"]
    assert e._fx_profile.has("tribute")  # el plan se rehízo con el perfil guardado
