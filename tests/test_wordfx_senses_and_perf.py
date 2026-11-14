"""Tests for expanded bilingual semantic intelligence, contextual senses, 32 motion signatures, and UI/render optimizations."""
import pytest

from marea.model.lyrics import Line, Lyrics, Syllable
from marea.wordfx import lexicon, library, matcher, rules
from marea.wordfx.plan import build_plan
from marea.wordfx.signatures import SIGNATURES


def _line(text: str, start: int = 0, step: int = 400) -> Line:
    words = text.split()
    syls = [Syllable(w, start + i * step, start + (i + 1) * step - 20, word_end=True) for i, w in enumerate(words)]
    return Line(start, start + max(1, len(words)) * step, "v1", syls)


def _match(lib, rls, text: str, max_fx: int = 5):
    return matcher.match_line(_line(text), 0, lib, max_fx=max_fx, rules=rls)


@pytest.fixture(scope="module")
def lib():
    return library.load()


@pytest.fixture(scope="module")
def rls():
    return rules.load()


def test_all_32_categories_have_bespoke_motion_signatures(lib):
    missing = set(lib.categories) - set(SIGNATURES)
    assert missing == set(), f"Missing MotionSignatures for categories: {sorted(missing)}"
    assert len(SIGNATURES) >= 32


@pytest.mark.parametrize("word,lang,expected", [
    ("tristisima", "es", "triste"),
    ("altisimo", "es", "alto"),
    ("crezco", "es", "crecer"),
    ("abrace", "es", "abrazar"),
    ("sintio", "es", "sentir"),
    ("muriendo", "es", "morir"),
    ("suelto", "es", "soltar"),
    ("deshice", "es", "deshacer"),
    ("cayeron", "es", "caer"),
    ("sank", "en", "sink"),
    ("froze", "en", "freeze"),
    ("clung", "en", "cling"),
    ("thrown", "en", "throw"),
])
def test_expanded_bilingual_morphology_candidates(word, lang, expected):
    assert expected in lexicon.lemma_candidates(word, lang)


def test_contextual_senses_bilingual_disambiguation(lib, rls):
    love_burn = {m.lemma: m for m in _match(lib, rls, "arder en tu piel y pecho")}
    assert love_burn["arder"].categories == ("love", "fire")
    assert love_burn["arder"].note == "sense:arder_pasion"
    assert love_burn["arder"].ambiguity == 0.15

    anger_burn = {m.lemma: m for m in _match(lib, rls, "arder con tanta rabia")}
    assert anger_burn["arder"].categories == ("anger", "fire")
    assert anger_burn["arder"].note == "sense:arder_rabia"

    anguish_burn = {m.lemma: m for m in _match(lib, rls, "todo el mar se apaga y arde en ceniza")}
    assert anguish_burn["arder"].categories == ("anguish", "fire")
    assert anguish_burn["arder"].note == "sense:arder_angustia"

    en_burn = {m.lemma: m for m in _match(lib, rls, "burn in your kiss tonight")}
    assert en_burn["burn"].categories == ("love", "fire")
    assert en_burn["burn"].note == "sense:burn_passion"

    grief_fall = {m.lemma: m for m in _match(lib, rls, "caer en el abismo y llanto")}
    assert grief_fall["caer"].categories == ("loss", "sadness")

    nature_fall = {m.lemma: m for m in _match(lib, rls, "caer suave como lluvia y hoja")}
    assert nature_fall["caer"].categories == ("calm", "autumn")

    resilience_fall = {m.lemma: m for m in _match(lib, rls, "levantar tras caer")}
    assert resilience_fall["caer"].categories == ("growth", "healing")

    empty_longing = {m.lemma: m for m in _match(lib, rls, "llenar este vacio en el pecho")}
    assert empty_longing["vacio"].categories == ("anguish", "longing")

    empty_absence = {m.lemma: m for m in _match(lib, rls, "vacio en la cama sin dormir")}
    assert empty_absence["vacio"].categories == ("loss", "sadness")

    return_heal = {m.lemma: m for m in _match(lib, rls, "volver a casa para abrazar")}
    assert return_heal["volver"].categories == ("longing", "healing")

    return_mem = {m.lemma: m for m in _match(lib, rls, "volver al ayer del espejo")}
    assert return_mem["volver"].categories == ("longing", "memory")

    en_sleep = {m.lemma: m for m in _match(lib, rls, "sleep softly beside peace")}
    assert "tenderness" in en_sleep["sleep"].categories

    en_dance = {m.lemma: m for m in _match(lib, rls, "dance alone in the sad rain")}
    assert en_dance["dance"].categories == ("sadness", "longing")


