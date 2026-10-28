"""Editor: preview 9:16 en vivo, lista de versos con rango, pestañas de estilo/fondo/audio y envío a la cola de export."""
import ctypes
import sys
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QActionGroup, QFontDatabase, QPainter
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QSlider, QStackedWidget, QStyle,
                               QStyledItemDelegate, QTabWidget, QVBoxLayout, QWidget)

from marea.audio import align, local
from marea.audio.loopback import AudioError
from marea.audio.session import record_song_range
from marea.engine.engine import Params
from marea.engine.motion import MODES as MOTION_MODES
from marea.export import ffmpeg
from marea.export.job import PRESETS, TAIL_MS, ExportJob, clip_name, clip_range, unique_path
from marea.export.queue import AudioSpec, ExportQueue, QueueItem
from marea.paths import default_output_dir, reveal_in_folder  # noqa: F401  (se reexportan)
from marea.project import ClipProject, load_cover, project_from_ttml
from marea.providers.errors import ProviderError
from marea.providers.spotify import Track
from marea.render.scene import Style
from marea.ui import theme
from marea.ui.net import run_async
from marea.ui.preview import PreviewWidget
from marea.ui.screens import EmptyState
from marea.ui.waveform import WaveformWidget

PANEL_W = 360
DEFAULT_VERSES = 6
MARKERS = {"none": "Ninguno", "flash": "Flash de 1 frame", "beep": "Pitido de 120 ms (solo sin audio)"}
MODES = {"lyrics": "Solo letra", "song": "Con canción", "full": "Canción completa"}
OUTROS = {"500": "0.5 s (corte seco)", "2000": "2 s", "4000": "4 s", "8000": "8 s"}  # ms tras el último verso, con fade-out del audio
LOOPBACK_OFFSET_RANGE, LOCAL_OFFSET_RANGE = (-500, 500), (-3000, 3000)  # ms
AUTO_RECORD_MS = 25_000  # cuánto de la canción se graba de Spotify para medir el desfase (más voz = estimación más segura)
AUDIO_FILTER = "Audio (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus);;Todos los archivos (*.*)"


MOTION_LABELS = {"auto": "Auto (según la letra)", "onda": "Onda", "rebote": "Rebote", "respira": "Respira", "suave": "Suave",
                 "clasico": "Clásico (sin capa)"}
assert set(MOTION_LABELS) == set(MOTION_MODES)


def system_reduces_motion() -> bool:
    """Windows: «Mostrar animaciones en Windows» desactivado (SPI_GETCLIENTAREAANIMATION). Solo es el valor inicial del editor."""
    if sys.platform != "win32":
        return False
    on = ctypes.c_int(1)
    ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)
    return bool(ok) and not on.value


def fmt_ms(ms: float, tenths: bool = True) -> str:
    s = max(0.0, ms) / 1000
    return f"{int(s // 60)}:{s % 60:04.1f}" if tenths else f"{int(s // 60)}:{int(s % 60):02d}"


class VerseDelegate(QStyledItemDelegate):
    """Fila de verso: número + texto; el rango elegido se resalta con la píldora del tema."""

    def __init__(self, editor: "EditorPage"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 38)

    def paint(self, p: QPainter, opt, index):
        t, e, row = theme.current, self.editor, index.row()
        inside = e.first <= row <= e.last
        r = QRectF(opt.rect).adjusted(2, 1, -2, -1)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if inside:
            p.setBrush(theme.qc(t.pill))
        elif opt.state & QStyle.State_MouseOver:
            p.setBrush(theme.qc(t.hover))
        else:
            p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, theme.RADIUS["control"], theme.RADIUS["control"])
        edge = row in (e.first, e.last)
        f = opt.font
        f.setPixelSize(theme.FONT_SIZES["xs"])
        p.setFont(f)
        p.setPen(theme.qc(t.accent_icon if edge and inside else t.text_faint))
        p.drawText(QRectF(r.left() + 10, r.top(), 28, r.height()), Qt.AlignVCenter | Qt.AlignRight, str(row + 1))
        f.setPixelSize(theme.FONT_SIZES["sm"])
        p.setFont(f)
        p.setPen(theme.qc(t.text if inside else t.text_muted))
        text = p.fontMetrics().elidedText(index.data(Qt.DisplayRole), Qt.ElideRight, int(r.width() - 52))
        p.drawText(QRectF(r.left() + 46, r.top(), r.width() - 52, r.height()), Qt.AlignVCenter | Qt.AlignLeft, text)
        p.restore()


