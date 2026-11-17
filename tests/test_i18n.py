import json

import pytest

from marea import i18n
from marea.ui import lang


@pytest.fixture
def cat(monkeypatch, tmp_path):
    """Isolated catalogs: a tiny es/en pair in a temp dir."""
    (tmp_path / "es.json").write_text(json.dumps({"_meta": {"name": "Español"}, "a": {"hi": "Hola {name}", "n": {"one": "{count} verso", "other": "{count} versos"}, "only_es": "solo"}}), encoding="utf-8")
    (tmp_path / "en.json").write_text(json.dumps({"_meta": {"name": "English"}, "a": {"hi": "Hi {name}", "n": {"one": "{count} line", "other": "{count} lines"}, "only_en": "fallback"}}), encoding="utf-8")
    monkeypatch.setattr(i18n, "DIR", tmp_path)
    monkeypatch.setattr(i18n, "_catalogs", {})
    monkeypatch.setattr(i18n, "_names", {})
    return tmp_path


def test_lookup_interpolation_and_plurals(cat):
    i18n.set_language("es")
    assert i18n.t("a.hi", name="Ana") == "Hola Ana"
    assert i18n.t("a.n", count=1) == "1 verso" and i18n.t("a.n", count=3) == "3 versos"
    i18n.set_language("en")
    assert i18n.t("a.n", count=1) == "1 line" and i18n.t("a.n", count=0) == "0 lines"


def test_fallback_chain_never_blank_or_raises(cat):
    i18n.set_language("es")
    assert i18n.t("a.only_en") == "fallback"  # missing in es -> English
    assert i18n.t("nope.key") == "nope.key"  # missing everywhere -> the key
    assert i18n.t("a.hi") == "Hola {name}"  # missing param stays visible instead of raising
    assert i18n.t("a.hi", name="x", extra=1) == "Hola x"


def test_broken_or_missing_locale_degrades(cat):
    (cat / "es.json").write_text("{not json", encoding="utf-8")
    i18n.set_language("es")
    assert i18n.t("a.hi", name="Z") == "Hi Z"


def test_available_uses_native_names(cat):
    assert i18n.available() == [("es", "Español"), ("en", "English")]


@pytest.mark.parametrize("pref,ui,expected", [
    ("system", ["es-MX"], "es"), ("system", ["es-ES"], "es"), ("system", ["es-AR", "en-US"], "es"),
    ("system", ["en-US"], "en"), ("system", ["en-GB"], "en"), ("system", ["fr-FR"], "en"),
    ("system", ["fr-FR", "es-MX"], "es"), ("system", [], "en"), ("system", ["es_MX"], "es"),
    ("en", ["es-MX"], "en"), ("es", ["en-US"], "es"), ("xx", ["fr"], "en"),
])
def test_resolve(pref, ui, expected):
    assert i18n.resolve(pref, ui) == expected


def test_set_language_exports_env_and_notifies(monkeypatch):
    seen = []
    i18n.on_change(lambda: seen.append(i18n.current()))
    try:
        i18n.set_language("en")
        assert i18n.current() == "en" and __import__("os").environ["MAREA_LANG"] == "en"
        assert seen[-1] == "en"
        assert i18n.set_language("zz") == "en"  # unsupported -> fallback
    finally:
        i18n._listeners.pop()


def test_bind_retranslates_live_and_forgets_destroyed(qapp, cat):
    from PySide6.QtWidgets import QComboBox, QLabel
    i18n.set_language("es")
    label = QLabel()
    lang.bind(label, "a.hi", name="Ana")
    assert label.text() == "Hola Ana"
    i18n.set_language("en")
    assert label.text() == "Hi Ana"
    combo = QComboBox()
    combo.addItem("x", "keep")
    lang.bind_items(combo, [("a.hi", "d1"), ("a.only_en", "keep")])
    combo.setCurrentIndex(1)
    i18n.set_language("es")
    assert combo.itemText(0) == "Hola {name}" and combo.currentData() == "keep"
    key = (id(label), "setText")
    assert key in lang._bindings
    label.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert key not in lang._bindings