def test_clitic_bridged_negation_preserves_distance3_non_clitic_boundary(lib, rls):
    assert _match(lib, rls, "no me dejes caer") == []  # sadness cancelled via clitic "me"
    # Main verb at distance 3 must remain unnegated (contract with test_wordfx_rules.py)
    ms = _match(lib, rls, "no puedo ya respirar")
    assert [(m.lemma, m.categories) for m in ms] == [("respirar", ("calm",))]


@pytest.mark.parametrize("text,expected_cats", [
    ("amor de cine", ("love", "photo")),
    ("movie love", ("love", "photo")),
    ("cabeza fria", ("winter", "calm")),
    ("cool head", ("winter", "calm")),
    ("quemar los barcos", ("growth", "fire")),
    ("burning bridges", ("loss", "fire")),
    ("de cero", ("growth", "healing")),
    ("from scratch", ("growth", "healing")),
    ("bailar lento", ("tenderness", "love")),
    ("slow dance", ("tenderness", "love")),
])
def test_bilingual_artist_phrases_match(lib, rls, text, expected_cats):
    ms = _match(lib, rls, text)
    assert len(ms) == 1
    assert set(ms[0].categories) == set(expected_cats)
    assert ms[0].note.startswith("phrase:")


@pytest.mark.parametrize("text", [
    "a negative result",
    "la bandera nacional",
    "spring cleaning",
    "la presa hidraulica",
])
def test_false_positive_pos_guards_and_exclusions(lib, rls, text):
    assert _match(lib, rls, text) == []


def test_editor_preview_hot_swaps_wordfx_without_scene_rebuild(make_window, qapp, monkeypatch, tmp_path):
    from marea.wordfx import library as lib_mod, rules as rules_mod, songs as songs_mod
    monkeypatch.setattr(lib_mod, "user_path", lambda: tmp_path / "library.json")
    monkeypatch.setattr(rules_mod, "user_path", lambda: tmp_path / "rules.json")
    monkeypatch.setattr(songs_mod, "path_for", lambda tid: tmp_path / f"{tid}.json")
    win = make_window()
    win.show()
    win.load_demo()
    qapp.processEvents()
    e = win.editor
    e.settings.setValue("experimental/wordfx", True)
    e._wordfx_sync()
    scene_before = e.preview.scene
    plan_before = e._fx_plan
    assert scene_before is not None and plan_before is not None

    # Moving intensity slider swaps the plan in-place without rebuilding Scene
    e.sl_fx.setValue(45)
    e._wordfx_sync()
    assert e.preview.scene is scene_before
    assert e._fx_plan is not plan_before
    assert e._fx_plan.intensity == pytest.approx(0.45)

    # Toggling debug checkbox keeps BOTH Scene AND FxPlan identical
    plan_mid = e._fx_plan
    e.cb_fxdebug.setChecked(True)
    e._wordfx_sync()
    assert e.preview.scene is scene_before
    assert e._fx_plan is plan_mid
    assert scene_before.wordfx_debug is True

    # Switching project lyrics must rebuild FxPlan and Scene cleanly (no stale debug short-circuit)
    from dataclasses import replace as dc_replace
    new_proj = dc_replace(win.editor.project, lyrics=Lyrics([_line("volver a casa para abrazar", start=0)]))
    e.set_project(new_proj)
    assert e._fx_plan is not plan_mid
    assert e.preview.scene is not scene_before


def test_negated_co_category_does_not_leak_into_senses(lib, rls):
    # "rabia" is cancelled by "sin" and must NOT cause "arder" to resolve as anger ("arder_rabia")
    ms = {m.lemma: m for m in _match(lib, rls, "sin rabia puede arder")}
    assert "rabia" not in ms
    assert ms["arder"].note != "sense:arder_rabia"
    assert ms["arder"].categories == ("fire",)


def test_build_plan_reuses_passed_rules_and_matches(lib, rls):
    lyrics = Lyrics([
        _line("quiero arder en tu piel y pecho", start=0),
        _line("volver a casa para abrazar", start=2500),
    ])
    raw_matches = matcher.match_lyrics(lyrics, lib, rules=rls)
    p1 = build_plan(lyrics, lib, intensity=0.8)
    p2 = build_plan(lyrics, lib, intensity=0.8, rules=rls, matches=raw_matches)
    assert set(p1.words) == set(p2.words)
    for k in p1.words:
        assert p1.words[k].params == p2.words[k].params


