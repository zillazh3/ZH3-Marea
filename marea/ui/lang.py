"""Qt glue for marea.i18n: live re-translation of widgets, system-language detection and locale-aware formatting.

Same pattern as theme.signals: widgets that show fixed text register it with `bind`/`bind_fn` and are re-applied when the
language changes; transient text (toasts, dialogs) just calls `t()` when it is shown.
"""
from collections.abc import Callable, Sequence

from PySide6.QtCore import QDate, QDateTime, QLocale, QObject, Signal
from shiboken6 import isValid

from marea import i18n
from marea.i18n import t  # noqa: F401  (re-exported so UI modules import everything from here)


class _Signals(QObject):
    changed = Signal()


signals = _Signals()  # custom-painted widgets connect to signals.changed and call update()
i18n.on_change(lambda: _retranslate())

_bindings: dict[tuple, tuple] = {}  # (id(obj), slot) -> (obj, apply)


def system_ui_languages() -> list[str]:
    """The OS's preferred UI languages, most preferred first (Windows: the user's language list)."""
    return list(QLocale.system().uiLanguages())


def apply_preference(pref: str) -> str:
    """Resolve a saved preference ("system" | code) against the OS languages and switch to it."""
    return i18n.set_language(i18n.resolve(pref, system_ui_languages()))


def _forget(key: tuple):
    _bindings.pop(key, None)


def bind_fn(obj: QObject, slot: str, apply: Callable[[], None]) -> None:
    """Run `apply()` now and again on every language change. Binding the same (obj, slot) again replaces the old one."""
    key = (id(obj), slot)
    if key not in _bindings:
        obj.destroyed.connect(lambda *_: _forget(key))
    _bindings[key] = (obj, apply)
    apply()


def bind(obj: QObject, key: str, setter: str = "setText", **params) -> None:
    """`obj.<setter>(t(key, **params))`, kept in sync with the language. Call again to change the key or params."""
    bind_fn(obj, setter, lambda: getattr(obj, setter)(t(key, **params)))


def unbind(obj: QObject, setter: str = "setText") -> None:
    """Stop managing a slot, e.g. before showing raw content such as a song title in a label that had a bound key."""
    _forget((id(obj), setter))


def bind_items(combo, items: Sequence[tuple[str, object]]) -> None:
    """(Re)build a combo from `(key, userData)` pairs, keeping the selection; item texts follow the language."""
    current = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    for key, data in items:
        combo.addItem(t(key), data)
    idx = combo.findData(current) if current is not None else -1
    combo.setCurrentIndex(max(0, idx))
    combo.blockSignals(False)

    def relabel():
        for i, (key, _) in enumerate(items):
            combo.setItemText(i, t(key))

    bind_fn(combo, "items", relabel)


def _retranslate():
    for key, (obj, apply) in list(_bindings.items()):
        if not isValid(obj):
            _forget(key)
            continue
        apply()
    signals.changed.emit()


def locale() -> QLocale:
    return QLocale(i18n.current())


def number(x: float, decimals: int = 0) -> str:
    return locale().toString(float(x), "f", decimals)


def date(dt: QDate | QDateTime) -> str:
    return locale().toString(dt, QLocale.ShortFormat)
