"""Galería de efectos (depuración de Semantic Word FX): una palabra de muestra con el efecto forzado, en bucle, y los sliders de ajuste.

Solo se importa con la función activada. No reimplementa nada: arma un `FxPlan` con `build_plan` (la palabra forzada con un `Override`)
y lo dibuja con el mismo `PreviewWidget`/`Scene` que el editor. Los ajustes se aplican con `config.set_fx` y se restauran al cerrar.
Uso suelto: `python -m marea.ui.wordfx_gallery`.
"""
import sys
from dataclasses import fields, replace
from types import SimpleNamespace

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from marea.demo_cover import demo_cover
from marea.engine.engine import Params
from marea.model.lyrics import Line, Lyrics, Syllable
from marea.render.scene import Style
from marea.ui import lang, theme
from marea.ui.preview import PreviewWidget
from marea.ui.widgets import SliderRow, blocked_signals, make_button
from marea.wordfx import config, library, rules as rules_mod, variants
from marea.wordfx.config import FxConfig
from marea.wordfx.plan import build_plan
from marea.wordfx.songs import Override

DEFAULTS = FxConfig()
ALL = "*"  # data of the "all variants" entry
WORD_MS, LINE_START_MS = 900, 1000  # every sample word lasts this long (a held word)
PRE_MS, POST_MS = 500, 1500  # the loop runs from PRE before the word to POST after its release
LANGS = (("es", "Español"), ("en", "English"))
# key -> (label i18n key, min, max, default, readout); every slider is an int, the value is /100 except where noted
_pct = lambda v: f"{v} %"
SLIDERS = (
    ("intensity", "wordfxUi.gallery.intensity", 0, 150, 100, _pct),
    ("glow", "wordfxUi.gallery.glow", 0, 200, 100, lambda v: f"×{v / 100:.2f}"),
    ("streak", "wordfxUi.gallery.streak", 0, 100, 0, lambda v: f"{v / 100:.2f}"),
    ("opacity", "wordfxUi.gallery.opacity", 0, 100, round(DEFAULTS.streak_opacity * 100), lambda v: f"{v / 100:.2f}"),
    ("thickness", "wordfxUi.gallery.thickness", 50, 400, round(DEFAULTS.streak_thickness_px * 100), lambda v: f"{v / 100:.2f}"),
    ("front", "wordfxUi.gallery.front", 5, 100, round(DEFAULTS.streak_front_em * 100), lambda v: f"{v / 100:.2f}"),
    ("trail", "wordfxUi.gallery.trail", 20, 300, round(DEFAULTS.streak_trail_em * 100), lambda v: f"{v / 100:.2f}"),
    ("decay", "wordfxUi.gallery.decay", 50, 600, round(DEFAULTS.streak_decay * 100), lambda v: f"{v / 100:.2f}"),
    ("overshoot", "wordfxUi.gallery.overshoot", 0, 50, round(DEFAULTS.streak_overshoot_em * 100), lambda v: f"{v / 100:.2f}"),
)
SLIDER_DEFAULTS = {s[0]: s[4] for s in SLIDERS}
DIRECT = {"opacity": "streak_opacity", "thickness": "streak_thickness_px", "front": "streak_front_em", "trail": "streak_trail_em",
          "decay": "streak_decay", "overshoot": "streak_overshoot_em"}  # slider key -> FxConfig field (value / 100)


def _row(form: QFormLayout, key: str, field: QWidget):
    """`form.addRow(label, field)` with a label that follows the UI language."""
    label = QLabel()
    lang.bind(label, key)
    form.addRow(label, field)


def config_snippet(cfg: FxConfig) -> str:
    """Python snippet with only the fields that differ from the shipped defaults."""
    parts = [f"{f.name}={getattr(cfg, f.name)!r}" for f in fields(cfg) if getattr(cfg, f.name) != getattr(DEFAULTS, f.name)]
    return f"FxConfig({', '.join(parts)})"


def _streak_default(cat: str) -> int:
    return round(DEFAULTS.streak_by_category.get(cat, DEFAULTS.streak_default) * 100)


def _lemmas(lib: library.Library, cat: str, lang: str) -> list[str]:
    """Usable sample words of a category, best language first: single alphabetic tokens, sorted, 3+ letters preferred."""
    lem = lib.categories[cat].lemmas
    for lg in (lang, *(g for g, _ in LANGS if g != lang)):
        words = sorted(w for w in lem.get(lg, ()) if w.isalpha())
        if words:
            return [w for w in words if len(w) >= 3] or words
    return [cat]


