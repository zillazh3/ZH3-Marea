"""Editor: preview 9:16 en vivo, lista de versos con rango, pestañas de estilo/fondo/cinemático/audio y envío a la cola de export."""
import copy
import ctypes
import sys
from dataclasses import asdict, replace
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QFontDatabase, QPainter
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QSlider,
                               QStackedWidget, QStyle, QStyledItemDelegate, QTabWidget, QVBoxLayout, QWidget)

from marea.audio import align, local
from marea.audio.loopback import AudioError
from marea.audio.session import record_song_range
from marea.engine.engine import Params
from marea.engine.motion import MODES as MOTION_MODES
from marea.export import ffmpeg
from marea.export.job import PRESETS, TAIL_MS, ExportJob, clip_name, clip_range, unique_path
from marea.export.queue import AudioSpec, ExportQueue, QueueItem
from marea.model.cinematic import (
    DEFAULT_INTERVAL_MS,
    MIN_INTERVAL_MS,
    CinematicInterval,
    CinematicTreatment,
    compute_opening_trim_ms,
    suggest_intervals,
)
from marea.paths import default_output_dir, reveal_in_folder  # noqa: F401  (se reexportan)
from marea.project import ClipProject, ProjectError, load_cover, load_project_file, project_from_ttml, save_project
from marea.providers.errors import ProviderError
from marea.providers.spotify import Track
from marea.render.cinematic import VideoError, probe_video
from marea.render.scene import Style
from marea.ui import theme
from marea.ui.net import run_async
from marea.ui.preview import PreviewWidget
from marea.ui.screens import EmptyState, scrollable
from marea.ui.waveform import WaveformWidget

