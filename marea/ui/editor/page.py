"""Editor: live 9:16 preview, verse range selection, style/cinematic/audio tabs, and export queue dispatch."""
import copy
import ctypes
import sys
from dataclasses import asdict, fields, replace
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from marea import library
from marea.async_util import run_async
from marea.audio import align, local
from marea.engine.engine import Params
from marea.export import ffmpeg
from marea.export.ffmpeg import ALPHA_TARGETS, TARGETS
from marea.export.job import PRESETS, TAIL_MS, ExportJob, clip_name, clip_range
from marea.export.queue import AudioSpec, ExportQueue, QueueItem
from marea.model.background import BackgroundConfig
from marea.model.lyrics import SOURCE_LRCLIB, SOURCE_MANUAL, SOURCE_SPICY, SOURCE_UNKNOWN
from marea.paths import default_output_dir
from marea.presets import schema as preset_schema
from marea.project import ClipProject, ProjectError, load_cover, load_project_file, project_from_ttml, save_project
from marea.providers.errors import ProviderError
from marea.providers.track import Track
from marea.render.scene import Style
from marea.ui import lang, theme
from marea.ui.dialogs import CandidateDialog, CandidateItem
from marea.ui.editor._pkg import pkg_attr
from marea.ui.editor.audio_tab import AudioController
from marea.ui.editor.cinematic_tab import CinematicController
from marea.ui.editor.export_bar import ExportBarController
from marea.ui.editor.hooks_panel import HooksController
from marea.ui.editor.loop_controls import LoopControls
from marea.ui.editor.meta_tabs import build_background_tab, build_watermark_tab
from marea.ui.editor.presets_panel import PresetsController
from marea.ui.editor.style_tab import build_style_tab
from marea.ui.editor.wordfx_controller import WordFXController
from marea.ui.preview import PreviewWidget
from marea.ui.services import decode_cover
from marea.ui.source_tag import SourceTagButton, source_label
from marea.ui.widgets import ClickSeekSlider, CoalescedAction, EmptyState, blocked_signals, fmt_ms

PANEL_W = 448  # las 6 pestañas (con Anzuelo) caben sin botones de desplazamiento
DEFAULT_VERSES = 6
SCRUB_MAX = 10000


def project_filter() -> str:
    """File-dialog filter for project files, in the current UI language."""
    return lang.t("editor.projectFilter")


