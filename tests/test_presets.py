"""Corrientes: formato, validación, aplicación parcial, almacenamiento y render de Bajamar."""
import hashlib
import json

import pytest

from marea.engine.engine import Params
from marea.model.background import BackgroundConfig
from marea.presets import schema
from marea.presets.schema import FIELDS, SECTIONS, FxSettings, PresetError, apply, capture, validate
from marea.presets.store import MAX_BYTES, PresetStore, load_file
from marea.render.scene import Style

BUILTIN_NAMES = ["Bajamar", "Pleamar", "Niebla", "Abisal"]


def defaults():
    return Style(title="T", artist="A"), Params(), BackgroundConfig(), FxSettings()


def target(objs, tgt):
    return dict(zip(("style", "params", "bg", "fx"), objs))[tgt]


def non_default_look():
    """Un look con TODOS los campos de la lista blanca distintos del valor por omisión."""
    objs = defaults()
    for key, (_sec, tgt, kind, extra) in FIELDS.items():
        obj, cur = target(objs, tgt), getattr(target(objs, tgt), key)
        if kind == "num":
            lo, hi = extra
            v = lo + (hi - lo) * 0.37
            v = v if abs(v - cur) > 1e-6 else lo + (hi - lo) * 0.81
        elif kind == "bool":
            v = not cur
        elif kind == "choice":
            v = [c for c in extra if c != cur and c != "video"][-1]
        elif kind == "rgb":
            v = (10, 20, 30)
        elif kind == "rgb_or_none":
            v = (1, 2, 3)
        elif kind == "font":
            v = "Inter Tight"
        else:
            v = "marca"
        setattr(obj, key, v)
    return objs


def test_every_field_is_covered_and_sections_are_consistent():
    assert {s for s, *_ in FIELDS.values()} == set(SECTIONS)
    for key, (_sec, tgt, _k, _e) in FIELDS.items():
        assert hasattr(target(defaults(), tgt), key), key
    assert not {"video", "path", "video_size", "video_mtime_ns", "trim_in_ms", "trim_out_ms", "offset_ms"} & set(FIELDS)


def test_style_to_preset_to_style_is_lossless():
    look = non_default_look()
    raw = capture(*look, name="x")
    back = validate(json.loads(json.dumps(raw)), installed_fonts={"Inter Tight"})  # pasa por JSON, como en disco
    assert back.flags == []
    out = apply(back.preset, *defaults())
    for key, (_sec, tgt, kind, _e) in FIELDS.items():
        want, got = getattr(target(look, tgt), key), getattr(target(out, tgt), key)
        assert got == pytest.approx(want) if kind == "num" else got == want, key


def test_bajamar_is_the_default_look_and_renders_identically():
    info = PresetStore().get("Bajamar")
    assert info and info.builtin
    style, params, bg, fx = apply(info.preset, *defaults())
    assert (style, params, bg, fx) == defaults()
    assert PresetStore().get("bajamar").name == "Bajamar"  # el nombre no distingue mayúsculas


def frame_hashes(style, params, bg, n=3):
    from marea.demo_cover import demo_cover
    from marea.demo_song import exact_lyrics
    from marea.export.job import ExportJob, render_frames
    job = ExportJob(exact_lyrics(), bytes(demo_cover().encodeToData()), "x.mp4", 12500, 12500 + n / 30 * 1000, size=(180, 320),
                    fps=30, prefer_gpu=False, params=params, style=style, background=bg)
    return [hashlib.sha256(f.tobytes()).hexdigest() for f in render_frames(job)]


def test_bajamar_frames_match_the_default_frames_exactly():
    base = defaults()
    applied = apply(PresetStore().get("Bajamar").preset, *base)
    assert frame_hashes(*base[:3]) == frame_hashes(*applied[:3])


def test_other_builtins_change_the_picture_and_all_validate():
    store = PresetStore()
    assert [i.name for i in store.list() if i.builtin] == BUILTIN_NAMES
    ref = frame_hashes(*defaults()[:3], n=1)
    for name in BUILTIN_NAMES[1:]:
        look = apply(store.get(name).preset, *defaults())
        assert frame_hashes(*look[:3], n=1) != ref, name


