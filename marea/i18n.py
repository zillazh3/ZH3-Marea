"""Dependency-free UI localization: `t(key, **params)` over JSON catalogs in assets/i18n/<code>.json.

Pure Python (no Qt), so core modules and the spawned export worker can use it too. To add a language, drop a new
`assets/i18n/<code>.json` (with `"_meta": {"name": "<native name>"}`) and add the code to SUPPORTED.
"""
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

log = logging.getLogger("marea")

SUPPORTED = ("es", "en")
FALLBACK = "en"
PLURAL_FORMS = frozenset({"zero", "one", "two", "few", "many", "other"})
# code -> f(count) -> plural form. shortcut: only one/other rules; add an entry here for languages that need more.
PLURAL_RULES = {}
DIR = Path(__file__).resolve().parent.parent / "assets" / "i18n"

_catalogs: dict[str, dict] = {}
_names: dict[str, str] = {}
_warned: set[str] = set()
_listeners: list = []


def _initial() -> str:
    forced = os.environ.get("MAREA_LANG", "")
    return forced if forced in SUPPORTED else FALLBACK


_current = _initial()


def _flatten(node: dict, prefix: str, out: dict):
    for k, v in node.items():
        if not prefix and k == "_meta":
            continue
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and not (v and set(v) <= PLURAL_FORMS):  # a plural leaf is {"one": ..., "other": ...}
            _flatten(v, key, out)
        else:
            out[key] = v


def _load(code: str) -> dict:
    if code not in _catalogs:
        flat: dict = {}
        try:
            raw = json.loads((DIR / f"{code}.json").read_text(encoding="utf-8"))
            _flatten(raw, "", flat)
            _names[code] = raw.get("_meta", {}).get("name", code)
        except (OSError, ValueError) as e:  # a broken locale must never crash the app: it degrades to the fallback/key
            log.error("Cannot load locale %s: %s", code, e)
        _catalogs[code] = flat
    return _catalogs[code]


def catalog(code: str) -> dict:
    """Flat `key -> text | plural dict` map of a locale (empty if it failed to load)."""
    return _load(code)


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _warn(msg: str, key: str):
    if key not in _warned and not getattr(sys, "frozen", False):  # dev-only: released builds stay quiet
        _warned.add(key)
        log.warning(msg, key)


def _plural(code: str, n) -> str:
    rule = PLURAL_RULES.get(code)
    return rule(n) if rule else ("one" if n == 1 else "other")


def t(key: str, **params) -> str:
    """Text for `key` in the current language, else English, else the key itself. Never raises, never blank."""
    code = _current
    value = _load(code).get(key)
    if value is None:
        code = FALLBACK
        value = _load(code).get(key)
        if value is None:
            _warn("Unknown i18n key %s", key)
            return key
        _warn("Missing translation for %s", f"{_current}:{key}")
    if isinstance(value, dict):
        form = _plural(code, params.get("count", 0))
        value = value.get(form) or value.get("other") or next(iter(value.values()))
    if not params:
        return value
    try:
        return value.format_map(_Safe(params))
    except (ValueError, AttributeError, IndexError, TypeError):  # malformed template: show it raw rather than crash
        return value


def current() -> str:
    return _current


def available() -> list[tuple[str, str]]:
    """[(code, native name)] for the language selector."""
    for c in SUPPORTED:
        _load(c)
    return [(c, _names.get(c, c)) for c in SUPPORTED]


def on_change(fn):
    _listeners.append(fn)


def set_language(code: str) -> str:
    """Switch live. The env var lets spawned worker processes inherit it. Returns the language actually set."""
    global _current
    code = code if code in SUPPORTED else FALLBACK
    os.environ["MAREA_LANG"] = code
    if code != _current:
        _current = code
        for fn in list(_listeners):
            fn()
    return code


def resolve(pref: str, ui_languages=()) -> str:
    """`pref` is "system" or a code. For "system", walk the OS preference list by base language (es-MX -> es)."""
    if pref in SUPPORTED:
        return pref
    for tag in ui_languages:
        base = re.split(r"[-_]", str(tag), maxsplit=1)[0].lower()
        if base in SUPPORTED:
            return base
    return FALLBACK


class Labels(Mapping):
    """Read-only table whose stored values are i18n keys; each read resolves them with `t()` (never at import time)."""

    def __init__(self, data: dict, resolve: Callable = t):
        self._data, self._resolve = dict(data), resolve

    def __getitem__(self, k):
        return self._resolve(self._data[k])

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)