def system_reduces_motion() -> bool:
    """Windows: «Mostrar animaciones en Windows» desactivado (SPI_GETCLIENTAREAANIMATION)."""
    sys_mod = pkg_attr("sys", sys)
    if sys_mod.platform != "win32":
        return False
    on = ctypes.c_int(1)
    ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)
    return bool(ok) and not on.value


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
        hover = e.hook_hover is not None and e.hook_hover[0] <= row <= e.hook_hover[1]
        r = QRectF(opt.rect).adjusted(2, 1, -2, -1)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if inside:
            p.setBrush(theme.qc(t.pill))
        elif hover or opt.state & QStyle.State_MouseOver:
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
    library_changed = Signal()  # se anotó un proyecto en la Biblioteca

    def __init__(self, settings: QSettings, notify, queue: ExportQueue, services=None, prefer_gpu: bool = True):
        """notify(texto, tipo='info'|'ok'|'error', accion=None) muestra un toast."""
        super().__init__()
        self.setObjectName("Page")
        self.settings, self.notify, self.queue, self.services = settings, notify, queue, services
        self.project: ClipProject | None = None
        self._linked = None
        self._encoder_warned: set[tuple[str, str, str, str]] = set()
        self.style, self.params = Style(), Params()
        self.first = self.last = self._anchor = 0
        self._scrubbing = False
        self._last_time_text = ""

        self.hook_hover: tuple[int, int] | None = None  # rango de versos resaltado por una sugerencia de Anzuelo
        self._presets = PresetsController(self)
        self._hooks = HooksController(self)
        self._cinematic = CinematicController(self)
        self._audio = AudioController(self)
        self._export_bar = ExportBarController(self)
        self._loops = LoopControls(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[5])
        root.setSpacing(theme.SPACE[3])
        head = QHBoxLayout()
        title = QLabel()
        lang.bind(title, "editor.title")
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
        empty = EmptyState("sliders-horizontal", "", "")
        lang.bind(empty.head, "editor.empty.title")
        lang.bind(empty.body, "editor.empty.message")
        QVBoxLayout(card).addWidget(empty)
        self.stack.addWidget(card)
        self.stack.addWidget(self._build_body(prefer_gpu))
        root.addWidget(self.stack, 1)
        root.addLayout(self._export_bar.build_bar())
        root.addWidget(self._loops.build())
        lang.bind_fn(self, "loop", self.refresh_loop)
        self._wordfx = WordFXController(self)
        theme.signals.changed.connect(self._refresh_icons)
        self._refresh_icons()

    # Expose WordFX state attributes used by existing UI tests cleanly via properties.
    @property
    def _fx_watch(self):
        return self._wordfx.watch

    @property
    def _fx_plan(self):
        return self._wordfx.plan

    @_fx_plan.setter
    def _fx_plan(self, val):
        self._wordfx.plan = val

    @property
    def _fx_profile(self):
        return self._wordfx.profile

    @_fx_profile.setter
    def _fx_profile(self, val):
        self._wordfx.profile = val

    @property
    def _fx_state(self):
        return self._wordfx.state

    @_fx_state.setter
    def _fx_state(self, val):
        self._wordfx.state = val

    @property
    def _fx_audio(self):
        return self._wordfx._audio

    @property
    def _fx_palette(self):
        return self._wordfx._palette

    @property
    def _fx_cached_base(self):
        return self._wordfx._cached_base

    @_fx_cached_base.setter
    def _fx_cached_base(self, val):
        self._wordfx._cached_base = val

    @property
    def _auto_busy(self) -> bool:
        return self._audio._auto_busy

    @_auto_busy.setter
    def _auto_busy(self, val: bool):
        self._audio._auto_busy = val

    @property
    def _video_busy(self) -> bool:
        return self._cinematic._video_busy

    @_video_busy.setter
    def _video_busy(self, val: bool):
        self._cinematic._video_busy = val

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
        ctrl.setSpacing(theme.SPACE[1])
        self.btn_play = QPushButton()
        self.btn_play.setObjectName("IconBtn")
        self.btn_play.setFixedSize(36, 36)
        self.btn_play.setCursor(Qt.PointingHandCursor)
        lang.bind(self.btn_play, "editor.playPause", "setToolTip")
        self.btn_play.clicked.connect(lambda: self.preview.set_playing(not self.preview.playing))
        self.btn_stop = QPushButton()
        self.btn_stop.setObjectName("IconBtn")
        self.btn_stop.setFixedSize(32, 32)
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        lang.bind(self.btn_stop, "editor.stop", "setToolTip")
        self.btn_stop.clicked.connect(self.preview.stop)
        self.scrub = ClickSeekSlider(Qt.Horizontal)
        self.scrub.setRange(0, SCRUB_MAX)
        self._scrub_action = CoalescedAction(16, self._flush_scrub, self)
        self._wave_seek_ms: float | None = None
        self._wave_action = CoalescedAction(16, self._flush_wave_seek, self)
        self.scrub.sliderPressed.connect(lambda: setattr(self, "_scrubbing", True))
        self.scrub.sliderReleased.connect(self._on_scrub_released)
        self.scrub.valueChanged.connect(self._on_scrub)
        self.time = QLabel("0:00 / 0:00")
        self.time.setObjectName("Caption")
        self.time.setStyleSheet("font-variant-numeric: tabular-nums;")
        self.btn_mute = QPushButton()
        self.btn_mute.setObjectName("IconBtn")
        self.btn_mute.setFixedSize(30, 30)
        self.btn_mute.setCursor(Qt.PointingHandCursor)
        self._preview_muted = self.settings.value("preview/muted", False, type=bool)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.sl_volume = ClickSeekSlider(Qt.Horizontal)
        self.sl_volume.setRange(0, 100)
        vol = max(0, min(100, int(self.settings.value("preview/volume", 100))))
        self.sl_volume.setValue(vol)
        self.sl_volume.setFixedWidth(68)
        lang.bind(self.sl_volume, "editor.volume", "setToolTip")
        self.sl_volume.valueChanged.connect(self._on_volume_changed)
        self.preview.set_volume(vol / 100.0)
        self.preview.set_muted(self._preview_muted)
        self._refresh_mute_tip()
        ctrl.addWidget(self.btn_play)
        ctrl.addWidget(self.btn_stop)
        ctrl.addWidget(self.scrub, 1)
        ctrl.addWidget(self.time)
        ctrl.addSpacing(theme.SPACE[1])
        ctrl.addWidget(self.btn_mute)
        ctrl.addWidget(self.sl_volume)
        left.addLayout(ctrl)
        left.addWidget(self._loops.build_preview_switch(), alignment=Qt.AlignLeft)
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(theme.SPACE[3])
        verses = QFrame()
        verses.setObjectName("Card")
        vl = QVBoxLayout(verses)
        vl.setContentsMargins(theme.SPACE[2], theme.SPACE[3], theme.SPACE[2], theme.SPACE[3])
        hint = QLabel()
        lang.bind(hint, "editor.versesHint")
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
        self.source_tag = SourceTagButton(SOURCE_UNKNOWN)
        self.source_tag.clicked.connect(self._open_source_menu)
        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, theme.SPACE[2], 0)
        meta_row.setSpacing(theme.SPACE[2])
        meta_row.addWidget(self.range_label, 1)
        meta_row.addWidget(self.source_tag)
        self.credit = QLabel()
        self.credit.setObjectName("Caption")
        self.credit.setTextFormat(Qt.PlainText)
        self.credit.setWordWrap(True)
        self.credit.setContentsMargins(theme.SPACE[2], 0, 0, 0)
        vl.addWidget(hint)
        vl.addWidget(self.list, 1)
        vl.addLayout(meta_row)
        vl.addWidget(self.credit)
        right.addWidget(verses, 3)

        tabs_card = QFrame()
        tabs_card.setObjectName("Card")
        tl = QVBoxLayout(tabs_card)
        tl.setContentsMargins(theme.SPACE[3], theme.SPACE[2], theme.SPACE[3], theme.SPACE[3])
        self.tabs = QTabWidget()
        self._add_tab(build_style_tab(self, system_reduces_motion), lambda: lang.t("editor.tabs.style"))
        self._add_tab(build_background_tab(self), lambda: lang.t("editor.tabs.background"))
        self._add_tab(self._cinematic.build_tab(), lambda: lang.t("editor.tabs.cinematic"))
        self._add_tab(self._audio.build_tab(), lambda: lang.t("editor.tabs.audio"))
        self._add_tab(build_watermark_tab(self), lambda: lang.t("editor.tabs.watermark"))
        self._add_tab(self._hooks.build_tab(), lambda: lang.t("editor.tabs.hooks"))
        tl.addWidget(self.tabs)
        right.addWidget(tabs_card, 2)
        container = QWidget()
        container.setFixedWidth(PANEL_W)
        container.setLayout(right)
        right.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(container)
        return body

    def _add_tab(self, widget: QWidget, title) -> None:
        """Add a tab whose name (`title()` -> text) follows the UI language; the widget itself is never rebuilt."""
        idx = self.tabs.addTab(widget, "")
        lang.bind_fn(self.tabs, f"tab-{idx}", lambda: self.tabs.setTabText(idx, title()))

    def _open_sync(self):
        from marea.ui.lab import open_review  # lazy import when experimental/align is active
        self.preview.set_playing(False)
        open_review(self)

    def _refresh_icons(self):
        t = theme.current
        self.btn_play.setIcon(theme.icon("pause" if self.preview.playing else "play", t.text, 18))
        self.btn_stop.setIcon(theme.icon("square", t.text_muted, 15))
        self.btn_mute.setIcon(theme.icon("volume-x" if self._preview_muted else "volume-2", t.text_muted, 16))
        self.btn_menu.setIcon(theme.icon("ellipsis", t.text_muted, 16))
        self.btn_format.setIcon(theme.icon("chevron-down", t.text_muted, 16))
        self.btn_format.setLayoutDirection(Qt.RightToLeft)  # chevron after the label

    def _refresh_mute_tip(self):
        lang.bind(self.btn_mute, "editor.unmute" if self._preview_muted else "editor.mute", "setToolTip")

    def _toggle_mute(self):
        self._preview_muted = not self._preview_muted
        self.settings.setValue("preview/muted", self._preview_muted)
        self.preview.set_muted(self._preview_muted)
        self._refresh_mute_tip()
        self._refresh_icons()

    def _on_volume_changed(self, value: int):
        self.settings.setValue("preview/volume", value)
        self.preview.set_volume(value / 100.0)
        if value > 0 and self._preview_muted:
            self._preview_muted = False
            self.settings.setValue("preview/muted", False)
            self.preview.set_muted(False)
            self._refresh_mute_tip()
            self._refresh_icons()

    # ---------------- proyecto ----------------
    def set_project(self, project: ClipProject, editor_state: dict | None = None):
        self.project = project
        self._cinematic.reset_generation()
        self.style = Style(
            bloom=self.cb_bloom.isChecked(),
            title=project.title,
            artist=project.artist,
            font_family=self.cmb_font.currentData() or "",
            credit=self.cb_credit.isChecked(),
        )
        self._apply_wm()
        self._bgctl.refresh()
        if project.background_warning:
            self.notify(project.background_warning, "info")
            project.background_warning = ""
        for ed, text in (
            (self.ed_title, project.title),
            (self.ed_artist, project.artist),
            (self.ed_album, project.album or (project.track.album if project.track else "")),
        ):
            with blocked_signals(ed):
                ed.setText(text)
        self._refresh_source_tag()
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
        self._loops.restore(editor_state or {})
        self._presets.applied = str((editor_state or {}).get("preset") or "")
        self._hooks.reset()
        self._presets.refresh_list()
        self.subtitle.setText(self._title_text())
        self.stack.setCurrentIndex(1)
        for w in (self.btn_export, self.btn_menu, self.btn_format, *self.mode_chips.values()):
            w.setEnabled(True)
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
        self.refresh_loop()
        self.project_changed.emit(self._title_text())

    def editor_state(self) -> dict:
        return {
            "first": self.first,
            "last": self.last,
            "style": asdict(self.style),
            "params": asdict(self.params),
            "audio_source": self.cmb_source.currentData(),
            "fx": {"fx_intensity": self.sl_fx.value() / 100, "adapt_bg": self.cb_fxbg.isChecked()},
            "preset": self._presets.applied,
            "loop": self._loops.requested,
            **({"loop_end_ms": self._loops.adjusted_end} if self._loops.adjusted_end is not None else {}),
        }

    def _restore_editor_controls(self, state: dict, n_lines: int):
        if "first" in state and "last" in state and n_lines > 0:
            f = max(0, min(n_lines - 1, int(state["first"])))
            l = max(f, min(n_lines - 1, int(state["last"])))
            self.first, self.last, self._anchor = f, l, f
        st = state.get("style") or {}
        pa = state.get("params") or {}
        for w, val in (
            (self.cb_bloom, st.get("bloom")),
            (self.cb_credit, st.get("credit")),
            (self.cb_vib, pa.get("vib")),
            (self.cb_emph, pa.get("emph")),
            (self.cb_reduce, pa.get("reduce")),
        ):
            if isinstance(val, bool):
                with blocked_signals(w):
                    w.setChecked(val)
        if "font_family" in st:
            fi = self.cmb_font.findData(st["font_family"])
            if fi >= 0:
                with blocked_signals(self.cmb_font):
                    self.cmb_font.setCurrentIndex(fi)
        if "motion" in pa:
            mi = self.cmb_motion.findData(pa["motion"])
            if mi >= 0:
                with blocked_signals(self.cmb_motion):
                    self.cmb_motion.setCurrentIndex(mi)
        for sl, key, mul in ((self.sl_blur, "blurm", 10), (self.sl_wob, "wob", 100), (self.sl_amt, "motion_amt", 100)):
            if key in pa:
                with blocked_signals(sl):
                    sl.setValue(round(float(pa[key]) * mul))
        for srow in self._style_sliders:
            srow.update_readout()
        blur, wob = self.sl_blur.value() / 10, self.sl_wob.value() / 100
        self.params.vib, self.params.emph = self.cb_vib.isChecked(), self.cb_emph.isChecked()
        self.params.blurm, self.params.wob = blur, wob
        self.params.motion, self.params.motion_amt = self.cmb_motion.currentData(), self.sl_amt.value() / 100
        self.params.reduce = self.cb_reduce.isChecked()
        self.style.bloom = self.cb_bloom.isChecked()
        self.style.credit = self.cb_credit.isChecked()
        self.style.font_family = self.cmb_font.currentData() or ""
        if "fx" in state:  # proyecto guardado con Corrientes: también recuerda sus campos nuevos (y la marca de agua)
            wanted = preset_schema.from_state(state)
            if self.cmb_font.findData(wanted["tipografia"].get("font_family", "")) < 0:
                wanted["tipografia"].pop("font_family", None)  # esa fuente ya no está instalada: se queda la elegida arriba
            style, params, bg, fx = preset_schema.apply(wanted, *self.look())
            self.set_look(style, params, bg, fx, rebuild=False)

    def look(self):
        """(style, params, fondo, fx) actuales, copiados: lo que una Corriente captura, compara y modifica."""
        bg = copy.copy(self.project.background) if self.project else BackgroundConfig()
        return replace(self.style), replace(self.params), bg, preset_schema.FxSettings(self.sl_fx.value() / 100, self.cb_fxbg.isChecked())

    def set_look(self, style: Style, params: Params, bg: BackgroundConfig, fx, rebuild: bool = True):
        """Vuelca un look en los objetos y controles del editor. Los objetos se actualizan en sitio: la preview los comparte."""
        for src, dst in ((style, self.style), (params, self.params)):
            for f in fields(src):
                setattr(dst, f.name, getattr(src, f.name))
        for w, val in ((self.cb_bloom, style.bloom), (self.cb_vib, params.vib), (self.cb_emph, params.emph),
                       (self.cb_fxbg, fx.adapt_bg), (self.cb_wm_glyph, style.wm_glyph)):
            with blocked_signals(w):
                w.setChecked(bool(val))
        for cmb, data in ((self.cmb_motion, params.motion), (self.cmb_font, style.font_family or ""),
                          (self.cmb_wm_side, style.wm_side)):
            i = cmb.findData(data)
            if i >= 0:
                with blocked_signals(cmb):
                    cmb.setCurrentIndex(i)
        for sl, val in ((self.sl_blur, params.blurm * 10), (self.sl_wob, params.wob * 100), (self.sl_amt, params.motion_amt * 100),
                        (self.sl_fx, fx.fx_intensity * 100)):
            with blocked_signals(sl):
                sl.setValue(round(val))
        for srow in self._style_sliders:
            srow.update_readout()
        self.lb_fx.setText(f"{self.sl_fx.value()} %")
        with blocked_signals(self.ed_wm):
            self.ed_wm.setText(style.wm_text)
        if self.project:
            self.project.background = bg.clamped()
        self._bgctl.refresh()
        if rebuild:
            self._wordfx_sync(rebuild=False)
            self.preview.rebuild()

    def select_range(self, first: int, last: int):
        """Elige el rango de versos (p. ej. desde una sugerencia de Anzuelo) y lo muestra en la lista."""
        n = len(self.project.lyrics.lines) if self.project else 0
        if not n:
            return
        old = self.first, self.last
        self.first = max(0, min(n - 1, first))
        self.last = max(self.first, min(n - 1, last))
        self._anchor = self.first
        self._loops.selection_changed(old)
        self.list.scrollToItem(self.list.item(self.first), QListWidget.PositionAtTop)
        self._range_changed()

    def set_hover_range(self, rng: tuple[int, int] | None):
        self.hook_hover = rng
        self.list.viewport().update()

    def _load_saved_audio(self, project: ClipProject, audio_path: Path):
        def done(samples):
            if self.project is project:
                project.audio_samples = samples
                self.preview.set_audio_samples(samples, project.audio_offset_ms)
                self._sync_auto_opening_trim()
                self._refresh_audio_tab()
                self._refresh_cinematic_tab()
                self._wordfx_sync()

        pkg_attr("run_async", run_async)(lambda: local.decode(audio_path), done, lambda _e: None)

    def save_project_dialog(self):
        if not self.project:
            return
        default_name = (
            self.project.project_path.name
            if self.project.project_path
            else f"{self.project.title or lang.t('editor.defaultProjectName')}.marea.json"
        )
        start_dir = str((self.project.project_path.parent if self.project.project_path else self._output_dir()) / default_name)
        path, _ = QFileDialog.getSaveFileName(self, lang.t("editor.saveProject"), start_dir, project_filter())
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".marea.json"
        try:
            saved = save_project(self.project, path, editor_state=self.editor_state())
        except OSError as e:
            self.notify(lang.t("editor.saveFailed", error=e), "error")
            return
        self.register_in_library()
        self.notify(lang.t("editor.saved", name=saved.name), "ok")

    def register_in_library(self):
        """Anota el proyecto actual (ya guardado o abierto desde archivo) en la Biblioteca."""
        if self.project and self.project.project_path:
            library.register(self.project, self.project.project_path)
            self.library_changed.emit()

    def open_project_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, lang.t("editor.openProject"), "", project_filter())
        if path:
            self.open_project_path(path)

    def open_project_path(self, path: str | Path) -> bool:
        try:
            project, state = load_project_file(path)
        except (ProjectError, OSError) as e:
            self.notify(str(e), "error")
            return False
        self.set_project(project, editor_state=state)
        self.register_in_library()
        return True

    def _title_text(self) -> str:
        p = self.project
        return f"{p.artist} – {p.title}" if p.artist else p.title

    @property
    def mode(self) -> str:
        return next((k for k, a in self.mode_actions.items() if a.isChecked()), "lyrics")

    # ---------------- selección de versos ----------------
    def _on_verse_clicked(self, item):
        old = self.first, self.last
        row = self.list.row(item)
        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
            self.first, self.last = sorted((self._anchor, row))
        else:
            self._anchor = self.first = self.last = row
        self._loops.selection_changed(old)
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
        lang.bind(self.range_label, "editor.range", first=self.first + 1, last=self.last + 1, span=fmt_ms(span))
        self.refresh_loop(seek=seek)
        self._export_bar.update_summary()
        self._audio.refresh_wave()

    def _clip_range(self) -> tuple[float, float]:
        if spec := self._loops.current():
            return spec.start_ms, spec.end_ms
        return clip_range(self.project.lyrics, self.first, self.last, float(self.settings.value("export/outro_ms", TAIL_MS)))

    def refresh_loop(self, *, seek: bool = False):
        spec = self._loops.current()
        self._loops.refresh(spec)
        if self.project is not None and self.preview.scene is not None:
            target = self.settings.value("export/target", "mp4")
            sound = spec is not None and self.mode == "song" and self.cmb_source.currentData() == "local"
            watermark = self.settings.value("export/alpha_watermark", True, type=bool)
            self.preview.configure_range(self._clip_range(), spec, sound, target, watermark, seek=seek)

    # ---------------- preview ----------------
    def _on_position(self, ms: float, total: float):
        text = f"{fmt_ms(ms)} / {fmt_ms(total)}"
        if text != self._last_time_text:
            self._last_time_text = text
            self.time.setText(text)
        if not self._scrubbing:
            val = round(min(1.0, ms / total) * SCRUB_MAX)
            if val != self.scrub.value():
                with blocked_signals(self.scrub):
                    self.scrub.setValue(val)
        if self.project:
            self.wave.set_playhead(self._clip_range()[0] + ms)

    def _on_wave_seek(self, song_ms: float):
        if not self.project:
            return
        if self.wave.dragging:  # throttled like the scrub bar: one cheap seek per 16 ms, the full one on release
            self._wave_seek_ms = song_ms
            if not self._wave_action.is_active():
                self._wave_action.schedule()
        else:
            self.preview.seek(song_ms - self._clip_range()[0])

    def _flush_wave_seek(self):
        if self.project and self._wave_seek_ms is not None:
            self.preview.seek(self._wave_seek_ms - self._clip_range()[0], fast=self.wave.dragging)
        if not self.wave.dragging:
            self._wave_seek_ms = None

    def _on_scrub(self, v: int):
        if self._scrubbing or self.scrub.isSliderDown():
            if not self._scrub_action.is_active():
                self._scrub_action.schedule()
        else:
            self.preview.seek(v / SCRUB_MAX * self.preview.duration)

    def _flush_scrub(self):
        self.preview.seek(self.scrub.value() / SCRUB_MAX * self.preview.duration, fast=self.scrub.isSliderDown())

    def _on_scrub_released(self):
        self._scrubbing = False
        self._scrub_action.flush()

    def _on_playing(self, playing: bool):
        self._refresh_icons()

    def showEvent(self, e):
        super().showEvent(e)
        self._wordfx_sync()

    # ---------------- delegados a controladores ----------------
    def _wordfx_sync(self, rebuild: bool = True):
        self._wordfx.sync(rebuild=rebuild)

    def _open_fx_profile(self):
        self._wordfx.open_profile_dialog()

    def _open_fx_gallery(self):
        self._wordfx.open_gallery()

    def _credit_toggled(self, on: bool):
        self.style.credit = on
        self.preview.rebuild()

    def _style_changed(self):
        blur, wob = self.sl_blur.value() / 10, self.sl_wob.value() / 100
        self.params.vib, self.params.emph = self.cb_vib.isChecked(), self.cb_emph.isChecked()
        self.params.blurm, self.params.wob = blur, wob
        self.params.motion, self.params.motion_amt = self.cmb_motion.currentData(), self.sl_amt.value() / 100
        self.params.reduce = self.cb_reduce.isChecked()
        self.style.bloom = self.cb_bloom.isChecked()
        self.preview.restyle()

    def _font_changed(self):
        self.style.font_family = self.cmb_font.currentData() or ""
        self.preview.rebuild()

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

    def _meta_changed(self):
        if not self.project:
            return
        self.project.title = self.ed_title.text()
        self.project.artist = self.ed_artist.text()
        self.project.album = self.ed_album.text()
        self.style.title, self.style.artist = self.project.title, self.project.artist
        self.subtitle.setText(self._title_text())
        self.project_changed.emit(self._title_text())
        self.preview.refresh()

    def _source_tooltip(self) -> str:
        if not self.project:
            return ""
        ly = self.project.lyrics
        tag = ly.resolved_source_tag
        parts = [lang.t("sourceTag.tipHeader", source=source_label(tag))]
        if ly.credit:
            parts.append(ly.credit)
        parts.append(lang.t("widgets.badgeSyllable" if ly.kind == "syllable" else "widgets.badgeLine"))
        if ly.manual_override:
            parts.append(lang.t("sourceTag.protectedTip"))
        return " · ".join(parts)

    def _refresh_source_tag(self):
        if not self.project:
            return
        self.source_tag.set_tag(self.project.lyrics.resolved_source_tag)
        lang.bind_fn(self.source_tag, "tooltip", lambda: self.source_tag.setToolTip(self._source_tooltip()))

    def _confirm_manual_overwrite(self) -> bool:
        if not (self.project and self.project.lyrics.manual_override):
            return True
        ans = QMessageBox.question(
            self,
            lang.t("sourceTag.confirmTitle"),
            lang.t("sourceTag.confirmBody"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return ans == QMessageBox.Yes

    def _open_source_menu(self):
        if not self.project:
            return
        p = self.project
        ly = p.lyrics
        m = QMenu(self)
        info = m.addAction(f"{source_label(ly.resolved_source_tag)} · {lang.t('widgets.badgeSyllable' if ly.kind == 'syllable' else 'widgets.badgeLine')}")
        info.setEnabled(False)
        if ly.credit:
            cred = m.addAction(ly.credit)
            cred.setEnabled(False)
        m.addSeparator()
        if self.services:
            act_retry = m.addAction(lang.t("sourceTag.retryAuto"), lambda: self._retry_lyrics("auto"))
            act_lrc = m.addAction(lang.t("sourceTag.retryLrclib"), lambda: self._retry_lyrics(SOURCE_LRCLIB))
            act_spicy = m.addAction(lang.t("sourceTag.retrySpicy"), lambda: self._retry_lyrics(SOURCE_SPICY))
            has_sp = bool(p.track and p.track.id_for("spotify"))
            act_spicy.setEnabled(has_sp)
            m.addAction(lang.t("sourceTag.pickCandidate"), self._pick_lyrics_candidate)
            m.addSeparator()
        m.addAction(lang.t("exportBar.replaceLyrics"), self._replace_ttml)
        m.exec(self.source_tag.mapToGlobal(self.source_tag.rect().bottomLeft()))

    def _current_query_track(self) -> Track:
        p = self.project
        if p.track is not None:
            return Track(
                id=p.track.id,
                title=p.title or p.track.title,
                artists=[p.artist] if p.artist else list(p.track.artists),
                album=p.album or p.track.album,
                duration_ms=p.track.duration_ms,
                cover_url=p.track.cover_url,
                uri=p.track.uri,
                provider=p.track.provider,
                provider_ids=dict(p.track.provider_ids),
            )
        dur = round(len(p.audio_samples) / local.RATE * 1000) if p.audio_samples is not None else 0
        return Track("", p.title, [p.artist] if p.artist else [], p.album, dur, None, provider="manual")

    def _apply_new_lyrics(self, lyrics):
        p = self.project
        if not p:
            return
        # Preserve audio file, decoded samples, and manual audio_offset_ms.
        self.set_project(replace(p, lyrics=lyrics, source=None))

    def _retry_lyrics(self, provider: str = "auto"):
        if not (self.project and self.services) or not self._confirm_manual_overwrite():
            return
        proj = self.project
        trk = self._current_query_track()
        self.notify(lang.t("sourceTag.retrying"))

        def done(ly):
            if self.project is not proj:
                return
            if not ly:
                self.notify(lang.t("services.noLyrics"), "error")
                return
            self._apply_new_lyrics(ly)
            self.notify(lang.t("sourceTag.updated", source=source_label(ly.resolved_source_tag)), "ok")

        run_async(
            lambda: self.services.fetch_lyrics_for_track(trk, provider=provider),
            done,
            lambda e: self.notify(str(e) if isinstance(e, ProviderError) else lang.t("search.failed"), "error"),
        )

    def _pick_lyrics_candidate(self):
        if not (self.project and self.services) or not self._confirm_manual_overwrite():
            return
        proj = self.project
        trk = self._current_query_track()
        self.notify(lang.t("sourceTag.retrying"))

        def done(cands):
            if self.project is not proj:
                return
            if not cands:
                self.notify(lang.t("services.noLyrics"), "error")
                return
            items = [
                CandidateItem(
                    title=str(raw.get("trackName") or proj.title),
                    subtitle=f"{raw.get('artistName') or proj.artist} · {raw.get('albumName') or ''}".strip(" ·"),
                    badge=lang.t("widgets.badgeSyllable" if ly.kind == "syllable" else "widgets.badgeLine"),
                    badge_tag=SOURCE_LRCLIB,
                    duration_ms=round((raw.get("duration") or 0) * 1000) or None,
                    payload=ly,
                )
                for raw, ly in cands
            ]
            chosen = CandidateDialog.pick(lang.t("dialogs.pickLyricsTitle"), lang.t("dialogs.pickLyricsBody"), items, self)
            if chosen is not None and self.project is proj:
                self._apply_new_lyrics(chosen)

        run_async(
            lambda: self.services.search_lyrics_candidates(trk.title, trk.artist, trk.album, trk.duration_ms or None),
            done,
            lambda e: self.notify(str(e) if isinstance(e, ProviderError) else lang.t("search.failed"), "error"),
        )

    def _match_metadata_dialog(self):
        if not (self.project and self.services):
            return
        proj = self.project
        default_q = " ".join(p for p in (proj.artist, proj.title) if p).strip()
        query, ok = QInputDialog.getText(
            self,
            lang.t("dialogs.matchMetaTitle"),
            lang.t("dialogs.matchMetaPrompt"),
            text=default_q,
        )
        if not ok or not query.strip():
            return
        self.notify(lang.t("sourceTag.retrying"))

        def done(res):
            ranked, _ambig = res
            if self.project is not proj:
                return
            if not ranked:
                self.notify(lang.t("search.noneTitle"), "error")
                return
            items = [
                CandidateItem(
                    title=r.track.title,
                    subtitle=" · ".join(p for p in (r.track.artist, r.track.album, f"{round(r.match.score * 100)}%") if p),
                    badge="Deezer" if r.track.provider == "deezer" else "Spotify",
                    badge_tag=SOURCE_LRCLIB if r.track.provider == "deezer" else SOURCE_SPICY,
                    duration_ms=r.track.duration_ms or None,
                    payload=r.track,
                )
                for r in ranked
            ]
            chosen: Track | None = ranked[0].track if (len(ranked) == 1 and not _ambig) else CandidateDialog.pick(
                lang.t("dialogs.matchMetaTitle"), lang.t("dialogs.pickMetaBody"), items, self
            )
            if chosen is None:
                return

            def apply_cover(cover_bytes: bytes | None):
                if self.project is not proj:
                    return
                new_cover = decode_cover(cover_bytes) if cover_bytes else None
                proj.track = chosen
                proj.title = chosen.title
                proj.artist = chosen.artist
                proj.album = chosen.album
                if new_cover is not None:
                    proj.cover = new_cover
                for ed, text in ((self.ed_title, proj.title), (self.ed_artist, proj.artist), (self.ed_album, proj.album)):
                    with blocked_signals(ed):
                        ed.setText(text)
                self.style.title, self.style.artist = proj.title, proj.artist
                self.subtitle.setText(self._title_text())
                self.project_changed.emit(self._title_text())
                self._wordfx_sync(rebuild=False)
                self.preview.rebuild()
                self._audio.refresh()
                self.notify(lang.t("dialogs.metaApplied", title=chosen.title), "ok")

            if chosen.cover_url:
                run_async(lambda: self.services.cover_bytes(chosen.cover_url), apply_cover, lambda _e: apply_cover(None))
            else:
                apply_cover(None)

        dur = round(len(proj.audio_samples) / local.RATE * 1000) if proj.audio_samples is not None else None
        run_async(
            lambda: self.services.resolve_metadata(query.strip(), title=proj.title, artist=proj.artist, duration_ms=dur),
            done,
            lambda e: self.notify(str(e) if isinstance(e, ProviderError) else lang.t("search.failed"), "error"),
        )

    def _choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, lang.t("editor.chooseCover"), "", lang.t("editor.imageFilter"))
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
        path, _ = QFileDialog.getOpenFileName(self, lang.t("editor.replaceLyrics"), "", lang.t("editor.ttmlFilter"))
        if not path:
            return
        try:
            lyrics = project_from_ttml(path).lyrics
        except (ValueError, OSError) as e:
            self.notify(lang.t("editor.lyricsOpenFailed", error=e), "error")
            return
        self.set_project(replace(self.project, lyrics=lyrics, source=Path(path), audio_offset_ms=0.0))

    def _sync_auto_opening_trim(self):
        self._cinematic.sync_auto_opening_trim()

    def _refresh_cinematic_tab(self, keep_id: str | None = None):
        self._cinematic.refresh(keep_id=keep_id)

    def import_video(self, path: str | Path):
        self._cinematic.import_video(path)

    def _suggest_cinematic(self):
        self._cinematic.suggest()

    def _add_cinematic_interval(self):
        self._cinematic.add_interval()

    def _remove_cinematic_interval(self):
        self._cinematic.remove_interval()

    def _spotify_track(self):
        return self._audio.spotify_track()

    def _spotify_ready(self) -> bool:
        return self._audio.spotify_ready()

    def _pick_default_source(self):
        self._audio.pick_default_source()

    def _refresh_audio_tab(self, *_):
        self._audio.refresh()
        if self.project:
            self.refresh_loop()

    def _refresh_wave(self):
        self._audio.refresh_wave()

    def _auto_offset(self):
        self._audio.auto_offset()

    def _auto_input(self):
        return self._audio._auto_input()

    def _choose_audio(self):
        self._audio.choose_audio()

    def _update_summary(self):
        self._export_bar.update_summary()
        if self.project:
            self.refresh_loop()

    # ---------------- export ----------------
    def _output_dir(self) -> Path:
        return Path(self.settings.value("output_dir", str(default_output_dir())))

    def _song_duration_ms(self) -> float:
        p = self.project
        if not p:
            return float(TAIL_MS)
        source = self.cmb_source.currentData()
        if source == "local" and p.audio_samples is not None:
            return max(1000.0, len(p.audio_samples) / local.RATE * 1000.0 - p.audio_offset_ms)
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

    def _resolve_encoder(self) -> tuple[str, str]:
        """(backend, códec) del export según Ajustes y el menú; avisa una vez si la GPU elegida no puede con el códec."""
        want_backend = self.settings.value("export/encoder", "auto")
        want_codec = self.settings.value("export/codec", "h264")
        backend, codec = ffmpeg.resolve_encoder(want_backend, want_codec)
        if (want_backend not in ("auto", backend)) or (want_codec != codec):
            key = (want_backend, want_codec, backend, codec)
            if key not in self._encoder_warned:
                self._encoder_warned.add(key)
                self.notify(lang.t("editor.encoderFallback", backend=ffmpeg.BACKENDS.get(want_backend) or lang.t("editor.thisMachine"),
                                   codec=ffmpeg.CODECS.get(want_codec, want_codec), usedBackend=ffmpeg.BACKENDS[backend],
                                   usedCodec=ffmpeg.CODECS[codec]), "info")
        return backend, codec

    def build_job(self, start_ms: float | None = None, end_ms: float | None = None, full: bool = False,
                  first: int | None = None, last: int | None = None, look_preset=None) -> ExportJob:
        """`first`/`last`: versos del nombre del archivo (por omisión, la selección actual). `look_preset`: PresetInfo de una Corriente
        que se aplica completa sobre copias del look actual (variantes A/B); el nombre de la Corriente va en el archivo."""
        preset = PRESETS.get(self.settings.value("export/preset", "tiktok"), PRESETS["tiktok"])
        if start_ms is None:
            start_ms, end_ms = self._clip_range()
        target = self.settings.value("export/target", "mp4")
        target = target if target in TARGETS else "mp4"
        encoder, codec = self._resolve_encoder() if target == "mp4" else ("cpu", "h264")  # los destinos con alfa traen su códec
        style, params, background, fx = self.look()
        background = copy.deepcopy(background)
        plan = self._fx_plan
        if look_preset is not None:
            style, params, background, fx = preset_schema.apply(look_preset.preset, style, params, background, fx)
            plan = self._wordfx.plan_for(fx.fx_intensity, fx.adapt_bg)
        if target in ALPHA_TARGETS:
            background.source = "transparent"
            if not self.settings.value("export/alpha_watermark", True, type=bool):
                style.wm_text = ""
        elif background.transparent:
            background.source = "cover"
            self.notify(lang.t("editor.transparentIgnored"), "info")
        first, last = (self.first if first is None else first), (self.last if last is None else last)
        loop = self._loops.spec(start_ms, end_ms, first, last, full=full)
        name = clip_name(self.project.artist, self.project.title, *((None, None) if full else (first, last)), target=target,
                         tag=look_preset.name if look_preset is not None else "")
        return ExportJob(
            self.project.lyrics,
            self.project.cover_png(),
            self._output_dir() / name,
            start_ms,
            end_ms,
            size=(preset[1], preset[2]),
            fps=60,
            encoder=encoder,
            codec=codec,
            marker=self.settings.value("export/marker", "none"),
            params=params,
            style=style,
            background=background,
            target=target,
            cinematic=copy.copy(self.project.cinematic) if self.project.cinematic is None else copy.deepcopy(self.project.cinematic),
            wordfx=plan,
            loop=loop,
        )

    def enqueue_range(self, first: int, last: int, start_ms: float, end_ms: float, look_preset=None) -> QueueItem | None:
        """Añade a la cola un rango explícito con los ajustes actuales (o con una Corriente): lo que usa Anzuelo.
        «Canción completa» cuenta como «con canción»: aquí el rango lo manda la sugerencia."""
        if not self.project:
            return None
        try:
            ffmpeg.find_ffmpeg()
            job = self.build_job(start_ms, end_ms, False, first, last, look_preset)
            audio = None
            if self.mode != "lyrics":
                audio = self._audio_spec(job.start_ms, job.end_ms)
                if audio is None:
                    return None
                if job.loop is None:
                    audio.fade_out_ms = float(self.settings.value("export/outro_ms", TAIL_MS))
            job.out_path.parent.mkdir(parents=True, exist_ok=True)
        except FileNotFoundError as e:
            self.notify(str(e), "error")
            return None
        except OSError as e:
            self.notify(lang.t("editor.outDirFailed", reason=e.strerror), "error")
            return None
        item = QueueItem(self.project.title, self.project.artist, job.cover_png, job, audio)
        self.queue.add(item)
        self.enqueued.emit(item)
        return item

    def _audio_spec(self, start_ms: float, end_ms: float) -> AudioSpec | None:
        return self._audio.audio_spec(start_ms, end_ms)

    def export(self):
        """Añade el clip a la cola de exportaciones (se procesa de uno en uno)."""
        if not self.project:
            return
        try:
            ffmpeg.find_ffmpeg()
            mode = self.mode
            full = mode == "full"
            start, end = (0.0, self._full_end_ms()) if full else self._clip_range()
            job = self.build_job(start, end, full)
            audio = None
            if mode != "lyrics":
                audio = self._audio_spec(job.start_ms, job.end_ms)
                if audio is None:
                    return
                if not full and job.loop is None:
                    audio.fade_out_ms = float(self.settings.value("export/outro_ms", TAIL_MS))
            job.out_path.parent.mkdir(parents=True, exist_ok=True)
        except FileNotFoundError as e:
            self.notify(str(e), "error")
            return
        except OSError as e:
            self.notify(lang.t("editor.outDirFailed", reason=e.strerror), "error")
            return
        item = QueueItem(self.project.title, self.project.artist, job.cover_png, job, audio)
        self.preview.set_playing(False)
        self.queue.add(item)
        self.enqueued.emit(item)

    def shutdown(self):
        self.preview.shutdown()
