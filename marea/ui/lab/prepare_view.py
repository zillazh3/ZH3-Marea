"""Prepare page view builder for LabReviewWindow."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget

from marea.lab import env
from marea.ui import lang, theme
from marea.ui.widgets import make_button

LANGS = [("Español", "es"), ("English", "en")]  # native names: never translated


def build_prepare_page(win) -> QWidget:
    """Populate prepare controls on win and return the prepare page widget."""
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    card = QFrame()
    card.setObjectName("Card")
    cl = QVBoxLayout(card)
    cl.setContentsMargins(theme.SPACE[5], theme.SPACE[4], theme.SPACE[5], theme.SPACE[4])
    cl.setSpacing(theme.SPACE[3])
    win.lb_mode = QLabel()
    lang.bind(win.lb_mode, "lab.prepare.modeTtml" if win.mode == "ttml" else "lab.prepare.modeText")
    win.lb_mode.setObjectName("Body")
    win.lb_mode.setWordWrap(True)
    win.lb_mode.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    cl.addWidget(win.lb_mode)

    row = QHBoxLayout()
    win.lb_audio = QLabel()
    win.lb_audio.setObjectName("Muted")
    win.btn_audio = make_button("", "Ghost", win._choose_audio)
    lang.bind(win.btn_audio, "lab.prepare.chooseAudio")
    row.addWidget(win.lb_audio, 1)
    row.addWidget(win.btn_audio)
    cl.addLayout(row)

    row = QHBoxLayout()
    lb = QLabel()
    lang.bind(lb, "lab.prepare.language")
    lb.setObjectName("Body")
    win.cb_lang = QComboBox()
    win.cb_lang.addItem("", "")
    lang.bind_fn(win.cb_lang, "detectItem", lambda: win.cb_lang.setItemText(0, lang.t("lab.prepare.langDetect")))
    for name, code in LANGS:
        win.cb_lang.addItem(name, code)
    guess = win.skeleton.language[:2] if win.skeleton and win.skeleton.language else ""
    win.cb_lang.setCurrentIndex(max(0, win.cb_lang.findData(guess)))
    row.addWidget(lb)
    row.addStretch()
    row.addWidget(win.cb_lang)
    cl.addLayout(row)

    win.cb_sep = QCheckBox()
    lang.bind(win.cb_sep, "lab.prepare.separate")
    win.cb_sep.setChecked(env.separator_model_ready(win.root) and env.separation_installed(win.root))
    cl.addWidget(win.cb_sep)

    win.cb_layers = QCheckBox()
    lang.bind(win.cb_layers, "lab.prepare.layers")
    win.cb_layers.setChecked(False)
    lang.bind(win.cb_layers, "lab.prepare.layersTip", "setToolTip")
    layer_row = QHBoxLayout()
    layer_row.addWidget(win.cb_layers, 1)
    win.btn_layer_models = make_button(
        "",
        "Ghost",
        lambda: win.editor.window().open_lab_model_settings(env.KARAOKE_MODEL),
    )
    lang.bind(win.btn_layer_models, "lab.prepare.layerModels")
    layer_row.addWidget(win.btn_layer_models)
    cl.addLayout(layer_row)

    win.lb_layers_status = QLabel()
    win.lb_layers_status.setObjectName("Muted")
    win.lb_layers_status.setWordWrap(True)
    win.lb_layers_status.setTextFormat(Qt.PlainText)
    win.lb_layers_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
    cl.addWidget(win.lb_layers_status)
    win.cb_layers.toggled.connect(lambda _: win._refresh_layers())
    win._refresh_layers()

    win.ed_text = QPlainTextEdit()
    lang.bind(win.ed_text, "lab.prepare.textPlaceholder", "setPlaceholderText")
    win.ed_text.setMinimumHeight(220)
    win.ed_text.setPlainText("\n".join(l.text for l in getattr(win.project.lyrics, "lines", [])) if win.mode == "text" else "")
    win.ed_text.setVisible(win.mode == "text")
    cl.addWidget(win.ed_text)

    row = QHBoxLayout()
    win.btn_open = make_button("", "Ghost", win._open_json)
    lang.bind(win.btn_open, "lab.prepare.openSaved")
    win.btn_asr = make_button("", "Ghost", win._transcribe)
    lang.bind(win.btn_asr, "lab.prepare.transcribe")
    win.btn_asr.setVisible(win.mode == "text")
    win.btn_cancel = make_button("", "Ghost", win._cancel)
    lang.bind(win.btn_cancel, "lab.common.cancel")
    win.btn_cancel.setVisible(False)
    win.btn_align = make_button("", "Primary", win._align)
    lang.bind(win.btn_align, "lab.prepare.align")
    row.addWidget(win.btn_open)
    row.addStretch()
    row.addWidget(win.btn_asr)
    row.addWidget(win.btn_cancel)
    row.addWidget(win.btn_align)
    cl.addLayout(row)

    if win.mode == "text":
        lay.addWidget(card, 1)
        cl.setStretchFactor(win.ed_text, 1)
    else:
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay.addWidget(card)
        lay.addStretch(1)
    return page
