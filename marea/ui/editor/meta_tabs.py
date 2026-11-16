"""Background/metadata and watermark tabs for the Editor."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QFrame, QLabel, QLineEdit, QScrollArea, QWidget

from marea.render.scene import Style
from marea.ui import theme
from marea.ui.editor.background_tab import BackgroundController
from marea.ui.widgets import make_button


def build_background_tab(editor) -> QWidget:
    w = QWidget()
    form = QFormLayout(w)
    form.setContentsMargins(0, theme.SPACE[3], 0, 0)
    form.setVerticalSpacing(theme.SPACE[2])
    editor.ed_title, editor.ed_artist = QLineEdit(), QLineEdit()
    editor.ed_title.textChanged.connect(editor._meta_changed)
    editor.ed_artist.textChanged.connect(editor._meta_changed)
    btn = make_button("Elegir imagen…", "Ghost", editor._choose_cover)
    form.addRow("Título", editor.ed_title)
    form.addRow("Artista", editor.ed_artist)
    form.addRow("Portada", btn)
    editor._bgctl = BackgroundController(editor)
    editor._bgctl.build(form)
    scroll = QScrollArea()  # con los controles de video la pestaña ya no cabe en el panel
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
    scroll.setWidget(w)
    return scroll


def build_watermark_tab(editor) -> QWidget:
    w = QWidget()
    form = QFormLayout(w)
    form.setContentsMargins(0, theme.SPACE[3], 0, 0)
    form.setVerticalSpacing(theme.SPACE[2])
    s = editor.settings
    editor.ed_wm = QLineEdit(s.value("watermark/text", Style.wm_text))
    editor.ed_wm.setPlaceholderText("Vacío = sin marca de agua")
    editor.cmb_wm_side = QComboBox()
    editor.cmb_wm_side.addItem("Esquina inferior izquierda", "left")
    editor.cmb_wm_side.addItem("Esquina inferior derecha", "right")
    editor.cmb_wm_side.setCurrentIndex(max(0, editor.cmb_wm_side.findData(s.value("watermark/side", Style.wm_side))))
    editor.cb_wm_glyph = QCheckBox("Ondas junto al texto")
    editor.cb_wm_glyph.setChecked(s.value("watermark/glyph", False, type=bool))
    hint = QLabel(
        "Queda fuera de la zona que tapa la interfaz de TikTok, entra con un fundido de 0.6 s y baja su opacidad "
        "si cruza la línea que se está cantando."
    )
    hint.setObjectName("Caption")
    hint.setWordWrap(True)
    form.addRow("Texto", editor.ed_wm)
    form.addRow("Posición", editor.cmb_wm_side)
    form.addRow(editor.cb_wm_glyph)
    form.addRow(hint)
    editor.ed_wm.textChanged.connect(editor._wm_changed)
    editor.cmb_wm_side.currentIndexChanged.connect(editor._wm_changed)
    editor.cb_wm_glyph.toggled.connect(editor._wm_changed)
    editor._apply_wm()
    return w
