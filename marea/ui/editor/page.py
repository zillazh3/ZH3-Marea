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
    QLabel,
    QListWidget,
    QListWidgetItem,
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
from marea.paths import default_output_dir
from marea.presets import schema as preset_schema
from marea.project import ClipProject, ProjectError, load_cover, load_project_file, project_from_ttml, save_project
from marea.render.scene import Style
from marea.ui import theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.editor.audio_tab import AudioController
from marea.ui.editor.cinematic_tab import CinematicController
from marea.ui.editor.export_bar import ExportBarController
from marea.ui.editor.hooks_panel import HooksController
from marea.ui.editor.meta_tabs import build_background_tab, build_watermark_tab
from marea.ui.editor.presets_panel import PresetsController
from marea.ui.editor.style_tab import build_style_tab
from marea.ui.editor.wordfx_controller import WordFXController
from marea.ui.preview import PreviewWidget
from marea.ui.widgets import CoalescedAction, EmptyState, blocked_signals, fmt_ms

PANEL_W = 448  # las 6 pestañas (con Anzuelo) caben sin botones de desplazamiento
DEFAULT_VERSES = 6
PROJECT_FILTER = "Proyecto ZH3 Marea (*.marea.json);;Todos los archivos (*.*)"


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
        QVBoxLayout(card).addWidget(
            EmptyState(
                "sliders-horizontal",
                "Elige una canción para editar",
                "Abre un .ttml o la canción demo desde Inicio, o busca una canción de Spotify; aquí eliges los versos y el estilo.",
            )
        )
        self.stack.addWidget(card)
        self.stack.addWidget(self._build_body(prefer_gpu))
        root.addWidget(self.stack, 1)
        root.addLayout(self._export_bar.build_bar())
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
        self.btn_play = QPushButton()
        self.btn_play.setObjectName("IconBtn")
        self.btn_play.setFixedSize(40, 40)
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.setToolTip("Reproducir / pausar")
        self.btn_play.clicked.connect(lambda: self.preview.set_playing(not self.preview.playing))
        self.scrub = QSlider(Qt.Horizontal)
        self.scrub.setRange(0, 1000)
        self._scrub_action = CoalescedAction(16, self._flush_scrub, self)
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
        self.credit = QLabel()
        self.credit.setObjectName("Caption")
        self.credit.setTextFormat(Qt.PlainText)
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
        self.tabs.addTab(build_style_tab(self, system_reduces_motion), "Estilo")
        self.tabs.addTab(build_background_tab(self), "Fondo")
        self.tabs.addTab(self._cinematic.build_tab(), "Cinemático")
        self.tabs.addTab(self._audio.build_tab(), "Audio")
        self.tabs.addTab(build_watermark_tab(self), "Marca de agua")
        self.tabs.addTab(self._hooks.build_tab(), "Anzuelo")
        tl.addWidget(self.tabs)
        right.addWidget(tabs_card, 2)
        container = QWidget()
        container.setFixedWidth(PANEL_W)
        container.setLayout(right)
        right.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(container)
        return body

    def _open_sync(self):
        from marea.ui.lab import open_review  # lazy import when experimental/align is active
        open_review(self)

    def _refresh_icons(self):
        t = theme.current
        self.btn_play.setIcon(theme.icon("pause" if self.preview.playing else "play", t.text, 18))
        self.btn_menu.setIcon(theme.icon("ellipsis", t.text_muted, 16))
        self.btn_format.setIcon(theme.icon("chevron-down", t.text_muted, 16))
        self.btn_format.setLayoutDirection(Qt.RightToLeft)  # chevron after the label

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
        for ed, text in ((self.ed_title, project.title), (self.ed_artist, project.artist)):
            with blocked_signals(ed):
                ed.setText(text)
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
        self.first = max(0, min(n - 1, first))
        self.last = max(self.first, min(n - 1, last))
        self._anchor = self.first
        self.list.scrollToItem(self.list.item(self.first), QListWidget.PositionAtTop)
        self._range_changed()

    def set_hover_range(self, rng: tuple[int, int] | None):
        self.hook_hover = rng
        self.list.viewport().update()

    def _load_saved_audio(self, project: ClipProject, audio_path: Path):
        def done(samples):
            if self.project is project:
                project.audio_samples = samples
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
            else f"{self.project.title or 'proyecto'}.marea.json"
        )
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
        self.register_in_library()
        self.notify(f"Proyecto guardado: {saved.name}", "ok")

    def register_in_library(self):
        """Anota el proyecto actual (ya guardado o abierto desde archivo) en la Biblioteca."""
        if self.project and self.project.project_path:
            library.register(self.project, self.project.project_path)
            self.library_changed.emit()

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
        self.range_label.setText(f"Versos {self.first + 1}–{self.last + 1} · {fmt_ms(span, tenths=True)}")
        if seek:
            self.preview.set_range(self._clip_range())
        self._export_bar.update_summary()
        self._audio.refresh_wave()

    def _clip_range(self) -> tuple[float, float]:
        return clip_range(self.project.lyrics, self.first, self.last, float(self.settings.value("export/outro_ms", TAIL_MS)))

    # ---------------- preview ----------------
    def _on_position(self, ms: float, total: float):
        text = f"{fmt_ms(ms, tenths=True)} / {fmt_ms(total, tenths=True)}"
        if text != self._last_time_text:
            self._last_time_text = text
            self.time.setText(text)
        if not self._scrubbing:
            val = round(min(1.0, ms / total) * 1000)
            if val != self.scrub.value():
                with blocked_signals(self.scrub):
                    self.scrub.setValue(val)

    def _on_scrub(self, v: int):
        if self._scrubbing or self.scrub.isSliderDown():
            if not self._scrub_action.is_active():
                self._scrub_action.schedule()
        else:
            self.preview.seek(v / 1000 * self.preview.duration)

    def _flush_scrub(self):
        self.preview.seek(self.scrub.value() / 1000 * self.preview.duration)

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
        path, _ = QFileDialog.getOpenFileName(self, "Reemplazar letra", "", "Letras TTML (*.ttml *.xml)")
        if not path:
            return
        try:
            lyrics = project_from_ttml(path).lyrics
        except (ValueError, OSError) as e:
            self.notify(f"No pude abrir esa letra: {e} Usa un .ttml de Apple Music con tiempos por sílaba.", "error")
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

    def build_job(self, start_ms: float | None = None, end_ms: float | None = None, full: bool = False,
                  first: int | None = None, last: int | None = None, look_preset=None) -> ExportJob:
        """`first`/`last`: versos del nombre del archivo (por omisión, la selección actual). `look_preset`: PresetInfo de una Corriente
        que se aplica completa sobre copias del look actual (variantes A/B); el nombre de la Corriente va en el archivo."""
        preset = PRESETS.get(self.settings.value("export/preset", "tiktok"), PRESETS["tiktok"])
        if start_ms is None:
            start_ms, end_ms = self._clip_range()
        target = self.settings.value("export/target", "mp4")
        target = target if target in TARGETS else "mp4"
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
            self.notify("El fondo transparente solo aplica a WebM, ProRes o PNG: este MP4 salió con la portada.", "info")
        first, last = (self.first if first is None else first), (self.last if last is None else last)
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
            encoder=self.settings.value("export/encoder") or ffmpeg.default_encoder(),
            marker=self.settings.value("export/marker", "none"),
            params=params,
            style=style,
            background=background,
            target=target,
            cinematic=copy.copy(self.project.cinematic) if self.project.cinematic is None else copy.deepcopy(self.project.cinematic),
            wordfx=plan,
        )

    def enqueue_range(self, first: int, last: int, start_ms: float, end_ms: float, look_preset=None) -> QueueItem | None:
        """Añade a la cola un rango explícito con los ajustes actuales (o con una Corriente): lo que usa Anzuelo.
        «Canción completa» cuenta como «con canción»: aquí el rango lo manda la sugerencia."""
        if not self.project:
            return None
        try:
            ffmpeg.find_ffmpeg()
            audio = None
            if self.mode != "lyrics":
                audio = self._audio_spec(start_ms, end_ms)
                if audio is None:
                    return None
                audio.fade_out_ms = float(self.settings.value("export/outro_ms", TAIL_MS))
            job = self.build_job(start_ms, end_ms, False, first, last, look_preset)
            job.out_path.parent.mkdir(parents=True, exist_ok=True)
        except FileNotFoundError as e:
            self.notify(str(e), "error")
            return None
        except OSError as e:
            self.notify(f"No pude crear la carpeta de salida ({e.strerror}). Elige otra en el menú de exportación.", "error")
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
        item = QueueItem(self.project.title, self.project.artist, job.cover_png, job, audio)
        self.preview.set_playing(False)
        self.queue.add(item)
        self.enqueued.emit(item)

    def shutdown(self):
        self.preview.shutdown()