PANEL_W = 404
DEFAULT_VERSES = 6
MARKERS = {"none": "Ninguno", "flash": "Flash de 1 frame", "beep": "Pitido de 120 ms (solo sin audio)"}
MODES = {"lyrics": "Solo letra", "song": "Con canción", "full": "Canción completa"}
OUTROS = {"500": "0.5 s (corte seco)", "2000": "2 s", "4000": "4 s", "8000": "8 s"}  # ms tras el último verso, con fade-out del audio
LOOPBACK_OFFSET_RANGE, LOCAL_OFFSET_RANGE = (-500, 500), (-3000, 3000)  # ms
AUTO_RECORD_MS = 25_000  # cuánto de la canción se graba de Spotify para medir el desfase (más voz = estimación más segura)
AUDIO_FILTER = "Audio (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus);;Todos los archivos (*.*)"
VIDEO_FILTER = "Video (*.mp4 *.mov *.mkv *.webm *.avi *.m4v);;Todos los archivos (*.*)"
PROJECT_FILTER = "Proyecto ZH3 Marea (*.marea.json);;Todos los archivos (*.*)"


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
        self._scrub_timer = QTimer(self, singleShot=True, interval=16)
        self._scrub_timer.timeout.connect(self._flush_scrub)
        self.scrub.sliderPressed.connect(lambda: setattr(self, "_scrubbing", True))
        self.scrub.sliderReleased.connect(self._on_scrub_released)
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
        self.tabs = QTabWidget()
        self.tabs.addTab(self._style_tab(), "Estilo")
        self.tabs.addTab(self._background_tab(), "Fondo")
        self.tabs.addTab(self._cinematic_tab(), "Cinemático")
        self.tabs.addTab(self._audio_tab(), "Audio")
        self.tabs.addTab(self._watermark_tab(), "Marca de agua")
        tl.addWidget(self.tabs)
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
        self.cb_fxdebug = QCheckBox("Depurar efectos por palabra")  # experimental/wordfx: solo se ve con la función activada en Ajustes
        self.cb_fxdebug.setToolTip("Marca cada palabra con efecto semántico y muestra su lema, idioma y categorías (no sale en el export)")
        self.cb_fxdebug.setVisible(False)
        self.cb_fxdebug.toggled.connect(lambda _: self._wordfx_sync())
        form.addRow(self.cb_fxdebug)
        self.sl_fx, self.lb_fx = self._slider(0, 150, 100), QLabel("100 %")
        self.sl_fx.setToolTip("Intensidad global de los efectos por palabra (0 % los apaga sin desactivar la función)")
        self.fx_row = QWidget()
        row = QHBoxLayout(self.fx_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.lb_fx.setObjectName("Caption")
        self.lb_fx.setMinimumWidth(40)
        row.addWidget(self.sl_fx, 1)
        row.addWidget(self.lb_fx)
        form.addRow("Intensidad FX", self.fx_row)
        self.fx_row.setVisible(False)
        self.fx_label = form.labelForField(self.fx_row)
        self.fx_label.setVisible(False)
        self.cb_fxbg = QCheckBox("Adaptar color de efectos al fondo")
        self.cb_fxbg.setToolTip("Mide el fondo detrás de cada palabra con efecto y ajusta su color para que contraste, conservando el tono")
        self.cb_fxbg.setChecked(self.settings.value("wordfx/adapt_bg", True, type=bool))
        self.cb_fxbg.setVisible(False)
        self.cb_fxbg.toggled.connect(lambda v: (self.settings.setValue("wordfx/adapt_bg", v), self._wordfx_sync()))
        form.addRow(self.cb_fxbg)
        self._fx_timer = QTimer(self, singleShot=True, interval=16)  # coalesce rápido sin bloquear al arrastrar
        self._fx_timer.timeout.connect(self._wordfx_sync)
        self.sl_fx.valueChanged.connect(lambda v: (self.lb_fx.setText(f"{v} %"), self._fx_timer.start()))
        self.sl_fx.sliderReleased.connect(lambda: (self._fx_timer.stop(), self._wordfx_sync()))
        self._fx_watch = QTimer(self, interval=2500)  # hot reload: mira las fechas de los JSON mientras la función está activa
        self._fx_watch.timeout.connect(lambda: self.isVisible() and self._wordfx_sync())
        self._fx_state = None  # (función, depuración, intensidad, audio) con los que se armó la escena
        self._fx_plan = None  # FxPlan vigente: la preview y el export usan el mismo
        self._fx_palette = (None, ())  # (id de la portada, colores dominantes)
        self._fx_audio = (None, None)  # (id de las muestras, AudioStats) del audio local: tensión vocal, tempo, brillo, modo
        self._fx_profile = None  # SongProfile con el que se armó el plan (guardado o calculado)
        self._fx_cached_base = None  # (base_key, lib, rls, raw_matches, profile) para no recomputar al mover sliders
        self._fx_cached_refs = None  # mantiene referencias vivas para evitar colisiones de id()
        self._last_time_text = ""
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

    @staticmethod
    def _dspin(lo: float, hi: float, val: float, step: float = 0.2) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(2)
        sp.setSingleStep(step)
        sp.setSuffix(" s")
        sp.setValue(val)
        return sp

    def _cinematic_tab(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, theme.SPACE[3], theme.SPACE[1], 0)
        lay.setSpacing(theme.SPACE[2])

        top = QHBoxLayout()
        self.cb_cinematic = QCheckBox("Activar fondo cinemático")
        self.cb_cinematic.toggled.connect(self._cinematic_toggled)
        self.btn_video = QPushButton("Importar video…")
        self.btn_video.setObjectName("Ghost")
        self.btn_video.setCursor(Qt.PointingHandCursor)
        self.btn_video.clicked.connect(self._choose_video)
        top.addWidget(self.cb_cinematic, 1)
        top.addWidget(self.btn_video)
        lay.addLayout(top)

        self.lb_video_info = QLabel("Ningún video cargado. El audio del video se excluye siempre.")
        self.lb_video_info.setObjectName("Caption")
        self.lb_video_info.setWordWrap(True)
        lay.addWidget(self.lb_video_info)

        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE[2])
        self.btn_suggest = QPushButton("Sugerir")
        self.btn_suggest.setToolTip("Sugiere momentos usando interludios, pausas, acentos y cambios de energía")
        self.btn_add_iv = QPushButton("+ Momento")
        self.btn_add_iv.setToolTip("Crea un momento manual en la posición actual del preview")
        self.btn_del_iv = QPushButton("Quitar")
        self.btn_del_iv.setToolTip("Elimina el momento seleccionado")
        for b, fn in ((self.btn_suggest, self._suggest_cinematic),
                      (self.btn_add_iv, self._add_cinematic_interval),
                      (self.btn_del_iv, self._remove_cinematic_interval)):
            b.setObjectName("Ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            actions.addWidget(b)
        lay.addLayout(actions)

        self.iv_list = QListWidget()
        self.iv_list.setObjectName("Intervals")
        self.iv_list.setMinimumHeight(88)
        self.iv_list.setMaximumHeight(112)
        self.iv_list.currentRowChanged.connect(self._on_interval_selected)
        self.iv_list.itemDoubleClicked.connect(self._on_interval_double_clicked)
        lay.addWidget(self.iv_list)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(theme.SPACE[1])

        self.sp_open_trim = self._dspin(0, 36000, 0)
        self.sp_open_trim.setToolTip("Recorte inicial del video para igualar su duración con la pista musical")
        self.sp_open_trim.valueChanged.connect(self._opening_trim_edited)
        self.btn_open_auto = QPushButton("Auto")
        self.btn_open_auto.setObjectName("Ghost")
        self.btn_open_auto.setCursor(Qt.PointingHandCursor)
        self.btn_open_auto.setToolTip("Calcula el recorte inicial usando las duraciones reales del video y del audio")
        self.btn_open_auto.clicked.connect(self._auto_opening_trim)
        open_row = QHBoxLayout()
        open_row.setSpacing(theme.SPACE[1])
        open_row.addWidget(self.sp_open_trim, 1)
        open_row.addWidget(self.btn_open_auto)
        form.addRow("Recorte inicial", open_row)

        self.sp_iv_start, self.sp_iv_end = self._dspin(0, 36000, 0), self._dspin(0.2, 36000, 4.5)
        self.sp_vid_start, self.sp_vid_end = self._dspin(0, 36000, 0), self._dspin(0, 36000, 4.5)
        self.sp_fade_in, self.sp_fade_out = self._dspin(0, 15, 0.9, 0.1), self._dspin(0, 15, 1.1, 0.1)
        for sp in (self.sp_iv_start, self.sp_iv_end, self.sp_vid_start, self.sp_vid_end, self.sp_fade_in, self.sp_fade_out):
            sp.valueChanged.connect(self._interval_edited)

        for label, a, b in (("Canción", self.sp_iv_start, self.sp_iv_end),
                            ("Video", self.sp_vid_start, self.sp_vid_end),
                            ("Fundidos", self.sp_fade_in, self.sp_fade_out)):
            r = QHBoxLayout()
            r.setSpacing(theme.SPACE[1])
            sep = QLabel("→")
            sep.setObjectName("Caption")
            r.addWidget(a, 1)
            r.addWidget(sep)
            r.addWidget(b, 1)
            form.addRow(label, r)

        self.sl_cine_op, self.lb_cine_op = self._slider(20, 100, 100), QLabel("100 %")
        self.sl_cine_blur, self.lb_cine_blur = self._slider(0, 100, 35), QLabel("35 %")
        self.sl_cine_dim, self.lb_cine_dim = self._slider(0, 75, 32), QLabel("32 %")
        self.sl_cine_vig, self.lb_cine_vig = self._slider(0, 100, 45), QLabel("45 %")
        self.sl_cine_grain, self.lb_cine_grain = self._slider(0, 35, 8), QLabel("8 %")
        self.sl_cine_sat, self.lb_cine_sat = self._slider(0, 140, 82), QLabel("82 %")
        self.sl_cine_con, self.lb_cine_con = self._slider(60, 130, 92), QLabel("92 %")
        self._cine_sliders = (
            ("Opacidad", self.sl_cine_op, self.lb_cine_op),
            ("Desenfoque", self.sl_cine_blur, self.lb_cine_blur),
            ("Velo", self.sl_cine_dim, self.lb_cine_dim),
            ("Viñeta", self.sl_cine_vig, self.lb_cine_vig),
            ("Grano", self.sl_cine_grain, self.lb_cine_grain),
            ("Saturación", self.sl_cine_sat, self.lb_cine_sat),
            ("Contraste", self.sl_cine_con, self.lb_cine_con),
        )
        self._cine_timer = QTimer(self, singleShot=True, interval=16)
        self._cine_timer.timeout.connect(self.preview.update_cinematic)
        for name, sl, lb in self._cine_sliders:
            r = QHBoxLayout()
            lb.setObjectName("Caption")
            lb.setMinimumWidth(42)
            r.addWidget(sl, 1)
            r.addWidget(lb)
            form.addRow(name, r)
            sl.valueChanged.connect(self._treatment_changed)
            sl.sliderReleased.connect(self._flush_treatment)

        lay.addLayout(form)
        lay.addStretch()
        self._video_busy = False
        self._suggest_gen = 0
        return scrollable(inner)

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
        m.addAction("Guardar proyecto…", self.save_project_dialog)
        m.addAction("Abrir proyecto…", self.open_project_dialog)
        m.addAction("Reemplazar letra con .ttml…", self._replace_ttml)
        m.addAction("Carpeta de salida…", self._choose_folder)
        self.act_sync_sep = m.addSeparator()
        self.act_sync = m.addAction("Sincronizar con audio (experimental)…", self._open_sync)
        self.act_sync.setVisible(False)
        self.act_sync_sep.setVisible(False)
        self.act_fx_sep = m.addSeparator()
        self.act_fxprofile = m.addAction("Perfil de la canción (efectos por palabra)…", self._open_fx_profile)
        self.act_fxprofile.setVisible(False)
        self.act_fx_sep.setVisible(False)
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
        fx = self.settings.value("experimental/wordfx", False, type=bool)
        self.act_fxprofile.setVisible(fx)
        self.act_fx_sep.setVisible(fx)
        for key, actions in (("export/preset", self.preset_actions), ("export/encoder", self.encoder_actions)):
            current = self.settings.value(key)
            if current is None and key == "export/encoder":
                current = ffmpeg.default_encoder()
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
        elif ok and self.settings.value("export/encoder") is None:
            self.encoder_actions["nvenc"].setChecked(True)

    def _refresh_icons(self):
        t = theme.current
        self.btn_play.setIcon(theme.icon("pause" if self.preview.playing else "play", t.text, 18))
        self.btn_menu.setIcon(theme.icon("chevron-down", t.text_muted, 16))

    # ---------------- proyecto ----------------
    def set_project(self, project: ClipProject, editor_state: dict | None = None):
        self.project = project
        self._suggest_gen = 0
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
        if editor_state:
            self._restore_editor_controls(editor_state, n)
        self.subtitle.setText(self._title_text())
        self.stack.setCurrentIndex(1)
        self.btn_export.setEnabled(True)
        self.btn_menu.setEnabled(True)
        self._wordfx_sync(rebuild=False)
        self.preview.set_project(project, self.style, self.params, self._clip_range())
        self._range_changed(seek=False)
        self._pick_default_source()
        if editor_state and editor_state.get("audio_source") in ("spotify", "local"):
            idx = self.cmb_source.findData(editor_state["audio_source"])
            if idx >= 0:
                self.cmb_source.setCurrentIndex(idx)
        self._refresh_cinematic_tab()
        if project.audio_file and project.audio_samples is None and project.audio_file.is_file():
            self._load_saved_audio(project, project.audio_file)
        self.project_changed.emit(self._title_text())

    def editor_state(self) -> dict:
        return {
            "first": self.first,
            "last": self.last,
            "style": asdict(self.style),
            "params": asdict(self.params),
            "audio_source": self.cmb_source.currentData(),
        }

    def _restore_editor_controls(self, state: dict, n_lines: int):
        if "first" in state and "last" in state and n_lines > 0:
            f = max(0, min(n_lines - 1, int(state["first"])))
            l = max(f, min(n_lines - 1, int(state["last"])))
            self.first, self.last, self._anchor = f, l, f
        st = state.get("style") or {}
        pa = state.get("params") or {}
        for w, val in ((self.cb_bloom, st.get("bloom")), (self.cb_credit, st.get("credit")),
                       (self.cb_vib, pa.get("vib")), (self.cb_emph, pa.get("emph")), (self.cb_reduce, pa.get("reduce"))):
            if isinstance(val, bool):
                w.blockSignals(True)
                w.setChecked(val)
                w.blockSignals(False)
        if "font_family" in st:
            fi = self.cmb_font.findData(st["font_family"])
            if fi >= 0:
                self.cmb_font.blockSignals(True)
                self.cmb_font.setCurrentIndex(fi)
                self.cmb_font.blockSignals(False)
        if "motion" in pa:
            mi = self.cmb_motion.findData(pa["motion"])
            if mi >= 0:
                self.cmb_motion.blockSignals(True)
                self.cmb_motion.setCurrentIndex(mi)
                self.cmb_motion.blockSignals(False)
        for sl, key, mul in ((self.sl_blur, "blurm", 10), (self.sl_wob, "wob", 100), (self.sl_amt, "motion_amt", 100)):
            if key in pa:
                sl.blockSignals(True)
                sl.setValue(round(float(pa[key]) * mul))
                sl.blockSignals(False)
        blur, wob = self.sl_blur.value() / 10, self.sl_wob.value() / 100
        self.lb_blur.setText(f"{blur:.1f}×")
        self.lb_wob.setText(f"ζ {wob:.2f}")
        self.lb_amt.setText(f"{self.sl_amt.value()} %")
        self.params.vib, self.params.emph = self.cb_vib.isChecked(), self.cb_emph.isChecked()
        self.params.blurm, self.params.wob = blur, wob
        self.params.motion, self.params.motion_amt = self.cmb_motion.currentData(), self.sl_amt.value() / 100
        self.params.reduce = self.cb_reduce.isChecked()
        self.style.bloom = self.cb_bloom.isChecked()
        self.style.credit = self.cb_credit.isChecked()
        self.style.font_family = self.cmb_font.currentData() or ""

    def _load_saved_audio(self, project: ClipProject, audio_path: Path):
        def done(samples):
            if self.project is project:
                project.audio_samples = samples
                self._sync_auto_opening_trim()
                self._refresh_audio_tab()
                self._refresh_cinematic_tab()
                self._wordfx_sync()  # el RMS del audio entra en los efectos

        run_async(lambda: local.decode(audio_path), done, lambda _e: None)

    def save_project_dialog(self):
        if not self.project:
            return
        default_name = (self.project.project_path.name if self.project.project_path
                        else f"{self.project.title or 'proyecto'}.marea.json")
        start_dir = str((self.project.project_path.parent if self.project.project_path else self._output_dir()) / default_name)
        path, _ = QFileDialog.getSaveFileName(self, "Guardar proyecto", start_dir, PROJECT_FILTER)
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".marea.json"
        try:
            saved = save_project(self.project, path, editor_state=self.editor_state())
        except OSError as e:
            self.notify(f"No pude guardar el proyecto: {e}", "error")
            return
        self.notify(f"Proyecto guardado: {saved.name}", "ok")

    def open_project_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Abrir proyecto", "", PROJECT_FILTER)
        if path:
            self.open_project_path(path)

    def open_project_path(self, path: str | Path) -> bool:
        try:
            project, state = load_project_file(path)
        except (ProjectError, OSError) as e:
            self.notify(str(e), "error")
            return False
        self.set_project(project, editor_state=state)
        return True

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
        text = f"{fmt_ms(ms)} / {fmt_ms(total)}"
        if text != getattr(self, "_last_time_text", ""):
            self._last_time_text = text
            self.time.setText(text)
        if not self._scrubbing:
            val = round(min(1.0, ms / total) * 1000)
            if val != self.scrub.value():
                self.scrub.blockSignals(True)
                self.scrub.setValue(val)
                self.scrub.blockSignals(False)

    def _on_scrub(self, v: int):
        if self._scrubbing or self.scrub.isSliderDown():
            if not self._scrub_timer.isActive():
                self._scrub_timer.start()
        else:
            self.preview.seek(v / 1000 * self.preview.duration)

    def _flush_scrub(self):
        self.preview.seek(self.scrub.value() / 1000 * self.preview.duration)

    def _on_scrub_released(self):
        self._scrubbing = False
        self._scrub_timer.stop()
        self._flush_scrub()

    def _on_playing(self, playing: bool):
        self._refresh_icons()

    def showEvent(self, e):
        super().showEvent(e)
        self._wordfx_sync()  # Ajustes pudo activar o apagar la función mientras esta página no se veía

    def _wordfx_sync(self, rebuild: bool = True):
        """Semantic Word FX (experimental): arma el plan de la letra si la función está activa y lo pasa al preview."""
        on = self.settings.value("experimental/wordfx", False, type=bool)
        for w in (self.cb_fxdebug, self.cb_fxbg, self.fx_row, self.fx_label):
            w.setVisible(on)
        p = self.project
        if on and not self._fx_watch.isActive():
            self._fx_watch.start()
        elif not on:
            self._fx_watch.stop()
        stats = self._wordfx_audio_stats() if on and p else None
        state = (
            on,
            on and self.cb_fxdebug.isChecked(),
            self.sl_fx.value(),
            self.cb_fxbg.isChecked(),
            id(p) if p else None,
            id(p.lyrics) if p else None,
            id(p.cover) if p else None,
            id(p.audio_samples) if p else None,
            id(stats) if stats is not None else None,
            self._wordfx_files_key() if on and p else None,
        )
        if p is None or (rebuild and state == self._fx_state):
            return
        prev_state = self._fx_state
        if not on:
            self._fx_state = state
            self._fx_plan = self._fx_profile = self._fx_cached_base = self._fx_cached_refs = None
        elif (prev_state is not None and self._fx_plan is not None
              and prev_state[0] == state[0] and prev_state[2:] == state[2:]):
            # Solo cambió la casilla de depuración: no reconstruir el plan ni reclasificar.
            self._fx_state = state
        else:
            from marea.model.cinematic import cached_audio_analysis
            from marea.wordfx import library, rules, songs  # perezoso: con la función apagada no se importa nada de marea.wordfx
            from marea.wordfx.matcher import match_lyrics
            from marea.wordfx.palette import dominant_colors
            from marea.wordfx.plan import build_plan, track_key
            from marea.wordfx.profile import ProfileError, classify
            if self._fx_palette[0] != id(p.cover):
                self._fx_palette = (id(p.cover), dominant_colors(p.cover))
            try:
                # Non-blocking cache check: when audio_samples is loaded, _wordfx_audio_stats computes analyze_audio off-thread.
                analysis = cached_audio_analysis(p.audio_samples, local.RATE) if p.audio_samples is not None else None
                key = track_key(p.lyrics, p.track.id if p.track else "")
                base_key = (id(p.lyrics), key, state[9], id(stats) if stats is not None else None)
                if self._fx_cached_base is not None and self._fx_cached_base[0] == base_key:
                    _, lib, rls, raw_matches, profile = self._fx_cached_base
                else:
                    lib = library.load()
                    rls = rules.load()
                    raw_matches = match_lyrics(p.lyrics, lib, rules=rls)
                    profile = songs.load_profile(key) or classify(p.lyrics, lib, stats, track_id=key)  # lo guardado manda sobre lo calculado
                    self._fx_cached_base = (base_key, lib, rls, raw_matches, profile)
                self._fx_plan = build_plan(p.lyrics, lib, intensity=self.sl_fx.value() / 100, track_id=p.track.id if p.track else "",
                                           palette=self._fx_palette[1], analysis=analysis, profile=profile, audio_stats=stats,
                                           adapt_bg=self.cb_fxbg.isChecked(), rules=rls, matches=raw_matches)
                self._fx_profile = profile
                self._fx_cached_refs = (p, p.lyrics, p.cover, p.audio_samples, stats)
                self._fx_state = state
            except (library.LibraryError, rules.RulesError, songs.SongError, ProfileError) as err:
                self._fx_cached_base = None
                self.notify(f"Efectos por palabra: {err} Se mantiene el plan anterior.", "error")  # un JSON a medio editar no apaga todo
        self.preview.set_wordfx(self._fx_plan, state[1], rebuild=rebuild)

    def _wordfx_audio_stats(self):
        """AudioStats del audio local o None mientras se calculan (la primera vez, en segundo plano) o si no hay audio local."""
        p = self.project
        if p.audio_samples is None:
            return None
        key = id(p.audio_samples)
        if self._fx_audio[0] == key:
            return self._fx_audio[1]
        self._fx_audio = (key, None)
        samples, audio_file = p.audio_samples, p.audio_file

        def work():
            from marea.model.cinematic import analyze_audio
            from marea.wordfx import audiofeat
            stem = audiofeat.find_cached_stem(audio_file) if audio_file else None  # voz separada del laboratorio si ya existe
            return audiofeat.analyze(samples, local.RATE, stem=stem, analysis=analyze_audio(samples, local.RATE))

        def done(stats):
            if self.project is p and self._fx_audio[0] == key:
                self._fx_audio = (key, stats)
                self._wordfx_sync()

        run_async(work, done, lambda _e: None)  # sin estadísticas el plan sigue sin tensión vocal
        return None

    def _open_fx_profile(self):
        """Panel del perfil de ánimo: tags, arco y notas editables; guardar escribe el archivo de la canción y rehace el plan."""
        from PySide6.QtWidgets import QDialog

        from marea.ui.wordfx_profile import ProfileDialog
        from marea.wordfx import library, songs
        from marea.wordfx.plan import track_key
        from marea.wordfx.profile import ProfileError, classify
        p = self.project
        if p is None:
            return
        self._wordfx_sync()
        if self._fx_profile is None:
            self.notify("Activa los efectos por palabra en Ajustes para editar el perfil.", "info")
            return
        key = track_key(p.lyrics, p.track.id if p.track else "")
        stats = self._fx_audio[1] if p.audio_samples is not None else None
        try:
            lib = library.load()
        except library.LibraryError as err:
            self.notify(f"Biblioteca de efectos no válida: {err}", "error")
            return
        end = max((l.end for l in p.lyrics.lines), default=1000) / 1000
        arc_end = self._fx_profile.arc[-1].end if self._fx_profile.arc else 0.0
        dlg = ProfileDialog(self._fx_profile, max(end, arc_end), lambda notes: classify(p.lyrics, lib, stats, notes=notes, track_id=key),
                            p.title, self)
        if dlg.exec() == QDialog.Accepted:
            try:
                songs.save_profile(key, dlg.result_profile())
            except (songs.SongError, ProfileError, OSError) as err:
                self.notify(f"No pude guardar el perfil: {err}", "error")
                return
            self._fx_state = None
            self._fx_cached_base = None
            self._wordfx_sync()

    def _wordfx_files_key(self) -> tuple:
        """Fechas de los JSON editables (biblioteca, reglas y overrides de esta canción): si cambian, se rearma el plan (hot reload)."""
        from marea.wordfx import library, rules, songs
        from marea.wordfx.plan import track_key
        p = self.project
        files = (library.user_path(), library.BUNDLED, rules.user_path(), rules.BUNDLED,
                 songs.path_for(track_key(p.lyrics, p.track.id if p.track else "")))
        out = []
        for f in files:
            try:
                out.append(f.stat().st_mtime_ns)
            except OSError:
                out.append(None)
        return tuple(out)

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
        self._wordfx_sync(rebuild=False)
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

    # ---------------- fondo cinemático ----------------
    def _selected_interval(self) -> CinematicInterval | None:
        if not self.project:
            return None
        row = self.iv_list.currentRow()
        ivs = self.project.cinematic.intervals
        return ivs[row] if 0 <= row < len(ivs) else None

    @staticmethod
    def _fmt_interval_row(idx: int, iv: CinematicInterval) -> str:
        return f"{idx + 1} · {fmt_ms(iv.start_ms)}–{fmt_ms(iv.end_ms)}  ←  video {fmt_ms(iv.video_start_ms)}–{fmt_ms(iv.video_end_ms)}"

    def _sync_auto_opening_trim(self):
        p = self.project
        if not p or not p.cinematic.primary_video or not p.cinematic.auto_opening_trim:
            return
        res = p.cinematic.primary_video
        p.cinematic.opening_trim_ms = compute_opening_trim_ms(res.duration_ms, self._song_duration_ms(), resource=res)

    def _opening_trim_edited(self, *_):
        p = self.project
        if not p or not p.cinematic.primary_video:
            return
        res = p.cinematic.primary_video
        max_open = max(0.0, float(res.duration_ms) - MIN_INTERVAL_MS) if res.duration_ms > 0 else 36_000_000.0
        p.cinematic.auto_opening_trim = False
        p.cinematic.opening_trim_ms = min(max_open, max(0.0, round(self.sp_open_trim.value() * 1000.0, 1)))
        self._refresh_cinematic_tab()

    def _auto_opening_trim(self):
        p = self.project
        if not p or not p.cinematic.primary_video:
            return
        p.cinematic.auto_opening_trim = True
        self._sync_auto_opening_trim()
        self._refresh_cinematic_tab()
        self.notify(f"Recorte inicial ajustado a {fmt_ms(p.cinematic.opening_trim_ms)}.", "ok")

    def _refresh_cinematic_tab(self, keep_id: str | None = None):
        p = self.project
        if not p:
            return
        cfg = p.cinematic
        self.cb_cinematic.blockSignals(True)
        self.cb_cinematic.setChecked(cfg.enabled)
        self.cb_cinematic.blockSignals(False)

        res = cfg.primary_video
        self.btn_video.setText("Reemplazar video…" if res else "Importar video…")
        if res:
            fps_txt = f"{int(round(res.fps))} fps" if abs(res.fps - round(res.fps)) < 0.01 else f"{res.fps:.2f} fps"
            trim_txt = f" · inicio +{fmt_ms(cfg.opening_trim_ms)}" if cfg.opening_trim_ms >= 50.0 else ""
            missing = " · no encontrado" if not Path(res.path).is_file() else ""
            self.lb_video_info.setText(
                f"{Path(res.path).name} · {fmt_ms(res.duration_ms)} · {res.width}×{res.height} · {fps_txt}{trim_txt}{missing}"
            )
        else:
            self.lb_video_info.setText("Ningún video cargado. El audio del video se excluye siempre.")

        prev_id = keep_id or (self._selected_interval().id if self._selected_interval() else "")
        self.iv_list.blockSignals(True)
        self.iv_list.clear()
        sel_row = -1
        for i, iv in enumerate(cfg.intervals):
            self.iv_list.addItem(self._fmt_interval_row(i, iv))
            if iv.id == prev_id:
                sel_row = i
        if sel_row < 0 and cfg.intervals:
            sel_row = 0
        if sel_row >= 0:
            self.iv_list.setCurrentRow(sel_row)
        self.iv_list.blockSignals(False)

        has_video = res is not None
        self.btn_suggest.setEnabled(has_video)
        self.btn_add_iv.setEnabled(has_video)
        self.sp_open_trim.setEnabled(has_video)
        self.btn_open_auto.setEnabled(has_video)
        max_open_s = max(0.0, (res.duration_ms - MIN_INTERVAL_MS) / 1000.0) if (res and res.duration_ms > 0) else 36000.0
        self.sp_open_trim.blockSignals(True)
        self.sp_open_trim.setMaximum(max(0.2, max_open_s))
        self.sp_open_trim.setValue(cfg.opening_trim_ms / 1000.0 if has_video else 0.0)
        self.sp_open_trim.blockSignals(False)
        self._sync_interval_controls()

    def _sync_interval_controls(self):
        p = self.project
        iv = self._selected_interval()
        has_iv = iv is not None
        self.btn_del_iv.setEnabled(has_iv)
        for sp in (self.sp_iv_start, self.sp_iv_end, self.sp_vid_start, self.sp_vid_end, self.sp_fade_in, self.sp_fade_out):
            sp.setEnabled(has_iv)
        res = p.cinematic.video_for(iv) if (p and iv) else (p.cinematic.primary_video if p else None)
        max_v = res.duration_ms / 1000.0 if (res and res.duration_ms > 0) else 36000.0
        if iv is not None:
            for sp, val, hi in (
                (self.sp_iv_start, iv.start_ms / 1000.0, 36000.0),
                (self.sp_iv_end, iv.end_ms / 1000.0, 36000.0),
                (self.sp_vid_start, iv.video_start_ms / 1000.0, max_v),
                (self.sp_vid_end, iv.video_end_ms / 1000.0, max_v),
                (self.sp_fade_in, iv.fade_in_ms / 1000.0, 15.0),
                (self.sp_fade_out, iv.fade_out_ms / 1000.0, 15.0),
            ):
                sp.blockSignals(True)
                sp.setMaximum(max(0.2, hi))
                sp.setValue(val)
                sp.blockSignals(False)
        tr = (iv.treatment if iv is not None else (p.cinematic.default_treatment if p else CinematicTreatment())).clamped()
        vals = (
            round(tr.opacity * 100),
            round(tr.blur * 100),
            round(tr.dim * 100),
            round(tr.vignette * 100),
            round(tr.grain * 100),
            round(tr.saturation * 100),
            round(tr.contrast * 100),
        )
        for (_, sl, lb), v in zip(self._cine_sliders, vals):
            sl.blockSignals(True)
            sl.setValue(v)
            sl.blockSignals(False)
            lb.setText(f"{v} %")

    def _cinematic_toggled(self, on: bool):
        if not self.project:
            return
        self.project.cinematic.enabled = on
        self.preview.update_cinematic()

    def _choose_video(self):
        if not self.project or self._video_busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Elegir video musical", "", VIDEO_FILTER)
        if path:
            self.import_video(path)

    def import_video(self, path: str | Path):
        """Inspecciona e importa (o reemplaza) el video en un hilo sin bloquear la interfaz."""
        if not self.project or self._video_busy:
            return
        project = self.project
        vid_id = project.cinematic.next_video_id()
        had_video = project.cinematic.primary_video is not None
        self._video_busy = True
        self.btn_video.setEnabled(False)
        self.notify("Inspeccionando el video…")

        def done(resource):
            self._video_busy = False
            self.btn_video.setEnabled(True)
            if self.project is not project:
                return
            project.cinematic.set_primary_video(resource)
            self._sync_auto_opening_trim()
            if not had_video:
                project.cinematic.enabled = True
            self._refresh_cinematic_tab()
            self.preview.update_cinematic()
            self.notify(f"Video listo: {Path(resource.path).name} ({resource.width}×{resource.height}).", "ok")

        def failed(err):
            self._video_busy = False
            self.btn_video.setEnabled(True)
            msg = str(err) if isinstance(err, VideoError) else "No pude abrir ese archivo de video."
            self.notify(msg, "error")

        run_async(lambda: probe_video(path, vid_id), done, failed)

    def _suggest_cinematic(self):
        p = self.project
        if not p or not p.cinematic.primary_video:
            self.notify("Importa primero un video para sugerir momentos.", "error")
            return
        self._sync_auto_opening_trim()
        res = p.cinematic.primary_video
        samples = p.audio_samples if self.cmb_source.currentData() == "local" else None
        song_dur = self._song_duration_ms()
        gen_seed = self._suggest_gen if self._suggest_gen > 0 else None
        suggested = suggest_intervals(
            p.lyrics,
            res.duration_ms,
            audio_samples=samples,
            sample_rate=local.RATE,
            audio_offset_ms=p.audio_offset_ms if samples is not None else 0.0,
            range_ms=(0.0, song_dur),
            treatment=p.cinematic.default_treatment,
            video_id=res.id,
            resource=res,
            opening_trim_ms=p.cinematic.opening_trim_ms,
            song_duration_ms=song_dur,
            seed=gen_seed,
        )
        self._suggest_gen += 1
        if not suggested:
            self.notify("No encontré momentos claros en esta canción. Puedes añadir uno manual con «+ Momento».", "info")
            return
        p.cinematic.intervals = suggested
        p.cinematic.enabled = True
        c_start, c_end = self._clip_range()
        focus_iv = next((iv for iv in suggested if iv.end_ms > c_start and iv.start_ms < c_end), suggested[0])
        self._refresh_cinematic_tab(keep_id=focus_iv.id)
        self.preview.update_cinematic()
        self.notify(f"Se sugirieron {len(suggested)} momentos sincronizados con la canción.", "ok")

    def _add_cinematic_interval(self):
        p = self.project
        if not p or not p.cinematic.primary_video:
            return
        c_start, c_end = self._clip_range()
        cur_ms = self.preview.scene.engine.t * 1000.0 if self.preview.scene else c_start
        start_ms = max(0.0, cur_ms)
        end_ms = min(max(c_end, start_ms + MIN_INTERVAL_MS), start_ms + DEFAULT_INTERVAL_MS)
        if end_ms - start_ms < MIN_INTERVAL_MS:
            start_ms = max(0.0, end_ms - DEFAULT_INTERVAL_MS)
        res = p.cinematic.primary_video
        dur = end_ms - start_ms
        open_ms = p.cinematic.opening_trim_ms
        song_dur = max(1.0, self._song_duration_ms())
        v_max = max(0.0, float(res.duration_ms or DEFAULT_INTERVAL_MS))
        v_usable = max(0.0, v_max - open_ms - dur)
        v_offset = open_ms + min(1.0, start_ms / max(1.0, song_dur - dur)) * v_usable if v_max > dur else 0.0
        iv = p.cinematic.add_interval(start_ms, end_ms, video_start_ms=v_offset, video_end_ms=v_offset + dur)
        p.cinematic.enabled = True
        self._refresh_cinematic_tab(keep_id=iv.id)
        self.preview.update_cinematic()

    def _remove_cinematic_interval(self):
        p, iv = self.project, self._selected_interval()
        if not p or not iv:
            return
        p.cinematic.remove_interval(iv.id)
        self._refresh_cinematic_tab()
        self.preview.update_cinematic()

    def _on_interval_selected(self, _row: int):
        self._sync_interval_controls()

    def _on_interval_double_clicked(self, _item):
        iv = self._selected_interval()
        if not iv or not self.project:
            return
        c_start, c_end = self._clip_range()
        if c_start <= iv.start_ms <= c_end:
            self.preview.seek(iv.start_ms - c_start)

    def _interval_edited(self, *_):
        p, iv = self.project, self._selected_interval()
        if not p or not iv:
            return
        start_ms = round(self.sp_iv_start.value() * 1000.0, 1)
        end_ms = round(self.sp_iv_end.value() * 1000.0, 1)
        if end_ms < start_ms + 200.0:
            end_ms = start_ms + 200.0
            self.sp_iv_end.blockSignals(True)
            self.sp_iv_end.setValue(end_ms / 1000.0)
            self.sp_iv_end.blockSignals(False)
        res = p.cinematic.video_for(iv)
        max_v = float(res.duration_ms) if (res and res.duration_ms > 0) else 36_000_000.0
        v_start = min(max_v, round(self.sp_vid_start.value() * 1000.0, 1))
        v_end = min(max_v, max(v_start, round(self.sp_vid_end.value() * 1000.0, 1)))
        if abs(v_end - self.sp_vid_end.value() * 1000.0) > 50:
            self.sp_vid_end.blockSignals(True)
            self.sp_vid_end.setValue(v_end / 1000.0)
            self.sp_vid_end.blockSignals(False)
        iv.start_ms, iv.end_ms = start_ms, end_ms
        iv.video_start_ms, iv.video_end_ms = v_start, v_end
        iv.fade_in_ms = round(self.sp_fade_in.value() * 1000.0, 1)
        iv.fade_out_ms = round(self.sp_fade_out.value() * 1000.0, 1)
        keep_id = iv.id
        p.cinematic.sort_intervals()
        self._refresh_cinematic_tab(keep_id=keep_id)
        self.preview.update_cinematic()

    def _treatment_changed(self, *_):
        if not self.project:
            return
        tr = CinematicTreatment(
            opacity=self.sl_cine_op.value() / 100.0,
            blur=self.sl_cine_blur.value() / 100.0,
            dim=self.sl_cine_dim.value() / 100.0,
            vignette=self.sl_cine_vig.value() / 100.0,
            grain=self.sl_cine_grain.value() / 100.0,
            saturation=self.sl_cine_sat.value() / 100.0,
            contrast=self.sl_cine_con.value() / 100.0,
        ).clamped()
        for _, sl, lb in self._cine_sliders:
            lb.setText(f"{sl.value()} %")
        self.project.cinematic.default_treatment = copy.copy(tr)
        iv = self._selected_interval()
        if iv is not None:
            iv.treatment = tr
        if any(sl.isSliderDown() for _, sl, _ in self._cine_sliders):
            if not self._cine_timer.isActive():
                self._cine_timer.start()
        else:
            self.preview.update_cinematic()

    def _flush_treatment(self):
        if self._cine_timer.isActive():
            self._cine_timer.stop()
            self.preview.update_cinematic()

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
        self._sync_auto_opening_trim()
        self._refresh_audio_tab()
        self._refresh_cinematic_tab()

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
            self._sync_auto_opening_trim()
            self._refresh_wave()
            self._refresh_cinematic_tab()
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
            if self.project is project:
                self._sync_auto_opening_trim()
                self._refresh_audio_tab()
                self._refresh_cinematic_tab()
                self._wordfx_sync()

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

    def _song_duration_ms(self) -> float:
        """Duración real de la pista musical activa (prioriza las muestras decodificadas del audio local)."""
        p = self.project
        if not p:
            return float(TAIL_MS)
        source = self.cmb_source.currentData()
        if source == "local" and p.audio_samples is not None:
            audio_ms = len(p.audio_samples) / local.RATE * 1000.0
            return max(1000.0, audio_ms - p.audio_offset_ms)
        if source == "spotify":
            t = p.track or self._spotify_track()
            if t and t.duration_ms:
                return float(t.duration_ms)
        if p.track and p.track.duration_ms:
            return float(p.track.duration_ms)
        if p.audio_samples is not None:
            return max(1000.0, len(p.audio_samples) / local.RATE * 1000.0 - p.audio_offset_ms)
        if p.lyrics.lines:
            return float(max(l.end for l in p.lyrics.lines) + TAIL_MS)
        return float(TAIL_MS)

    def _full_end_ms(self) -> float:
        return self._song_duration_ms()

    def build_job(self, start_ms: float | None = None, end_ms: float | None = None, full: bool = False) -> ExportJob:
        preset = PRESETS.get(self.settings.value("export/preset", "tiktok"), PRESETS["tiktok"])
        if start_ms is None:
            start_ms, end_ms = self._clip_range()
        name = clip_name(self.project.artist, self.project.title, *((None, None) if full else (self.first, self.last)))
        return ExportJob(self.project.lyrics, self.project.cover_png(), unique_path(self._output_dir(), name), start_ms, end_ms,
                         size=(preset[1], preset[2]), fps=60, encoder=self.settings.value("export/encoder") or ffmpeg.default_encoder(),
                         marker=self.settings.value("export/marker", "none"), params=replace(self.params),
                         style=replace(self.style), cinematic=copy.deepcopy(self.project.cinematic), wordfx=self._fx_plan)

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
