"""Background/metadata and watermark tabs for the Editor."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QFrame, QLabel, QLineEdit, QScrollArea, QWidget

from marea.render.scene import Style
from marea.ui import lang, theme
from marea.ui.editor.background_tab import BackgroundController
from marea.ui.widgets import make_button


def build_background_tab(editor) -> QWidget:
    w = QWidget()
    form = QFormLayout(w)
    form.setContentsMargins(0, theme.SPACE[3], 0, 0)
    form.setVerticalSpacing(theme.SPACE[2])
    editor.ed_title, editor.ed_artist, editor.ed_album = QLineEdit(), QLineEdit(), QLineEdit()
    editor.ed_title.textChanged.connect(editor._meta_changed)
    editor.ed_artist.textChanged.connect(editor._meta_changed)
    editor.ed_album.textChanged.connect(editor._meta_changed)
    btn = make_button("", "Ghost", editor._choose_cover)
    lang.bind(btn, "editor.meta.chooseImage")
    btn_match = make_button("", "Ghost", editor._match_metadata_dialog)
    lang.bind(btn_match, "editor.meta.matchInfo")
    editor.btn_match_meta = btn_match
    for key, field in (
        ("editor.meta.title", editor.ed_title),
        ("editor.meta.artist", editor.ed_artist),
        ("editor.meta.album", editor.ed_album),
        ("editor.meta.cover", btn),
    ):
        label = QLabel()
        lang.bind(label, key)
        form.addRow(label, field)
    form.addRow(btn_match)
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
    lang.bind(editor.ed_wm, "editor.watermark.placeholder", "setPlaceholderText")
    editor.cmb_wm_side = QComboBox()
    lang.bind_items(editor.cmb_wm_side, [("editor.watermark.left", "left"), ("editor.watermark.right", "right")])
    editor.cmb_wm_side.setCurrentIndex(max(0, editor.cmb_wm_side.findData(s.value("watermark/side", Style.wm_side))))
    editor.cb_wm_glyph = QCheckBox()
    lang.bind(editor.cb_wm_glyph, "editor.watermark.glyph")
    editor.cb_wm_glyph.setChecked(s.value("watermark/glyph", False, type=bool))
    hint = QLabel()
    lang.bind(hint, "editor.watermark.hint")
    hint.setObjectName("Caption")
    hint.setWordWrap(True)
    for key, field in (("editor.watermark.text", editor.ed_wm), ("editor.watermark.position", editor.cmb_wm_side)):
        label = QLabel()
        lang.bind(label, key)
        form.addRow(label, field)
    form.addRow(editor.cb_wm_glyph)
    form.addRow(hint)
    editor.ed_wm.textChanged.connect(editor._wm_changed)
    editor.cmb_wm_side.currentIndexChanged.connect(editor._wm_changed)
    editor.cb_wm_glyph.toggled.connect(editor._wm_changed)
    editor._apply_wm()
    return w
