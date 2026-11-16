"""Fondo del clip en el Editor: portada, video propio o transparente (marea.model.background)."""
import os
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QLabel, QWidget

from marea.async_util import run_async
from marea.export.ffmpeg import ALPHA_TARGETS
from marea.model.background import FITS, LOOPS, BackgroundConfig
from marea.ui import theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.widgets import CoalescedAction, SliderRow, blocked_signals, make_button

SOURCE_LABELS = {"cover": "Portada (difuminada)", "video": "Video propio", "transparent": "Transparente (para WebM / ProRes / PNG)"}
LOOP_LABELS = {"repeat": "Repetir", "pingpong": "Ida y vuelta", "hold": "Congelar último cuadro"}
FIT_LABELS = {"cover": "Cubrir", "contain": "Contener"}
VIDEO_FILTER = "Videos (*.mp4 *.mov *.webm *.mkv);;Todos los archivos (*.*)"
assert set(LOOP_LABELS) == set(LOOPS) and set(FIT_LABELS) == set(FITS)


def _spin(tip: str) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setDecimals(2)
    s.setSingleStep(0.1)
    s.setSuffix(" s")
    s.setRange(-3600.0, 3600.0)
    s.setToolTip(tip)
    return s


class BackgroundController:
    """Construye los controles de fondo dentro del formulario de la pestaña «Fondo» y los sincroniza con el proyecto."""

    def __init__(self, editor):
        self.ed = editor
        self._busy = False

    def build(self, form: QFormLayout):
        ed = self.ed
        ed.cmb_bgsrc = QComboBox()
        for key, label in SOURCE_LABELS.items():
            ed.cmb_bgsrc.addItem(label, key)
        ed.cmb_bgsrc.setToolTip("Portada: el fondo animado de siempre. Video propio: tu video detrás de la letra. "
                                "Transparente: sin fondo, para exportar con canal alfa.")
        form.addRow("Fondo", ed.cmb_bgsrc)

        self.hint = QLabel("Sin fondo: la vista previa muestra un tablero de ajedrez. Elige WebM, ProRes o PNG en el menú de "
                           "exportación para conservar la transparencia.")
        self.hint.setObjectName("Caption")
        self.hint.setWordWrap(True)
        form.addRow(self.hint)

        self.video = QWidget()
        vf = QFormLayout(self.video)
        vf.setContentsMargins(0, 0, 0, 0)
        vf.setVerticalSpacing(theme.SPACE[2])
        ed.btn_bgvideo = make_button("Elegir video…", "Ghost", self.choose_video)
        self.lb_video = QLabel("Ningún video")
        self.lb_video.setObjectName("Caption")
        self.lb_video.setWordWrap(True)
        vf.addRow("Video", ed.btn_bgvideo)
        vf.addRow(self.lb_video)
        self.sp_in = _spin("Dónde empieza el video (se recorta lo anterior)")
        self.sp_out = _spin("Dónde termina el video (0 = hasta el final)")
        self.sp_off = _spin("Adelanta (+) o atrasa (−) el video respecto al inicio del clip")
        self.cmb_loop, self.cmb_fit = QComboBox(), QComboBox()
        for k, v in LOOP_LABELS.items():
            self.cmb_loop.addItem(v, k)
        for k, v in FIT_LABELS.items():
            self.cmb_fit.addItem(v, k)
        self.cmb_fit.setToolTip("Cubrir llena el cuadro recortando; Contener muestra el video completo sobre la portada difuminada")
        vf.addRow("Entrada", self.sp_in)
        vf.addRow("Salida", self.sp_out)
        vf.addRow("Desfase", self.sp_off)
        vf.addRow("Al terminar", self.cmb_loop)
        vf.addRow("Ajuste", self.cmb_fit)
        pct = lambda v: f"{v} %"
        self.sl = {
            "zoom": SliderRow(100, 300, 100, lambda v: f"{v / 100:.2f}×"),
            "pan_x": SliderRow(-100, 100, 0, pct),
            "pan_y": SliderRow(-100, 100, 0, pct),
            "dim": SliderRow(0, 85, 25, pct, tooltip="Oscurece el video para que la letra se lea"),
            "blur": SliderRow(0, 100, 0, pct),
            "saturation": SliderRow(0, 160, 100, pct),
            "scrim": SliderRow(0, 100, 0, pct, tooltip="Degradado oscuro suave detrás del bloque de letra"),
            "cover_tint": SliderRow(0, 100, 0, pct, tooltip="Deja ver la portada difuminada por encima del video"),
        }
        for key, label in (("zoom", "Zoom"), ("pan_x", "Posición X"), ("pan_y", "Posición Y"), ("dim", "Oscurecer"),
                           ("blur", "Desenfoque"), ("saturation", "Saturación"), ("scrim", "Velo de lectura"),
                           ("cover_tint", "Tinte de portada")):
            vf.addRow(label, self.sl[key])
        form.addRow(self.video)

        self._action = CoalescedAction(120, self._apply, ed)
        ed.cmb_bgsrc.currentIndexChanged.connect(self._source_changed)
        for w in (self.sp_in, self.sp_out, self.sp_off):
            w.valueChanged.connect(lambda _v: self._action.schedule())
        for c in (self.cmb_loop, self.cmb_fit):
            c.currentIndexChanged.connect(lambda _i: self._action.schedule())
        for row in self.sl.values():
            row.valueChanged.connect(lambda _v: self._action.schedule())
            row.sliderReleased.connect(self._action.flush)
        self._show_rows("cover")

    # ---------------- estado <-> widgets ----------------
    def _show_rows(self, source: str):
        self.video.setVisible(source == "video")
        self.hint.setVisible(source == "transparent")

    def _current(self) -> BackgroundConfig:
        ed = self.ed
        old = ed.project.background if ed.project else BackgroundConfig()
        c = BackgroundConfig(**{k: getattr(old, k) for k in old.__dataclass_fields__})
        c.source = ed.cmb_bgsrc.currentData() or "cover"
        c.trim_in_ms, c.trim_out_ms, c.offset_ms = (self.sp_in.value() * 1000.0, self.sp_out.value() * 1000.0,
                                                    self.sp_off.value() * 1000.0)
        c.loop, c.fit = self.cmb_loop.currentData(), self.cmb_fit.currentData()
        c.zoom = self.sl["zoom"].value() / 100
        c.pan_x, c.pan_y = self.sl["pan_x"].value() / 100, self.sl["pan_y"].value() / 100
        c.dim, c.blur = self.sl["dim"].value() / 100, self.sl["blur"].value() / 100
        c.saturation, c.scrim = self.sl["saturation"].value() / 100, self.sl["scrim"].value() / 100
        c.cover_tint = self.sl["cover_tint"].value() / 100
        return c

    def refresh(self):
        """Widgets desde `project.background` (al abrir un proyecto o cambiar de fondo desde fuera)."""
        ed = self.ed
        cfg = ed.project.background if ed.project else BackgroundConfig()
        self._busy = True
        try:
            for w in (ed.cmb_bgsrc, self.cmb_loop, self.cmb_fit, self.sp_in, self.sp_out, self.sp_off, *(r.slider for r in self.sl.values())):
                w.blockSignals(True)
            ed.cmb_bgsrc.setCurrentIndex(max(0, ed.cmb_bgsrc.findData(cfg.source)))
            self.cmb_loop.setCurrentIndex(max(0, self.cmb_loop.findData(cfg.loop)))
            self.cmb_fit.setCurrentIndex(max(0, self.cmb_fit.findData(cfg.fit)))
            self.sp_in.setValue(cfg.trim_in_ms / 1000), self.sp_out.setValue(cfg.trim_out_ms / 1000), self.sp_off.setValue(cfg.offset_ms / 1000)
            for key, mul in (("zoom", 100), ("pan_x", 100), ("pan_y", 100), ("dim", 100), ("blur", 100), ("saturation", 100),
                             ("scrim", 100), ("cover_tint", 100)):
                self.sl[key].setValue(round(getattr(cfg, key) * mul))
        finally:
            for w in (ed.cmb_bgsrc, self.cmb_loop, self.cmb_fit, self.sp_in, self.sp_out, self.sp_off, *(r.slider for r in self.sl.values())):
                w.blockSignals(False)
            self._busy = False
        for row in self.sl.values():
            row.update_readout()
        v = cfg.video
        self.lb_video.setText(f"{Path(v.path).name} · {v.width}×{v.height} · {v.duration_ms / 1000:.1f} s · {v.fps:.0f} fps" if v else "Ningún video")
        self._show_rows(cfg.source)

    def set_source(self, source: str):
        """Cambia el fondo desde fuera (p. ej. al elegir un formato con transparencia)."""
        ed = self.ed
        i = ed.cmb_bgsrc.findData(source)
        if i >= 0 and ed.cmb_bgsrc.currentIndex() != i:
            ed.cmb_bgsrc.setCurrentIndex(i)  # dispara _source_changed

    # ---------------- acciones ----------------
    def _source_changed(self, _i=None):
        ed = self.ed
        src = ed.cmb_bgsrc.currentData()
        self._show_rows(src)
        if src == "video" and ed.project and ed.project.background.video is None:
            self._apply()
            self.choose_video()
            return
        self._apply()
        if src != "transparent" and ed.settings.value("export/target", "mp4") in ALPHA_TARGETS:
            ed.settings.setValue("export/target", "mp4")
            ed._export_bar.sync_menu()
            ed.notify("El formato volvió a MP4: los formatos con transparencia necesitan el fondo «Transparente».", "info")

    def _apply(self):
        ed = self.ed
        if self._busy or not ed.project:
            return
        cfg = self._current().clamped()
        if cfg.source == "video" and cfg.video is None:
            cfg.source = "cover"
        ed.project.background = cfg
        self._show_rows(cfg.source)
        ed.preview.update_background()

    def choose_video(self):
        ed = self.ed
        path, _ = QFileDialog.getOpenFileName(ed, "Elegir video de fondo", "", VIDEO_FILTER)
        if not path:
            if ed.project and ed.project.background.video is None:
                self.refresh()  # sin video no hay fondo de video: vuelve a lo anterior
            return
        self.load_video(path)

    def load_video(self, path: str | Path):
        from marea.render.cinematic import VideoError, probe_video

        ed = self.ed
        ed.btn_bgvideo.setEnabled(False)
        self.lb_video.setText("Leyendo el video…")

        def done(res):
            ed.btn_bgvideo.setEnabled(True)
            if not ed.project:
                return
            st = os.stat(res.path)
            old = ed.project.background
            cfg = BackgroundConfig(**{k: getattr(old, k) for k in old.__dataclass_fields__})
            cfg.source, cfg.video, cfg.video_size, cfg.video_mtime_ns = "video", res, st.st_size, st.st_mtime_ns
            cfg.trim_in_ms = cfg.trim_out_ms = cfg.offset_ms = 0.0
            ed.project.background = cfg.clamped()
            self.refresh()
            ed.preview.update_background()

        def failed(e):
            ed.btn_bgvideo.setEnabled(True)
            ed.notify(str(e) if isinstance(e, VideoError) else f"No pude abrir ese video: {e}", "error")
            self.refresh()

        pkg_attr("run_async", run_async)(lambda: probe_video(path, "bg_video"), done, failed)