def test_partial_apply_touches_only_selected_sections():
    store = PresetStore()
    abisal = store.get("Abisal").preset
    base = defaults()
    s, p, b, f = apply(abisal, *base, sections=("color",))
    assert s.text_rgb != base[0].text_rgb and s.glow_rgb is not None
    assert (s.bg_veil, s.bg_sat, s.font_frac, p, b, f) == (base[0].bg_veil, base[0].bg_sat, base[0].font_frac, base[1], base[2], base[3])
    s2, p2, *_ = apply(abisal, *base, sections=("fondo",))
    assert s2.bg_veil > base[0].bg_veil and s2.text_rgb == base[0].text_rgb and p2 == base[1]
    none = apply(abisal, *base, sections=())
    assert none == base
    for o_in, o_out in zip(base, none):
        assert o_in is not o_out  # nunca modifica los objetos de entrada


def test_apply_never_brings_a_video_and_falls_back_without_one():
    p = capture(*defaults())
    p["fondo"]["source"] = "video"
    p["fondo"]["video"] = {"path": "C:/evil.mp4"}
    p["fondo"]["path"] = "C:/evil.mp4"
    v = validate(p)
    assert "video" not in v.preset["fondo"] and "path" not in v.preset["fondo"]
    _, _, bg, _ = apply(v.preset, *defaults())
    assert bg.source == "cover" and bg.video is None  # sin video propio en el proyecto no hay fondo de video


# ---------------- validación ----------------

def test_validation_clamps_fixes_types_and_ignores_unknown_keys():
    raw = {"schema": "marea.preset", "version": 1, "name": "X" * 200, "mystery": 1,
           "tipografia": {"font_frac": 99, "extra": 1}, "color": {"text_rgb": "rojo", "glow_gain": "mucho", "bloom": "si",
                                                                 "dim_alpha": -3, "glow_rgb": "#12ab34"},
           "fondo": {"source": "hacker", "bg_blur": float("inf"), "zoom": True}, "efectos": "no soy un objeto",
           "marca_de_agua": {"wm_text": "x" * 500, "wm_side": "up", "wm_alpha": 7}}
    v = validate(raw).preset
    assert len(v["name"]) == 60 and "mystery" not in v and "extra" not in v["tipografia"]
    assert v["tipografia"]["font_frac"] == 0.10 and v["color"]["text_rgb"] == "#FFFFFF" and v["color"]["glow_gain"] == 1.0
    assert v["color"]["bloom"] is True and v["color"]["dim_alpha"] == 0.05 and v["color"]["glow_rgb"] == "#12AB34"
    assert v["fondo"]["source"] == "cover" and v["fondo"]["bg_blur"] == 1.0 and v["fondo"]["zoom"] == 1.0
    assert v["efectos"] == {} and len(v["marca_de_agua"]["wm_text"]) == 60
    assert v["marca_de_agua"]["wm_side"] == "left" and v["marca_de_agua"]["wm_alpha"] == 1.0


def test_validation_rejects_wrong_schema_and_future_versions():
    for bad in ([], "x", {"schema": "otra"}, {"schema": "marea.preset", "version": 2}, {"schema": "marea.preset", "version": "x"},
                {"schema": "marea.preset", "version": 0}):
        with pytest.raises(PresetError):
            validate(bad)


def test_missing_font_is_flagged_and_falls_back_to_the_bundled_one():
    raw = {"schema": "marea.preset", "version": 1, "tipografia": {"font_family": "Comic Sans Fantasma"}}
    v = validate(raw)
    assert v.preset["tipografia"]["font_family"] == "" and any("Comic Sans Fantasma" in f for f in v.flags)
    ok = validate(raw, installed_fonts={"Comic Sans Fantasma"})
    assert ok.preset["tipografia"]["font_family"] == "Comic Sans Fantasma" and not ok.flags
    assert not validate({"schema": "marea.preset", "version": 1, "tipografia": {"font_family": "Inter Tight"}}).flags


def test_oversized_file_is_rejected_without_reading_it(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"schema": "marea.preset", "version": 1, "name": "x", "pad": "y" * (MAX_BYTES + 10)}))
    with pytest.raises(PresetError, match="256 KB"):
        load_file(big)
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe no es json")
    with pytest.raises(PresetError):
        load_file(bad)


