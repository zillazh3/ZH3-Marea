"""Style tab builder and handler for the Editor."""
from PySide6.QtGui import QFontDatabase
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLayout, QScrollArea, QWidget

from marea.engine.motion import MODES as MOTION_MODES
from marea.ui import lang, theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.widgets import CoalescedAction, SliderRow

MOTION_LABELS = {  # motion mode -> i18n key (resolved where shown)
    "auto": "style.motion.auto",
    "onda": "style.motion.wave",
    "rebote": "style.motion.bounce",
    "respira": "style.motion.breathe",
    "suave": "style.motion.soft",
    "clasico": "style.motion.classic",
}
assert set(MOTION_LABELS) == set(MOTION_MODES)


def form_row(form: QFormLayout, key: str, field: "QWidget | QLayout") -> QLabel:
    """`form.addRow(label, field)` with a label that follows the UI language."""
    label = QLabel()
    lang.bind(label, key)
    form.addRow(label, field)
    return label


def build_style_tab(editor, reduces_motion_fn) -> QWidget:
    w = QWidget()
    form = QFormLayout(w)
    form.setContentsMargins(0, theme.SPACE[3], 0, 0)
    form.setVerticalSpacing(theme.SPACE[2])

    ed = editor
    ed._presets.build(form)
    ed.cb_bloom, ed.cb_vib, ed.cb_emph = QCheckBox(), QCheckBox(), QCheckBox()
    lang.bind(ed.cb_bloom, "style.bloom")
    lang.bind(ed.cb_vib, "style.vibrato")
    lang.bind(ed.cb_emph, "style.emphasis")
    for cb in (ed.cb_bloom, ed.cb_vib, ed.cb_emph):
        cb.setChecked(True)
        cb.toggled.connect(ed._style_changed)
    checks = QHBoxLayout()
    checks.addWidget(ed.cb_bloom)
    checks.addWidget(ed.cb_vib)
    form.addRow(checks)
    form.addRow(ed.cb_emph)

    ed.cb_credit = QCheckBox()
    lang.bind(ed.cb_credit, "style.credit")
    ed.cb_credit.setChecked(True)
    lang.bind(ed.cb_credit, "style.creditTip", "setToolTip")
    ed.cb_credit.toggled.connect(ed._credit_toggled)
    form.addRow(ed.cb_credit)

    ed.cmb_motion = QComboBox()
    lang.bind_items(ed.cmb_motion, [(MOTION_LABELS[key], key) for key in MOTION_MODES])
    lang.bind(ed.cmb_motion, "style.motionTip", "setToolTip")
    ed.cmb_motion.currentIndexChanged.connect(ed._style_changed)
    form_row(form, "style.motionLabel", ed.cmb_motion)

    ed.cb_reduce = QCheckBox()
    lang.bind(ed.cb_reduce, "style.reduce")
    ed.cb_reduce.setChecked(pkg_attr("system_reduces_motion", reduces_motion_fn)())
    ed.params.reduce = ed.cb_reduce.isChecked()
    lang.bind(ed.cb_reduce, "style.reduceTip", "setToolTip")
    ed.cb_reduce.toggled.connect(ed._style_changed)
    form.addRow(ed.cb_reduce)

    ed.cb_fxdebug = QCheckBox()
    lang.bind(ed.cb_fxdebug, "style.fxDebug")
    lang.bind(ed.cb_fxdebug, "style.fxDebugTip", "setToolTip")
    ed.cb_fxdebug.setVisible(False)
    ed.cb_fxdebug.toggled.connect(lambda _: ed._wordfx_sync())
    form.addRow(ed.cb_fxdebug)

    fx_row = SliderRow(0, 150, 100, lambda v: f"{v} %")
    lang.bind(fx_row.slider, "style.fxIntensityTip", "setToolTip")
    ed.sl_fx, ed.lb_fx, ed.fx_row = fx_row.slider, fx_row.label, fx_row
    form_row(form, "style.fxIntensity", ed.fx_row)
    ed.fx_row.setVisible(False)
    ed.fx_label = form.labelForField(ed.fx_row)
    ed.fx_label.setVisible(False)

    ed.cb_fxbg = QCheckBox()
    lang.bind(ed.cb_fxbg, "style.fxAdaptBg")
    lang.bind(ed.cb_fxbg, "style.fxAdaptBgTip", "setToolTip")
    ed.cb_fxbg.setChecked(ed.settings.value("wordfx/adapt_bg", True, type=bool))
    ed.cb_fxbg.setVisible(False)
    ed.cb_fxbg.toggled.connect(lambda v: (ed.settings.setValue("wordfx/adapt_bg", v), ed._wordfx_sync()))
    form.addRow(ed.cb_fxbg)

    ed._fx_action = CoalescedAction(16, ed._wordfx_sync, ed)
    fx_row.valueChanged.connect(lambda _v: ed._fx_action.schedule())
    fx_row.sliderReleased.connect(ed._fx_action.flush)

    blur_row = SliderRow(0, 20, 10, lambda v: f"{v / 10:.1f}×")
    wob_row = SliderRow(25, 100, 40, lambda v: f"ζ {v / 100:.2f}")
    amt_row = SliderRow(0, 150, 100, lambda v: f"{v} %")
    ed.sl_blur, ed.lb_blur = blur_row.slider, blur_row.label
    ed.sl_wob, ed.lb_wob = wob_row.slider, wob_row.label
    ed.sl_amt, ed.lb_amt = amt_row.slider, amt_row.label
    ed._style_sliders = (blur_row, wob_row, amt_row)
    for key, srow in (("style.blur", blur_row), ("style.bounce", wob_row), ("style.expressiveness", amt_row)):
        form_row(form, key, srow)
        srow.valueChanged.connect(ed._style_changed)

    ed.cmb_font = QComboBox()
    ed.cmb_font.addItem(lang.t("style.fontBundled"), "")
    lang.bind_fn(ed.cmb_font, "bundled", lambda: ed.cmb_font.setItemText(0, lang.t("style.fontBundled")))
    for fam in QFontDatabase.families():
        if not fam.startswith("@") and fam != "Inter Tight":
            ed.cmb_font.addItem(fam, fam)
    ed.cmb_font.currentIndexChanged.connect(ed._font_changed)
    form_row(form, "style.font", ed.cmb_font)
    scroll = QScrollArea()  # con el panel de Corrientes la pestaña ya no cabe sin desplazarse
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setWidget(w)
    return scroll
