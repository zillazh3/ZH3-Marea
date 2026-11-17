"""Fondo del clip en el Editor: portada, video propio o transparente (marea.model.background)."""
import os
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QLabel, QWidget

from marea.async_util import run_async
from marea.export.ffmpeg import ALPHA_TARGETS
from marea.model.background import FITS, LOOPS, BackgroundConfig
from marea.ui import lang, theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.editor.style_tab import form_row
from marea.ui.widgets import CoalescedAction, SliderRow, blocked_signals, make_button

# value -> i18n key (resolved where shown)
SOURCE_LABELS = {"cover": "background.source.cover", "video": "background.source.video", "transparent": "background.source.transparent"}
LOOP_LABELS = {"repeat": "background.loop.repeat", "pingpong": "background.loop.pingpong", "hold": "background.loop.hold"}
FIT_LABELS = {"cover": "background.fit.cover", "contain": "background.fit.contain"}
VIDEO_FILTER = "background.videoFilter"
assert set(LOOP_LABELS) == set(LOOPS) and set(FIT_LABELS) == set(FITS)


def _spin(tip: str) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setDecimals(2)
    s.setSingleStep(0.1)
    s.setSuffix(" s")
    s.setRange(-3600.0, 3600.0)
    lang.bind(s, tip, "setToolTip")
    return s


class BackgroundController:
    """Construye los controles de fondo dentro del formulario de la pestaña «Fondo» y los sincroniza con el proyecto."""

    def __init__(self, editor):
        self.ed = editor
        self._busy = False

    def build(self, form: QFormLayout):
        ed = self.ed
        ed.cmb_bgsrc = QComboBox()
        lang.bind_items(ed.cmb_bgsrc, [(label, key) for key, label in SOURCE_LABELS.items()])
        lang.bind(ed.cmb_bgsrc, "background.sourceTip", "setToolTip")
        form_row(form, "background.title", ed.cmb_bgsrc)

        self.hint = QLabel()
        lang.bind(self.hint, "background.transparentHint")
        self.hint.setObjectName("Caption")
        self.hint.setWordWrap(True)
        form.addRow(self.hint)

        self.video = QWidget()
        vf = QFormLayout(self.video)
        vf.setContentsMargins(0, 0, 0, 0)
        vf.setVerticalSpacing(theme.SPACE[2])
        ed.btn_bgvideo = make_button("", "Ghost", self.choose_video)
        lang.bind(ed.btn_bgvideo, "background.chooseVideo")
        self.lb_video = QLabel()
        lang.bind(self.lb_video, "background.noVideo")
        self.lb_video.setObjectName("Caption")
        self.lb_video.setWordWrap(True)
        form_row(vf, "background.video", ed.btn_bgvideo)
        vf.addRow(self.lb_video)
        self.sp_in = _spin("background.trimInTip")
        self.sp_out = _spin("background.trimOutTip")
        self.sp_off = _spin("background.offsetTip")
        self.cmb_loop, self.cmb_fit = QComboBox(), QComboBox()
        lang.bind_items(self.cmb_loop, [(label, k) for k, label in LOOP_LABELS.items()])
        lang.bind_items(self.cmb_fit, [(label, k) for k, label in FIT_LABELS.items()])
        lang.bind(self.cmb_fit, "background.fitTip", "setToolTip")
        form_row(vf, "background.trimIn", self.sp_in)
        form_row(vf, "background.trimOut", self.sp_out)
        form_row(vf, "background.offset", self.sp_off)
        form_row(vf, "background.loopLabel", self.cmb_loop)
        form_row(vf, "background.fitLabel", self.cmb_fit)
        pct = lambda v: f"{v} %"
        self.sl = {
            "zoom": SliderRow(100, 300, 100, lambda v: f"{v / 100:.2f}×"),
            "pan_x": SliderRow(-100, 100, 0, pct),
            "pan_y": SliderRow(-100, 100, 0, pct),
            "dim": SliderRow(0, 85, 25, pct),
            "blur": SliderRow(0, 100, 0, pct),
            "saturation": SliderRow(0, 160, 100, pct),
            "scrim": SliderRow(0, 100, 0, pct),
            "cover_tint": SliderRow(0, 100, 0, pct),
        }
        lang.bind(self.sl["dim"].slider, "background.dimTip", "setToolTip")
        lang.bind(self.sl["scrim"].slider, "background.scrimTip", "setToolTip")
        lang.bind(self.sl["cover_tint"].slider, "background.coverTintTip", "setToolTip")
        for key, label in (("zoom", "background.zoom"), ("pan_x", "background.panX"), ("pan_y", "background.panY"),
                           ("dim", "background.dim"), ("blur", "background.blur"), ("saturation", "background.saturation"),
                           ("scrim", "background.scrim"), ("cover_tint", "background.coverTint")):
            form_row(vf, label, self.sl[key])
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
        if v:
            lang.unbind(self.lb_video)
            self.lb_video.setText(f"{Path(v.path).name} · {v.width}×{v.height} · {v.duration_ms / 1000:.1f} s · {v.fps:.0f} fps")
        else:
            lang.bind(self.lb_video, "background.noVideo")
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
            ed.notify(lang.t("background.formatReset"), "info")

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
        path, _ = QFileDialog.getOpenFileName(ed, lang.t("background.chooseTitle"), "", lang.t(VIDEO_FILTER))
        if not path:
            if ed.project and ed.project.background.video is None:
                self.refresh()  # sin video no hay fondo de video: vuelve a lo anterior
            return
        self.load_video(path)

    def load_video(self, path: str | Path):
        from marea.render.cinematic import VideoError, probe_video

        ed = self.ed
        ed.btn_bgvideo.setEnabled(False)
        lang.bind(self.lb_video, "background.probing")

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
            ed.notify(str(e) if isinstance(e, VideoError) else lang.t("background.openFailed", error=e), "error")
            self.refresh()

        pkg_attr("run_async", run_async)(lambda: probe_video(path, "bg_video"), done, failed)