def test_migration_hook_exists_for_a_future_version(monkeypatch):
    assert schema.MIGRATIONS == {}
    monkeypatch.setattr(schema, "VERSION", 2)
    monkeypatch.setitem(schema.MIGRATIONS, 1, lambda d: {**d, "version": 2, "tipografia": {"font_frac": 0.05}})
    v = validate({"schema": "marea.preset", "version": 1})
    assert v.preset["tipografia"]["font_frac"] == 0.05 and v.preset["version"] == 2


# ---------------- almacenamiento ----------------

@pytest.fixture
def store(tmp_path):
    return PresetStore(user=tmp_path / "user")


def test_builtins_are_read_only_and_user_presets_crud(store):
    look = capture(*non_default_look())
    with pytest.raises(PresetError):
        store.delete("Bajamar")
    with pytest.raises(PresetError):
        store.rename("Bajamar", "Otra")
    with pytest.raises(PresetError):
        store.update("Bajamar", look)
    with pytest.raises(PresetError):
        store.save("bajamar", look)  # no se puede esconder una integrada
    a = store.save("Mi look", look)
    assert a.path.parent == store.user_dir and not a.builtin
    with pytest.raises(PresetError):
        store.save("MI LOOK", look)
    assert [i.name for i in store.list()][-1] == "Mi look" and store.get("mi look")
    b = store.rename("Mi look", "Renombrada")
    assert store.get("Mi look") is None and store.get("Renombrada").path == b.path
    d = store.duplicate("Renombrada")
    assert d.name == "Renombrada (copia)" and store.duplicate("Renombrada").name == "Renombrada (copia) 2"
    store.update("Renombrada", capture(*defaults()))
    assert store.get("Renombrada").preset["color"]["glow_gain"] == 1.0
    store.delete("Renombrada")
    assert store.get("Renombrada") is None and store.get("Bajamar")


def test_import_export_roundtrip_and_hostile_import(store, tmp_path):
    store.save("Original", capture(*non_default_look()))
    out = tmp_path / "out.json"
    store.export_file("Original", out)
    imp = store.import_file(out, installed_fonts={"Inter Tight"})
    assert imp.name == "Original (2)" and imp.preset["color"] == store.get("Original").preset["color"]
    hostile = tmp_path / "h.json"
    hostile.write_text(json.dumps({"schema": "marea.preset", "version": 1, "name": "../../evil", "fondo": {"path": "C:/x.mp4"}}))
    h = store.import_file(hostile)
    assert h.path.parent == store.user_dir and ".." not in h.path.name  # el nombre nunca sale de la carpeta de Corrientes
    assert "path" not in h.preset["fondo"]


def test_invalid_user_files_are_skipped_not_fatal(store):
    store.user_dir.mkdir(parents=True)
    (store.user_dir / "roto.json").write_text("{no es json")
    assert [i.name for i in store.list()] == BUILTIN_NAMES


def test_signature_changes_only_when_the_look_changes(store):
    store.save("A", capture(*defaults()))
    before = store.get("A").signature
    store.update("A", capture(*defaults()))  # otra fecha, mismo look
    assert store.get("A").signature == before
    store.rename("A", "B")  # otro nombre, mismo look
    assert store.get("B").signature == before
    store.update("B", capture(*non_default_look()))
    assert store.get("B").signature != before


def test_modified_indicator_compares_only_selected_keys():
    base = defaults()
    info = PresetStore().get("Abisal")
    now = schema.current_values(*base)
    assert set(schema.changed_sections(info.preset, now)) >= {"color", "fondo"}
    s, p, b, f = apply(info.preset, *base)
    assert schema.changed_sections(info.preset, schema.current_values(s, p, b, f)) == []
    s.glow_gain += 0.5
    assert schema.changed_sections(info.preset, schema.current_values(s, p, b, f)) == ["color"]


def test_render_thumb_writes_a_png_of_the_thumb_size(tmp_path):
    from marea.presets.thumbs import THUMB_SIZE, ensure_thumb

    path = ensure_thumb(PresetStore().get("Abisal"), tmp_path)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(data[16:20], "big") == THUMB_SIZE[0] and int.from_bytes(data[20:24], "big") == THUMB_SIZE[1]