class EditorPage(QWidget):
    project_changed = Signal(str)  # título para la barra de la ventana
    enqueued = Signal(object)  # QueueItem añadido a la cola

    def __init__(self, settings: QSettings, notify, queue: ExportQueue, services=None, prefer_gpu: bool = True):
        """notify(texto, tipo='info'|'ok'|'error', accion=None) muestra un toast."""
        super().__init__()
        self.setObjectName("Page")
        self.settings, self.notify, self.queue, self.services = settings, notify, queue, services
        self.project: ClipProject | None = None
        self._linked: Track | None = None  # pista de Spotify vinculada a un proyecto que no vino de Spotify (.ttml, demo)
        self.style, self.params = Style(), Params()
        self.first = self.last = self._anchor = 0
        self._scrubbing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[5])
        root.setSpacing(theme.SPACE[3])
        head = QHBoxLayout()
        title = QLabel("Editor")
        title.setObjectName("H1")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("Body")
        head.addWidget(title)
        head.addSpacing(theme.SPACE[3])
        head.addWidget(self.subtitle, 1, Qt.AlignBottom)
        root.addLayout(head)

        self.stack = QStackedWidget()
        card = QFrame()
        card.setObjectName("Card")
        QVBoxLayout(card).addWidget(EmptyState(
            "sliders-horizontal", "Elige una canción para editar",
            "Abre un .ttml o la canción demo desde Inicio, o busca una canción de Spotify; aquí eliges los versos y el estilo."))
        self.stack.addWidget(card)
        self.stack.addWidget(self._build_body(prefer_gpu))
        root.addWidget(self.stack, 1)
        root.addLayout(self._build_export_bar())
        theme.signals.changed.connect(self._refresh_icons)
        self._refresh_icons()

    # ---------------- construcción ----------------
    def _build_body(self, prefer_gpu: bool) -> QWidget:
        body = QWidget()
        lay = QHBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACE[5])

        left = QVBoxLayout()
        self.preview = PreviewWidget(prefer_gpu=prefer_gpu)
        self.preview.position.connect(self._on_position)
        self.preview.playing_changed.connect(self._on_playing)
        left.addWidget(self.preview, 1)
        ctrl = QHBoxLayout()
        self.btn_play = QPushButton()
        self.btn_play.setObjectName("IconBtn")
        self.btn_play.setFixedSize(40, 40)
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.setToolTip("Reproducir / pausar")
        self.btn_play.clicked.connect(lambda: self.preview.set_playing(not self.preview.playing))
        self.scrub = QSlider(Qt.Horizontal)
        self.scrub.setRange(0, 1000)
        self.scrub.sliderPressed.connect(lambda: setattr(self, "_scrubbing", True))
        self.scrub.sliderReleased.connect(lambda: setattr(self, "_scrubbing", False))
        self.scrub.valueChanged.connect(self._on_scrub)
        self.time = QLabel("0:00.0 / 0:00.0")
        self.time.setObjectName("Caption")
        ctrl.addWidget(self.btn_play)
        ctrl.addWidget(self.scrub, 1)
        ctrl.addWidget(self.time)
        left.addLayout(ctrl)
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(theme.SPACE[3])
        verses = QFrame()
        verses.setObjectName("Card")
        vl = QVBoxLayout(verses)
        vl.setContentsMargins(theme.SPACE[2], theme.SPACE[3], theme.SPACE[2], theme.SPACE[3])
        hint = QLabel("Clic en el primer verso y Shift+clic en el último")
        hint.setObjectName("Caption")
        hint.setContentsMargins(theme.SPACE[2], 0, 0, 0)
        self.list = QListWidget()
        self.list.setItemDelegate(VerseDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.itemClicked.connect(self._on_verse_clicked)
        self.list.itemDoubleClicked.connect(self._on_verse_double_clicked)
        self.range_label = QLabel()
        self.range_label.setObjectName("Muted")
        self.range_label.setContentsMargins(theme.SPACE[2], 0, 0, 0)
        self.credit = QLabel()  # de dónde salió la letra: Apple Music / Spicy Lyrics / LRCLIB (hay que mostrarlo)
        self.credit.setObjectName("Caption")
        self.credit.setTextFormat(Qt.PlainText)  # el nombre del autor lo escribe un tercero: nunca como HTML
        self.credit.setWordWrap(True)
        self.credit.setContentsMargins(theme.SPACE[2], 0, 0, 0)
        vl.addWidget(hint)
        vl.addWidget(self.list, 1)
        vl.addWidget(self.range_label)
        vl.addWidget(self.credit)
        right.addWidget(verses, 3)

        tabs_card = QFrame()
        tabs_card.setObjectName("Card")
        tl = QVBoxLayout(tabs_card)
        tl.setContentsMargins(theme.SPACE[3], theme.SPACE[2], theme.SPACE[3], theme.SPACE[3])
        tabs = QTabWidget()
        tabs.addTab(self._style_tab(), "Estilo")
        tabs.addTab(self._background_tab(), "Fondo")
        tabs.addTab(self._audio_tab(), "Audio")
        tabs.addTab(self._watermark_tab(), "Marca de agua")
        tl.addWidget(tabs)
        right.addWidget(tabs_card, 2)
        container = QWidget()
        container.setFixedWidth(PANEL_W)
        container.setLayout(right)
        right.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(container)
        return body

    def _style_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(0, theme.SPACE[3], 0, 0)
        form.setVerticalSpacing(theme.SPACE[2])
        self.cb_bloom, self.cb_vib, self.cb_emph = QCheckBox("Bloom"), QCheckBox("Micro-vibrato"), QCheckBox("Letras en palabras largas")
        for cb in (self.cb_bloom, self.cb_vib, self.cb_emph):
            cb.setChecked(True)
            cb.toggled.connect(self._style_changed)
        checks = QHBoxLayout()
        checks.addWidget(self.cb_bloom)
        checks.addWidget(self.cb_vib)
        form.addRow(checks)
        form.addRow(self.cb_emph)
        self.cb_credit = QCheckBox("Crédito de la letra al final")  # no es un parámetro del motor: agrega un elemento, hay que reconstruir
        self.cb_credit.setChecked(True)
        self.cb_credit.setToolTip("«Written by…» y «Provided by…» tras el último verso, como en Spicetify")
        self.cb_credit.toggled.connect(self._credit_toggled)
        form.addRow(self.cb_credit)
        self.cmb_motion = QComboBox()
        for key in MOTION_MODES:
            self.cmb_motion.addItem(MOTION_LABELS[key], key)
        self.cmb_motion.setToolTip("Movimiento por carácter: onda, rebote o respiración. Auto elige por línea según su tempo y su texto.")
        self.cmb_motion.currentIndexChanged.connect(self._style_changed)
        form.addRow("Movimiento", self.cmb_motion)
        self.cb_reduce = QCheckBox("Movimiento reducido")
        self.cb_reduce.setChecked(system_reduces_motion())
        self.params.reduce = self.cb_reduce.isChecked()  # el valor inicial viene del sistema: el motor debe saberlo desde el principio
        self.cb_reduce.setToolTip("Sin capa por carácter, sin saltos por sílaba ni vibrato: quedan el relleno, el brillo y el desplazamiento")
        self.cb_reduce.toggled.connect(self._style_changed)
        form.addRow(self.cb_reduce)
        self.sl_blur, self.lb_blur = self._slider(0, 20, 10), QLabel("1.0×")
        self.sl_wob, self.lb_wob = self._slider(25, 100, 40), QLabel("ζ 0.40")
        self.sl_amt, self.lb_amt = self._slider(0, 150, 100), QLabel("100 %")
        for name, sl, lb in (("Blur", self.sl_blur, self.lb_blur), ("Rebote", self.sl_wob, self.lb_wob),
                             ("Expresividad", self.sl_amt, self.lb_amt)):
            row = QHBoxLayout()
            lb.setObjectName("Caption")
            lb.setMinimumWidth(40)
            row.addWidget(sl, 1)
            row.addWidget(lb)
            form.addRow(name, row)
            sl.valueChanged.connect(self._style_changed)
        self.cmb_font = QComboBox()
        self.cmb_font.addItem("Inter Tight (incluida)", "")
        for fam in QFontDatabase.families():
            if not fam.startswith("@") and fam != "Inter Tight":
                self.cmb_font.addItem(fam, fam)
        self.cmb_font.currentIndexChanged.connect(self._font_changed)
        form.addRow("Fuente", self.cmb_font)
        return w

    @staticmethod
    def _slider(lo, hi, val) -> QSlider:
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(val)
        return s

    def _background_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(0, theme.SPACE[3], 0, 0)
        form.setVerticalSpacing(theme.SPACE[2])
        self.ed_title, self.ed_artist = QLineEdit(), QLineEdit()
        self.ed_title.textChanged.connect(self._meta_changed)
        self.ed_artist.textChanged.connect(self._meta_changed)
        btn = QPushButton("Elegir imagen…")
        btn.setObjectName("Ghost")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._choose_cover)
        form.addRow("Título", self.ed_title)
        form.addRow("Artista", self.ed_artist)
        form.addRow("Portada", btn)
        return w

    def _audio_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, theme.SPACE[3], 0, 0)
        lay.setSpacing(theme.SPACE[2])
        self.cmb_source = QComboBox()
        self.cmb_source.addItem("Loopback de Spotify", "spotify")
        self.cmb_source.addItem("Archivo de audio", "local")
        self.cmb_source.currentIndexChanged.connect(self._source_changed)
        if self.services:
            self.services.now_playing.connect(self._on_playback)
        lay.addWidget(self.cmb_source)
        self.lb_audio = QLabel()
        self.lb_audio.setObjectName("Caption")
        self.lb_audio.setWordWrap(True)
        lay.addWidget(self.lb_audio)
        self.btn_audio_file = QPushButton("Elegir archivo…")
        self.btn_audio_file.setObjectName("Ghost")
        self.btn_audio_file.setCursor(Qt.PointingHandCursor)
        self.btn_audio_file.clicked.connect(self._choose_audio)
        lay.addWidget(self.btn_audio_file)
        self.wave = WaveformWidget()
        lay.addWidget(self.wave)
        row = QHBoxLayout()
        self.sl_offset, self.lb_offset = QSlider(Qt.Horizontal), QLabel("0 ms")
        self.lb_offset.setObjectName("Caption")
        self.lb_offset.setMinimumWidth(52)
        self.sl_offset.valueChanged.connect(self._offset_changed)
        offset_label = QLabel("Desfase")
        offset_label.setObjectName("Muted")
        self.btn_auto = QPushButton("Auto")
        self.btn_auto.setObjectName("Ghost")
        self.btn_auto.setCursor(Qt.PointingHandCursor)
        self.btn_auto.setToolTip("Detecta el desfase comparando los ataques del audio con los inicios de sílaba de la letra")
        self.btn_auto.clicked.connect(self._auto_offset)
        self._auto_busy = False
        row.addWidget(offset_label)
        row.addWidget(self.sl_offset, 1)
        row.addWidget(self.lb_offset)
        row.addWidget(self.btn_auto)
        lay.addLayout(row)
        lay.addStretch()
        return w

    def _watermark_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(0, theme.SPACE[3], 0, 0)
        form.setVerticalSpacing(theme.SPACE[2])
        s = self.settings
        self.ed_wm = QLineEdit(s.value("watermark/text", Style.wm_text))
        self.ed_wm.setPlaceholderText("Vacío = sin marca de agua")
        self.cmb_wm_side = QComboBox()
        self.cmb_wm_side.addItem("Esquina inferior izquierda", "left")
        self.cmb_wm_side.addItem("Esquina inferior derecha", "right")
        self.cmb_wm_side.setCurrentIndex(max(0, self.cmb_wm_side.findData(s.value("watermark/side", Style.wm_side))))
        self.cb_wm_glyph = QCheckBox("Ondas junto al texto")
        self.cb_wm_glyph.setChecked(s.value("watermark/glyph", False, type=bool))
        hint = QLabel("Queda fuera de la zona que tapa la interfaz de TikTok, entra con un fundido de 0.6 s y baja su opacidad "
                      "si cruza la línea que se está cantando.")
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        form.addRow("Texto", self.ed_wm)
        form.addRow("Posición", self.cmb_wm_side)
        form.addRow(self.cb_wm_glyph)
        form.addRow(hint)
        self.ed_wm.textChanged.connect(self._wm_changed)
        self.cmb_wm_side.currentIndexChanged.connect(self._wm_changed)
        self.cb_wm_glyph.toggled.connect(self._wm_changed)
        self._apply_wm()
        return w

    def _apply_wm(self):
        self.style.wm_text, self.style.wm_side = self.ed_wm.text(), self.cmb_wm_side.currentData()
        self.style.wm_glyph = self.cb_wm_glyph.isChecked()

    def _wm_changed(self):
        self._apply_wm()
        s = self.settings
        s.setValue("watermark/text", self.style.wm_text)
        s.setValue("watermark/side", self.style.wm_side)
        s.setValue("watermark/glyph", self.style.wm_glyph)
        self.preview.refresh()

    def _build_export_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(theme.SPACE[3])
        self.summary = QLabel()
        self.summary.setObjectName("Caption")
        self.btn_export = QPushButton("Exportar")
        self.btn_export.setObjectName("Primary")
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.clicked.connect(self.export)
        self.btn_menu = QPushButton()
        self.btn_menu.setObjectName("Ghost")
        self.btn_menu.setCursor(Qt.PointingHandCursor)
        self.btn_menu.setToolTip("Modo, formato y opciones de exportación")
        self.menu = self._build_menu()
        self.btn_menu.setMenu(self.menu)
        bar.addStretch()
        bar.addWidget(self.summary)
        bar.addWidget(self.btn_export)
        bar.addWidget(self.btn_menu)
        self.btn_export.setEnabled(False)
        self.btn_menu.setEnabled(False)
        self._update_summary()
        return bar

    def _build_menu(self) -> QMenu:
        m = QMenu(self)
        self.mode_actions = self._radio_submenu(m, None, MODES, "export/mode", "lyrics")
        m.addSeparator()
        self.preset_actions = self._radio_submenu(m, "Formato", {k: v[0] for k, v in PRESETS.items()}, "export/preset", "tiktok")
        self._radio_submenu(m, "Marcador de inicio", MARKERS, "export/marker", "none")
        for a in self._radio_submenu(m, "Outro", OUTROS, "export/outro_ms", "500").values():
            a.triggered.connect(lambda: self.project and self._range_changed())  # el preview y la onda muestran el nuevo final
        self.encoder_actions = self._radio_submenu(m, "Codificador", {"cpu": "CPU (libx264, mejor compresión)",
                                                                      "nvenc": "NVIDIA NVENC (más rápido)"}, "export/encoder", "cpu")
        m.addSeparator()
        m.addAction("Reemplazar letra con .ttml…", self._replace_ttml)
        m.addAction("Carpeta de salida…", self._choose_folder)
        self.act_sync_sep = m.addSeparator()
        self.act_sync = m.addAction("Sincronizar con audio (experimental)…", self._open_sync)
        self.act_sync.setVisible(False)
        self.act_sync_sep.setVisible(False)
        m.aboutToShow.connect(self._sync_menu)
        m.aboutToShow.connect(self._probe_nvenc)
        return m

    def _open_sync(self):
        from marea.ui.lab import open_review  # import perezoso: solo existe si la función experimental está activada
        open_review(self)

    def _sync_menu(self):
        """Ajustes también cambia formato y codificador: el menú se reajusta a lo guardado cada vez que se abre."""
        on = self.settings.value("experimental/align", False, type=bool)
        self.act_sync.setVisible(on)
        self.act_sync_sep.setVisible(on)
        for key, actions in (("export/preset", self.preset_actions), ("export/encoder", self.encoder_actions)):
            current = self.settings.value(key)
            if current in actions:
                actions[current].setChecked(True)
        self._update_summary()

    def _radio_submenu(self, menu: QMenu, title: str | None, options: dict, key: str, default: str) -> dict:
        target = menu.addMenu(title) if title else menu
        group, actions = QActionGroup(target), {}
        current = self.settings.value(key, default)
        for value, label in options.items():
            a = target.addAction(label)
            a.setCheckable(True)
            a.setChecked(value == current or (current not in options and value == default))
            a.triggered.connect(lambda _=False, k=key, v=value: (self.settings.setValue(k, v), self._update_summary(),
                                                                 self._refresh_audio_tab()))
            group.addAction(a)
            actions[value] = a
        return actions

    def _probe_nvenc(self):
        try:
            ok = ffmpeg.nvenc_available()
        except FileNotFoundError:
            ok = False
        self.encoder_actions["nvenc"].setEnabled(ok)
        if not ok and self.encoder_actions["nvenc"].isChecked():
            self.encoder_actions["cpu"].setChecked(True)
            self.settings.setValue("export/encoder", "cpu")

    def _refresh_icons(self):
        t = theme.current
        self.btn_play.setIcon(theme.icon("pause" if self.preview.playing else "play", t.text, 18))
        self.btn_menu.setIcon(theme.icon("chevron-down", t.text_muted, 16))

    # ---------------- proyecto ----------------
    def set_project(self, project: ClipProject):
        self.project = project
        self.style = Style(bloom=self.cb_bloom.isChecked(), title=project.title, artist=project.artist,
                           font_family=self.cmb_font.currentData() or "", credit=self.cb_credit.isChecked())
        self._apply_wm()
        for ed, text in ((self.ed_title, project.title), (self.ed_artist, project.artist)):
            ed.blockSignals(True)
            ed.setText(text)
            ed.blockSignals(False)
        self._linked = None
        self.list.clear()
        for line in project.lyrics.lines:
            item = QListWidgetItem(line.text)
            item.setToolTip(line.text)
            self.list.addItem(item)
        credit = "\n".join(f"{label} {text}" for label, text in project.lyrics.credit_lines())
        self.credit.setText(credit)
        self.credit.setVisible(bool(credit))
        n = len(project.lyrics.lines)
        self.first, self.last, self._anchor = 0, min(n - 1, DEFAULT_VERSES - 1), 0
        self.subtitle.setText(self._title_text())
        self.stack.setCurrentIndex(1)
        self.btn_export.setEnabled(True)
        self.btn_menu.setEnabled(True)
        self.preview.set_project(project, self.style, self.params, self._clip_range())
        self._range_changed(seek=False)
        self._pick_default_source()
        self.project_changed.emit(self._title_text())

    def _title_text(self) -> str:
        p = self.project
        return f"{p.artist} – {p.title}" if p.artist else p.title

    @property
    def mode(self) -> str:
        return next((k for k, a in self.mode_actions.items() if a.isChecked()), "lyrics")

    # ---------------- selección de versos ----------------
    def _on_verse_clicked(self, item):
        row = self.list.row(item)
        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
            self.first, self.last = sorted((self._anchor, row))
        else:
            self._anchor = self.first = self.last = row
        self._range_changed()

    def _on_verse_double_clicked(self, item):
        row = self.list.row(item)
        if self.first <= row <= self.last:
            start = self._clip_range()[0]
            self.preview.seek(self.project.lyrics.lines[row].start - start)
            self.preview.set_playing(True)

    def _range_changed(self, seek: bool = True):
        self.list.viewport().update()
        lines = self.project.lyrics.lines[self.first:self.last + 1]
        span = max(l.end for l in lines) - lines[0].start
        self.range_label.setText(f"Versos {self.first + 1}–{self.last + 1} · {fmt_ms(span)}")
        if seek:
            self.preview.set_range(self._clip_range())
        self._update_summary()
        self._refresh_wave()

    def _clip_range(self) -> tuple[float, float]:
        return clip_range(self.project.lyrics, self.first, self.last, float(self.settings.value("export/outro_ms", TAIL_MS)))

    # ---------------- preview ----------------
    def _on_position(self, ms: float, total: float):
        self.time.setText(f"{fmt_ms(ms)} / {fmt_ms(total)}")
        if not self._scrubbing:
            self.scrub.blockSignals(True)
            self.scrub.setValue(round(min(1.0, ms / total) * 1000))
            self.scrub.blockSignals(False)

    def _on_scrub(self, v: int):
        self.preview.seek(v / 1000 * self.preview.duration)

    def _on_playing(self, playing: bool):
        self._refresh_icons()

    def _credit_toggled(self, on: bool):
        self.style.credit = on
        self.preview.rebuild()  # el crédito es un elemento más de la lista: no basta con re-maquetar

    def _style_changed(self):
        blur, wob = self.sl_blur.value() / 10, self.sl_wob.value() / 100
        self.lb_blur.setText(f"{blur:.1f}×")
        self.lb_wob.setText(f"ζ {wob:.2f}")
        self.lb_amt.setText(f"{self.sl_amt.value()} %")
        self.params.vib, self.params.emph = self.cb_vib.isChecked(), self.cb_emph.isChecked()
        self.params.blurm, self.params.wob = blur, wob
        self.params.motion, self.params.motion_amt = self.cmb_motion.currentData(), self.sl_amt.value() / 100
        self.params.reduce = self.cb_reduce.isChecked()
        self.style.bloom = self.cb_bloom.isChecked()
        self.preview.restyle()

    def _font_changed(self):
        self.style.font_family = self.cmb_font.currentData() or ""
        self.preview.rebuild()

    def _meta_changed(self):
        if not self.project:
            return
        self.project.title, self.project.artist = self.ed_title.text(), self.ed_artist.text()
        self.style.title, self.style.artist = self.project.title, self.project.artist
        self.subtitle.setText(self._title_text())
        self.project_changed.emit(self._title_text())
        self.preview.refresh()

    def _choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, "Elegir portada", "", "Imágenes (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        try:
            self.project.cover = load_cover(path)
        except ValueError as e:
            self.notify(str(e), "error")
            return
        self.preview.rebuild()

    def _replace_ttml(self):
        """Cambia solo la letra: portada, título, artista, pista y audio del proyecto se conservan."""
        path, _ = QFileDialog.getOpenFileName(self, "Reemplazar letra", "", "Letras TTML (*.ttml *.xml)")
        if not path:
            return
        try:
            lyrics = project_from_ttml(path).lyrics
        except (ValueError, OSError) as e:
            self.notify(f"No pude abrir esa letra: {e} Usa un .ttml de Apple Music con tiempos por sílaba.", "error")
            return
        self.set_project(replace(self.project, lyrics=lyrics, source=Path(path)))

    # ---------------- audio ----------------
    def _spotify_track(self) -> Track | None:
        """Pista que se grabaría: la del proyecto si vino de Spotify, o la que se vinculó de «ahora suena»."""
        if not (self.project and self.services and self.services.connected):
            return None
        return self.project.track or self._linked

    def _spotify_ready(self) -> bool:
        return self._spotify_track() is not None

    def _spotify_hint(self) -> str:
        if not (self.services and self.services.connected):
            return "Conecta tu cuenta de Spotify en Ajustes; o elige «Archivo de audio»."
        return "No hay nada sonando en Spotify. Dale play a la canción y se vinculará sola; o elige «Archivo de audio»."

    def _link_playing(self):
        """Vincula (y congela) lo que suena en Spotify: cambiar de canción después no cambia lo que se graba."""
        s = self.services
        if self.project and not self.project.track and not self._linked and s and s.connected and s.playback:
            self._linked = s.playback.track

    def _source_changed(self, *_):
        if self.project and self.cmb_source.currentData() == "spotify":
            self._link_playing()
            if not self._spotify_ready() and self.services and self.services.connected:
                self.services.poll()  # puede que lo que suena aún no se haya consultado
        self._refresh_audio_tab()

    def _on_playback(self, _pb):
        if self.project and self.cmb_source.currentData() == "spotify":
            self._link_playing()
            self._refresh_audio_tab()

    def _pick_default_source(self):
        self.cmb_source.blockSignals(True)
        self.cmb_source.setCurrentIndex(0 if self._spotify_ready() else 1)
        self.cmb_source.blockSignals(False)
        self._refresh_audio_tab()

    def _refresh_audio_tab(self, *_):
        if not self.project:
            return
        source = self.cmb_source.currentData()
        is_local = source == "local"
        self.btn_audio_file.setVisible(is_local)
        self.wave.setVisible(is_local)
        lo, hi = LOCAL_OFFSET_RANGE if is_local else LOOPBACK_OFFSET_RANGE
        value = self.project.audio_offset_ms if is_local else float(self.settings.value("audio/loopback_offset_ms", 0))
        self.sl_offset.blockSignals(True)
        self.sl_offset.setRange(lo, hi)
        self.sl_offset.setValue(round(value))
        self.sl_offset.blockSignals(False)
        self.lb_offset.setText(f"{round(value):+d} ms")
        if is_local:
            name = self.project.audio_file.name if self.project.audio_file else "ningún archivo"
            self.lb_audio.setText(f"Archivo: {name}. Mueve el desfase hasta que los golpes de la forma de onda coincidan "
                                  "con las líneas azules (inicio de cada verso).")
        else:
            device = self.settings.value("audio/device", "") or "la salida por defecto de Windows"
            t = self._spotify_track()
            if t:
                link = f"Vinculada con «{t.artist} – {t.title}» (la que suena en tu Spotify). " if self._linked and not self.project.track else ""
                self.lb_audio.setText(f"{link}Se reproducirá la canción en tu Spotify y se grabará lo que suene por {device}. "
                                      "Requiere Spotify Premium y que nada más suene en ese momento. El desfase corrige la latencia de tu equipo.")
            else:
                self.lb_audio.setText(self._spotify_hint())
        self._refresh_wave()

    def _offset_changed(self, v: int):
        if not self.project:
            return
        self.lb_offset.setText(f"{v:+d} ms")
        if self.cmb_source.currentData() == "local":
            self.project.audio_offset_ms = float(v)
            self._refresh_wave()
        else:
            self.settings.setValue("audio/loopback_offset_ms", v)

    def _auto_offset(self):
        """Mide el desfase: con archivo local sobre todo el audio; con Spotify graba un tramo (~25 s) y mide sobre eso."""
        p = self.project
        if not p or self._auto_busy:
            return
        is_local = self.cmb_source.currentData() == "local"
        hi = (LOCAL_OFFSET_RANGE if is_local else LOOPBACK_OFFSET_RANGE)[1]
        lyrics = p.lyrics
        if is_local:
            if p.audio_samples is None:
                self.notify("Elige primero un archivo de audio para medir el desfase.", "error")
                return
            samples = p.audio_samples

            def job():
                return align.detect_offset(samples, local.RATE, lyrics, 0.0, hi)

            self.notify("Midiendo el desfase…")
        else:
            if not self._spotify_ready():
                self.notify(self._spotify_hint(), "error")
                return
            if self.queue.busy:
                self.notify("Espera a que termine la exportación en curso: Spotify no puede reproducir dos cosas a la vez.", "error")
                return
            start, end = clip_range(lyrics, self.first, self.last)
            end = min(end, start + AUTO_RECORD_MS)
            client, track, device = self.services.spotify, self._spotify_track(), self.settings.value("audio/device", "")

            def job():  # sin corregir (offset 0): lo que se mide es justo lo que hay que compensar
                segment, rate = record_song_range(client, track, start, end, device=device, offset_ms=0.0, fade_ms=0.0)
                return align.detect_offset(segment, rate, lyrics, start, hi)

            self.notify(f"Se reproducirá la canción en Spotify unos {(end - start) / 1000:.0f} s para medir el desfase…")
        self._auto_busy = True
        self.btn_auto.setEnabled(False)
        run_async(job, self._auto_done, self._auto_failed)

    def _auto_done(self, d: "align.Detection"):
        self._auto_busy = False
        self.btn_auto.setEnabled(True)
        if not d.reliable:
            self.notify("No pude medir el desfase con seguridad: la letra y el audio no coinciden lo bastante en este tramo. "
                        "Prueba con un tramo con más voz o ajústalo a mano.", "error")
            return
        v = round(d.offset_ms)  # la búsqueda se limita al rango del control y un pico en el borde ya es «no fiable»
        self.sl_offset.setValue(v)  # dispara _offset_changed: lo guarda y refresca la forma de onda
        self.notify(f"Desfase detectado: {v:+d} ms.", "ok")

    def _auto_failed(self, e):
        self._auto_busy = False
        self.btn_auto.setEnabled(True)
        known = (ValueError, AudioError, ProviderError)
        self.notify(str(e) if isinstance(e, known) else "No pude medir el desfase. Intenta de nuevo.", "error")

    def _refresh_wave(self):
        p = self.project
        if not p or p.audio_samples is None or self.cmb_source.currentData() != "local":
            self.wave.set_data(None, (0, 1), 0, [], (0, 1))
            return
        start, end = self._clip_range()
        view = (max(0.0, start - 1500), end + 1500)
        markers = [l.start for l in p.lyrics.lines if view[0] <= l.start <= view[1]]
        self.wave.set_data(p.audio_samples, view, p.audio_offset_ms, markers, (start, end))

    def _choose_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, "Elegir audio", "", AUDIO_FILTER)
        if not path:
            return
        self.notify("Leyendo el audio…")
        project = self.project

        def done(samples):
            project.audio_file, project.audio_samples = Path(path), samples
            self._refresh_audio_tab()

        run_async(lambda: local.decode(path), done,
                  lambda e: self.notify(str(e) if isinstance(e, AudioError) else "No pude leer ese archivo de audio.", "error"))

    # ---------------- export ----------------
    def _output_dir(self) -> Path:
        return Path(self.settings.value("output_dir", str(default_output_dir())))

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta de salida", str(self._output_dir()))
        if d:
            self.settings.setValue("output_dir", d)
            self._update_summary()

    def _update_summary(self):
        preset = PRESETS.get(self.settings.value("export/preset", "tiktok"), PRESETS["tiktok"])[0]
        self.summary.setText(f"{MODES[self.mode]} · {preset}")

    def _full_end_ms(self) -> float:
        p = self.project
        t = p.track or (self._spotify_track() if self.cmb_source.currentData() == "spotify" else None)
        if t and t.duration_ms:
            return float(t.duration_ms)
        if p.audio_samples is not None and self.cmb_source.currentData() == "local":
            return len(p.audio_samples) / local.RATE * 1000
        return max(l.end for l in p.lyrics.lines) + TAIL_MS

    def build_job(self, start_ms: float | None = None, end_ms: float | None = None, full: bool = False) -> ExportJob:
        preset = PRESETS.get(self.settings.value("export/preset", "tiktok"), PRESETS["tiktok"])
        if start_ms is None:
            start_ms, end_ms = self._clip_range()
        name = clip_name(self.project.artist, self.project.title, *((None, None) if full else (self.first, self.last)))
        return ExportJob(self.project.lyrics, self.project.cover_png(), unique_path(self._output_dir(), name), start_ms, end_ms,
                         size=(preset[1], preset[2]), fps=60, encoder=self.settings.value("export/encoder", "cpu"),
                         marker=self.settings.value("export/marker", "none"), params=replace(self.params),
                         style=replace(self.style))

    def _audio_spec(self, start_ms: float, end_ms: float) -> AudioSpec | None:
        """Qué audio lleva el clip, o None (tras avisar qué falta) si no se puede."""
        p = self.project
        if self.cmb_source.currentData() == "spotify":
            if not self._spotify_ready():
                self.notify(self._spotify_hint(), "error")
                return None
            return AudioSpec("spotify", start_ms, end_ms, track=self._spotify_track(), device=self.settings.value("audio/device", ""),
                             offset_ms=float(self.settings.value("audio/loopback_offset_ms", 0)))
        if p.audio_samples is None:
            self.notify("Elige un archivo de audio en la pestaña Audio para exportar con canción.", "error")
            return None
        return AudioSpec("local", start_ms, end_ms, samples=p.audio_samples, offset_ms=p.audio_offset_ms)

    def export(self):
        """Añade el clip a la cola de exportaciones (se procesa de uno en uno)."""
        if not self.project:
            return
        try:
            ffmpeg.find_ffmpeg()
            mode = self.mode
            full = mode == "full"
            start, end = (0.0, self._full_end_ms()) if full else self._clip_range()
            audio = None
            if mode != "lyrics":
                audio = self._audio_spec(start, end)
                if audio is None:
                    return
                if not full:
                    audio.fade_out_ms = float(self.settings.value("export/outro_ms", TAIL_MS))
            job = self.build_job(start, end, full)
            job.out_path.parent.mkdir(parents=True, exist_ok=True)
        except FileNotFoundError as e:
            self.notify(str(e), "error")
            return
        except OSError as e:
            self.notify(f"No pude crear la carpeta de salida ({e.strerror}). Elige otra en el menú de exportación.", "error")
            return
        item = QueueItem(self.project.title, self.project.artist, self.project.cover_png(), job, audio)
        self.preview.set_playing(False)
        self.queue.add(item)
        self.enqueued.emit(item)

    def shutdown(self):
        self.preview.shutdown()
