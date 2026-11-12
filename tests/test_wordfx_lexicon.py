from marea.wordfx.lexicon import guess_lang, lemma_candidates, line_lang, normalize

KNOWN = {"es": {"tiempo", "perder"}, "en": {"again", "high", "loop"}}


def test_normalize():
    assert normalize("Lágrima") == "lagrima"
    assert normalize("Don’t!") == "don't"
    assert normalize("¿Qué?") == "que"
    assert normalize("Soñar ü") == "sonarü".replace("ü", "") + "u"
    assert normalize("!!!") == ""


def _es(word, *expected):
    cands = lemma_candidates(word, "es")
    for e in expected:
        assert e in cands, (word, e, cands)


def _en(word, *expected):
    cands = lemma_candidates(word, "en")
    for e in expected:
        assert e in cands, (word, e, cands)


def test_es_verbs():
    for w in ("perdido", "perdida", "perdidos", "perdiendo", "pierdo", "perdere"):
        _es(w, "perder")
    _es("olvidarte", "olvidar")
    _es("olvidado", "olvidar")
    _es("dimelo", "decir")
    _es("besaría", "besar")
    _es("llorando", "llorar")
    _es("durmiendo", "dormir")
    _es("durmió", "dormir")
    _es("sintió", "sentir")
    _es("cayó", "caer")
    _es("cayendo", "caer")
    _es("vuelvo", "volver")
    _es("recuerdo", "recordar", "recuerdo")
    _es("sueño", "sonar", "sueno")
    _es("fue", "ir", "ser")
    _es("estaba", "estar")
    _es("quiero", "querer")


def test_es_nouns():
    _es("perdida", "perdido")
    _es("tiempos", "tiempo")
    _es("lágrimas", "lagrima")
    _es("estrellas", "estrella")
    _es("ojos", "ojo")
    _es("besos", "beso")
    _es("cicatrices", "cicatriz")
    _es("luces", "luz")
    _es("cansada", "cansado")
    _es("gatitos", "gato")
    _es("corazoncito", "corazon")
    _es("rapidamente", "rapido")


def test_en():
    _en("lost", "lose")
    _en("crying", "cry")
    _en("tears", "tear")
    _en("dancing", "dance")
    _en("dying", "die")
    _en("stopped", "stop")
    _en("loved", "love")
    _en("cried", "cry")
    _en("wolves", "wolf")
    _en("forgotten", "forget")
    _en("dancin'", "dance")


def test_candidate_invariants():
    words = ["Perdido", "olvidarte", "dímelo", "cicatrices", "lágrimas", "estrellas", "durmiendo",
             "corazoncitos", "cayendo", "fuerzas", "stopped", "dancing", "wolves", "ok"]
    for lang in ("es", "en"):
        for w in words:
            c = lemma_candidates(w, lang)
            assert c[0] == normalize(w)
            assert len(c) < 30
            assert len(set(c)) == len(c)
            assert c == lemma_candidates(w, lang)
    assert lemma_candidates("!!!", "es") == []


def test_guess_lang_known():
    for w in ("again", "high", "loop"):
        assert guess_lang(w, KNOWN) == "en"
    for w in ("tiempo", "perdido"):
        assert guess_lang(w, KNOWN) == "es"


def test_guess_lang_heuristics():
    assert guess_lang("cardio") == "es"
    assert guess_lang("cardio", fallback="en") == "en"
    assert guess_lang("the") == "en"
    assert guess_lang("el") == "es"
    assert guess_lang("niño") == "es"
    assert guess_lang("canción") == "es"
    assert guess_lang("running") == "en"
    assert guess_lang("don't") == "en"
    for shared in ("no", "a", "me", "la"):
        assert guess_lang(shared, fallback="en") == "en"
        assert guess_lang(shared, fallback="es") == "es"


def test_line_lang():
    assert line_lang(["Fue", "tanto", "el", "tiempo", "perdido"]) == "es"
    assert line_lang(["I", "will", "be", "there"]) == "en"
    assert line_lang(["Fue", "tanto", "again", "el", "tiempo"], KNOWN) == "es"
    assert line_lang([]) == "es"


def test_suponer_irregulars():
    for w in ("supongo", "supuse", "supones", "supusieron"):
        _es(w, "suponer")