class WordFxGallery(QDialog):
    def __init__(self, lib: library.Library | None = None, parent=None):
        super().__init__(parent)
        lang.bind(self, "wordfxUi.gallery.title", "setWindowTitle")
        self._lib = lib or library.load()
        self._rules = rules_mod.load()
        self._prev = config.FX  # restored on close
        self._loop, self._last_pos = 0, 0.0
        self._lyrics, self._target, self._cats, self.plan = None, 0, [], None
        self._closed = False
        self._cover = demo_cover()
        self.cats = sorted(self._lib.categories)

        self.cmb_cat, self.cmb_var, self.cmb_lang = QComboBox(), QComboBox(), QComboBox()
        self.cmb_cat.addItems(self.cats)
        for code, name in LANGS:
            self.cmb_lang.addItem(name, code)
        self.cb_light = QCheckBox()
        lang.bind(self.cb_light, "wordfxUi.gallery.light")
        lang.bind(self.cb_light, "wordfxUi.gallery.lightTip", "setToolTip")
        self.lbl_word = QLabel()
        self.lbl_word.setObjectName("Caption")
        self.rows = {k: SliderRow(lo, hi, d, fmt, min_label_width=52) for k, _, lo, hi, d, fmt in SLIDERS}

        form = QFormLayout()
        _row(form, "wordfxUi.gallery.category", self.cmb_cat)
        _row(form, "wordfxUi.gallery.variant", self.cmb_var)
        _row(form, "wordfxUi.gallery.language", self.cmb_lang)
        _row(form, "wordfxUi.gallery.word", self.lbl_word)
        form.addRow(self.cb_light)
        for k, label, *_ in SLIDERS:
            _row(form, label, self.rows[k])
        self.btn_play = make_button("", "Ghost", self._toggle_play)
        lang.bind(self.btn_play, "wordfxUi.gallery.pause")
        btn_reset, btn_copy = make_button("", "Ghost", self.reset), make_button("", "Ghost", self.copy_config)
        lang.bind(btn_reset, "wordfxUi.gallery.reset")
        lang.bind(btn_copy, "wordfxUi.gallery.copyConfig")
        buttons = QHBoxLayout()
        for b in (self.btn_play, btn_reset, btn_copy):
            buttons.addWidget(b)
        left = QVBoxLayout()
        left.addLayout(form)
        left.addLayout(buttons)
        left.addStretch(1)
        side = QWidget()
        side.setLayout(left)
        side.setFixedWidth(420)

        self.preview = PreviewWidget(self, prefer_gpu=False, async_video=False)  # CPU: works offscreen, same Scene as the editor
        self.preview.setMinimumSize(300, 530)
        self.preview.position.connect(self._on_position)
        self.preview.playing_changed.connect(self._on_playing_changed)
        root = QHBoxLayout(self)
        root.addWidget(side)
        root.addWidget(self.preview, 1)

        self._fill_variants()
        self._reset_streak()
        self.cmb_lang.currentIndexChanged.connect(lambda _=0: self._refresh(new_word=True))
        self.cmb_var.currentIndexChanged.connect(lambda _=0: self._refresh())
        self.cmb_cat.currentIndexChanged.connect(self._on_category)
        self.cb_light.toggled.connect(lambda _: self._refresh(new_word=True))
        for r in self.rows.values():
            r.valueChanged.connect(lambda _: self._refresh())
        self._refresh(new_word=True)

    # ---- state -> config / plan ----
    @property
    def category(self) -> str:
        return self.cmb_cat.currentText()

    def current_config(self) -> FxConfig:
        """The gallery's tuning as an FxConfig (defaults + the sliders the user moved), without the forced variant."""
        ch, v = {}, {k: r.value() for k, r in self.rows.items()}
        for key, name in DIRECT.items():
            if v[key] != SLIDER_DEFAULTS[key]:
                ch[name] = v[key] / 100
        if v["glow"] != 100:
            ch["glow_gain"] = round(DEFAULTS.glow_gain * v["glow"] / 100, 4)
        if v["streak"] != _streak_default(self.category):
            ch["streak_by_category"] = {**DEFAULTS.streak_by_category, self.category: v["streak"] / 100}
        return replace(DEFAULTS, **ch)

    def _variant(self) -> str:
        opts = variants.options_for(self.category)
        data = self.cmb_var.currentData()
        return opts[self._loop % len(opts)].name if data == ALL else data

    def _fill_variants(self):
        with blocked_signals(self.cmb_var):
            self.cmb_var.clear()
            for v in variants.options_for(self.category):
                self.cmb_var.addItem(v.name, v.name)
            self.cmb_var.addItem(lang.t("wordfxUi.gallery.allVariants"), ALL)
        lang.bind_fn(self.cmb_var, "all", lambda: self.cmb_var.setItemText(
            max(0, self.cmb_var.findData(ALL)), lang.t("wordfxUi.gallery.allVariants")))

    def _reset_streak(self):
        with blocked_signals(self.rows["streak"]):
            self.rows["streak"].setValue(_streak_default(self.category))
        self.rows["streak"].update_readout()

    def _on_category(self, _=0):
        self._fill_variants()
        self._reset_streak()
        self._refresh(new_word=True)

    def _sample_lines(self) -> tuple[Lyrics, int, list[str]]:
        """-> (lyrics, index of the target word, forced category of each word). Light mode puts a strong word of ANOTHER category
        before it (a neighbour of the same category would exclude the forced variant, see `variants.pick`)."""
        cat, lang = self.category, self.cmb_lang.currentData()
        word = _lemmas(self._lib, cat, lang)[0]
        words, cats = [word], [cat]
        if self.cb_light.isChecked() and len(self.cats) > 1:
            other = self.cats[(self.cats.index(cat) + 1) % len(self.cats)]
            words, cats = [_lemmas(self._lib, other, lang)[0], word], [other, cat]
        syls = [Syllable(w, LINE_START_MS + i * WORD_MS, LINE_START_MS + (i + 1) * WORD_MS, word_end=i < len(words) - 1)
                for i, w in enumerate(words)]
        self.lbl_word.setText(word)
        return Lyrics([Line(syls[0].begin, syls[-1].end, syllables=syls)]), len(words) - 1, cats

    def _build(self):
        cat, name = self.category, self._variant()
        weights = {variants.variant_id(cat, v.name): float(v.name == name) for v in variants.options_for(cat)}
        config.set_fx(replace(self.current_config(), variant_weights=weights))
        self.plan = build_plan(self._lyrics, self._lib, self.rows["intensity"].value() / 100, track_id="gallery",
                               overrides=[Override(line=0, word=i, categories=(c,)) for i, c in enumerate(self._cats)],
                               rules=self._rules, matches={})

    def _refresh(self, new_word: bool = False):
        """Rebuild the plan (and the scene when the sample word changed) and show it right away."""
        if new_word or self._lyrics is None:
            self._lyrics, self._target, self._cats = self._sample_lines()
        self._build()
        pv = self.preview
        if new_word:
            begin = LINE_START_MS + self._target * WORD_MS
            rng = (begin - PRE_MS, begin + WORD_MS + DEFAULTS.max_release_s * 1000 + POST_MS)
            pv.set_wordfx(self.plan, False, rebuild=False)
            pv.set_project(SimpleNamespace(lyrics=self._lyrics, cover=self._cover), Style(), Params(), rng)
        else:
            pv.set_wordfx(self.plan)

    # ---- playback / actions ----
    def _on_position(self, ms: float, _dur: float):
        wrapped = ms < self._last_pos - 100
        self._last_pos = ms
        if wrapped:  # a new loop: the "all variants" entry moves to the next one
            self._loop += 1
            if self.cmb_var.currentData() == ALL:
                self._build()
                self.preview.set_wordfx(self.plan)

    def _on_playing_changed(self, on: bool):
        if on:
            lang.bind(self.btn_play, "wordfxUi.gallery.pause")
        else:
            lang.bind(self.btn_play, "wordfxUi.gallery.play")

    def _toggle_play(self):
        self.preview.set_playing(not self.preview.playing)

    def reset(self):
        for k, d in SLIDER_DEFAULTS.items():
            with blocked_signals(self.rows[k]):
                self.rows[k].setValue(d)
            self.rows[k].update_readout()
        self._reset_streak()
        self._refresh()

    def copy_config(self) -> str:
        text = config_snippet(self.current_config())
        QGuiApplication.clipboard().setText(text)
        return text

    # ---- lifecycle ----
    def showEvent(self, e):
        super().showEvent(e)
        self.preview.set_playing(True)

    def _restore(self):
        if self._closed:
            return
        self._closed = True
        config.set_fx(self._prev)
        self.preview.shutdown()

    def done(self, r):
        self._restore()
        super().done(r)

    def closeEvent(self, e):
        self._restore()
        super().closeEvent(e)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    theme.load_fonts()
    theme.apply(app, "dark")
    WordFxGallery().exec()


if __name__ == "__main__":
    main()
