"""Style tab builder and handler for the Editor."""
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QWidget

from marea.engine.motion import MODES as MOTION_MODES
from marea.ui import theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.widgets import CoalescedAction, SliderRow

MOTION_LABELS = {
    "auto": "Auto (según la letra)",
    "onda": "Onda",
    "rebote": "Rebote",
    "respira": "Respira",
    "suave": "Suave",
    "clasico": "Clásico (sin capa)",
}
assert set(MOTION_LABELS) == set(MOTION_MODES)


def build_style_tab(editor, reduces_motion_fn) -> QWidget:
    w = QWidget()
    form = QFormLayout(w)
    form.setContentsMargins(0, theme.SPACE[3], 0, 0)
    form.setVerticalSpacing(theme.SPACE[2])

    ed = editor
    ed.cb_bloom = QCheckBox("Bloom")
    ed.cb_vib = QCheckBox("Micro-vibrato")
    ed.cb_emph = QCheckBox("Letras en palabras largas")
    for cb in (ed.cb_bloom, ed.cb_vib, ed.cb_emph):
        cb.setChecked(True)
        cb.toggled.connect(ed._style_changed)
    checks = QHBoxLayout()
    checks.addWidget(ed.cb_bloom)
    checks.addWidget(ed.cb_vib)
    form.addRow(checks)
    form.addRow(ed.cb_emph)

    ed.cb_credit = QCheckBox("Crédito de la letra al final")
    ed.cb_credit.setChecked(True)
    ed.cb_credit.setToolTip("«Written by…» y «Provided by…» tras el último verso, como en Spicetify")
    ed.cb_credit.toggled.connect(ed._credit_toggled)
    form.addRow(ed.cb_credit)

    ed.cmb_motion = QComboBox()
    for key in MOTION_MODES:
        ed.cmb_motion.addItem(MOTION_LABELS[key], key)
    ed.cmb_motion.setToolTip("Movimiento por carácter: onda, rebote o respiración. Auto elige por línea según su tempo y su texto.")
    ed.cmb_motion.currentIndexChanged.connect(ed._style_changed)
    form.addRow("Movimiento", ed.cmb_motion)

    ed.cb_reduce = QCheckBox("Movimiento reducido")
    ed.cb_reduce.setChecked(pkg_attr("system_reduces_motion", reduces_motion_fn)())
    ed.params.reduce = ed.cb_reduce.isChecked()
    ed.cb_reduce.setToolTip("Sin capa por carácter, sin saltos por sílaba ni vibrato: quedan el relleno, el brillo y el desplazamiento")
    ed.cb_reduce.toggled.connect(ed._style_changed)
    form.addRow(ed.cb_reduce)

    ed.cb_fxdebug = QCheckBox("Depurar efectos por palabra")
    ed.cb_fxdebug.setToolTip("Marca cada palabra con efecto semántico y muestra su lema, idioma y categorías (no sale en el export)")
    ed.cb_fxdebug.setVisible(False)
    ed.cb_fxdebug.toggled.connect(lambda _: ed._wordfx_sync())
    form.addRow(ed.cb_fxdebug)

    fx_row = SliderRow(
        0,
        150,
        100,
        lambda v: f"{v} %",
        tooltip="Intensidad global de los efectos por palabra (0 % los apaga sin desactivar la función)",
    )
    ed.sl_fx, ed.lb_fx, ed.fx_row = fx_row.slider, fx_row.label, fx_row
    form.addRow("Intensidad FX", ed.fx_row)
    ed.fx_row.setVisible(False)
    ed.fx_label = form.labelForField(ed.fx_row)
    ed.fx_label.setVisible(False)

    ed.cb_fxbg = QCheckBox("Adaptar color de efectos al fondo")
    ed.cb_fxbg.setToolTip("Mide el fondo detrás de cada palabra con efecto y ajusta su color para que contraste, conservando el tono")
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
    for name, srow in (("Blur", blur_row), ("Rebote", wob_row), ("Expresividad", amt_row)):
        form.addRow(name, srow)
        srow.valueChanged.connect(ed._style_changed)

    ed.cmb_font = QComboBox()
    ed.cmb_font.addItem("Inter Tight (incluida)", "")
    for fam in QFontDatabase.families():
        if not fam.startswith("@") and fam != "Inter Tight":
            ed.cmb_font.addItem(fam, fam)
    ed.cmb_font.currentIndexChanged.connect(ed._font_changed)
    form.addRow("Fuente", ed.cmb_font)
    return w