def test_clitic_bridge_and_polysemy_before_negation(lib, rls):
    # Distance-3 negation requires an actual grammatical bridge, not any random word
    ms_bad = {m.lemma: m for m in _match(lib, rls, "no me perro amor")}
    assert "amar" in ms_bad or "amor" in ms_bad
    assert all(not m.note.startswith("neg:") for m in ms_bad.values())

    # Valid Spanish clitic + modal/causative bridge and English multiword negation work
    ms_es = {m.lemma: m for m in _match(lib, rls, "no me dejes llorar")}
    assert ms_es["llorar"].note == "neg:no"
    assert "dryness" in ms_es["llorar"].categories
    ms_en = {m.lemma: m for m in _match(lib, rls, "never to feel love")}
    assert "love" not in ms_en
    ms_en_cry = {m.lemma: m for m in _match(lib, rls, "not even to cry")}
    assert ms_en_cry["cry"].note == "neg:not"
    assert "dryness" in ms_en_cry["cry"].categories

    # Negated polysemous word resolves sense from unnegated neighbor before negation runs
    ms_poly = {m.lemma: m for m in _match(lib, rls, "no arder de rabia")}
    assert "arder" not in ms_poly
    assert "rabia" in ms_poly


def test_irregular_possessive_lemma():
    cands = lexicon.lemma_candidates("children's", "en")
    assert "child" in cands


def test_scene_attach_wordfx_mismatched_lyrics_safe():
    import skia
    from marea.render.scene import Scene
    cover = skia.Surface(8, 8).makeImageSnapshot()
    lyrics = Lyrics([_line("volver a casa", start=0)])
    sc = Scene(lyrics, cover, size=(128, 128))
    other = Lyrics([_line("otro verso distinto", start=0)])
    sc.attach_wordfx(other, None)
    assert sc.matches_lyrics(other)


def test_ambiguous_humbe_terms_and_literal_pos_guards(lib, rls):
    fuego_amor = {m.lemma: m for m in _match(lib, rls, "fuego en tu piel y beso")}
    assert fuego_amor["fuego"].categories == ("love", "fire")
    assert fuego_amor["fuego"].note == "sense:fuego_pasion"

    fuego_ruina = {m.lemma: m for m in _match(lib, rls, "el fuego dejo cenizas y ruina")}
    assert fuego_ruina["fuego"].categories == ("loss", "fire")
    assert fuego_ruina["ceniza"].categories == ("loss", "sadness")

    ceniza_fenix = {m.lemma: m for m in _match(lib, rls, "como fenix de las cenizas")}
    assert ceniza_fenix["ceniza"].categories == ("growth", "fire")
    assert ceniza_fenix["ceniza"].note == "sense:ceniza_renacer"

    marea_calma = {m.lemma: m for m in _match(lib, rls, "marea suave en la orilla")}
    assert marea_calma["marea"].categories == ("calm", "water")
    marea_alta_ms = {m.lemma: m for m in _match(lib, rls, "me ahoga esta marea")}
    assert marea_alta_ms["marea"].categories == ("anguish", "water")

    veneno_dulce = {m.lemma: m for m in _match(lib, rls, "dulce veneno en tus labios")}
    assert veneno_dulce["veneno"].categories == ("love", "intoxication")
    veneno_dano = {m.lemma: m for m in _match(lib, rls, "veneno lleno de traicion")}
    assert veneno_dano["veneno"].categories == ("anger", "sadness")

    sirena_mito = {m.lemma: m for m in _match(lib, rls, "canto de sirena en el mar")}
    assert sirena_mito["sirena"].categories == ("longing", "water")
    assert "calm" not in sirena_mito["sirena"].categories
    sirena_alerta = {m.lemma: m for m in _match(lib, rls, "suena una sirena de policia")}
    assert sirena_alerta["sirena"].categories == ("fear", "anguish")

    # Literal POS guards suppress false emotion triggers
    assert _match(lib, rls, "sacando pecho") == []
    assert _match(lib, rls, "opened an old wooden chest") == []
    assert _match(lib, rls, "una vena mineral") == []

    # Poetic chest/vein contexts activate expected senses
    pecho_dolor = {m.lemma: m for m in _match(lib, rls, "duele la herida en el pecho")}
    assert pecho_dolor["pecho"].categories == ("anguish", "sadness")
    vena_amor = {m.lemma: m for m in _match(lib, rls, "corres fuego por mis venas")}
    assert vena_amor["vena"].categories == ("love", "fire")
