"""Cinematic background tab controller for the Editor."""
import copy
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QVBoxLayout,
    QWidget,
)

from marea.async_util import run_async
from marea.audio import local
from marea.model.cinematic import (
    DEFAULT_INTERVAL_MS,
    MIN_INTERVAL_MS,
    CinematicInterval,
    CinematicTreatment,
    compute_opening_trim_ms,
    suggest_intervals,
)
from marea.render.cinematic import VideoError, probe_video
from marea.ui import theme
from marea.ui.editor._pkg import pkg_attr
from marea.ui.widgets import CoalescedAction, SliderRow, blocked_signals, fmt_ms, make_button, scrollable

VIDEO_FILTER = "Video (*.mp4 *.mov *.mkv *.webm *.avi *.m4v);;Todos los archivos (*.*)"


def _dspin(lo: float, hi: float, val: float, step: float = 0.2) -> QDoubleSpinBox:
    sp = QDoubleSpinBox()
    sp.setRange(lo, hi)
    sp.setDecimals(2)
    sp.setSingleStep(step)
    sp.setSuffix(" s")
    sp.setValue(val)
    return sp


class CinematicController:
    """Manage the Editor cinematic background tab state, controls, and actions."""

    def __init__(self, editor):
        self.editor = editor
        self._video_busy = False
        self._suggest_gen = 0

    def reset_generation(self):
        self._suggest_gen = 0

    def build_tab(self) -> QWidget:
        ed = self.editor
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, theme.SPACE[3], theme.SPACE[1], 0)
        lay.setSpacing(theme.SPACE[2])

        top = QHBoxLayout()
        ed.cb_cinematic = QCheckBox("Activar fondo cinemático")
        ed.cb_cinematic.toggled.connect(self.toggled)
        ed.btn_video = make_button("Importar video…", "Ghost", self.choose_video)
        top.addWidget(ed.cb_cinematic, 1)
        top.addWidget(ed.btn_video)
        lay.addLayout(top)

        ed.lb_video_info = QLabel("Ningún video cargado. El audio del video se excluye siempre.")
        ed.lb_video_info.setObjectName("Caption")
        ed.lb_video_info.setWordWrap(True)
        lay.addWidget(ed.lb_video_info)

        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE[2])
        ed.btn_suggest = make_button(
            "Sugerir",
            "Ghost",
            self.suggest,
            "Sugiere momentos usando interludios, pausas, acentos y cambios de energía",
        )
        ed.btn_add_iv = make_button(
            "+ Momento",
            "Ghost",
            self.add_interval,
            "Crea un momento manual en la posición actual del preview",
        )
        ed.btn_del_iv = make_button("Quitar", "Ghost", self.remove_interval, "Elimina el momento seleccionado")
        for b in (ed.btn_suggest, ed.btn_add_iv, ed.btn_del_iv):
            actions.addWidget(b)
        lay.addLayout(actions)

        ed.iv_list = QListWidget()
        ed.iv_list.setObjectName("Intervals")
        ed.iv_list.setMinimumHeight(88)
        ed.iv_list.setMaximumHeight(112)
        ed.iv_list.currentRowChanged.connect(self._on_interval_selected)
        ed.iv_list.itemDoubleClicked.connect(self._on_interval_double_clicked)
        lay.addWidget(ed.iv_list)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(theme.SPACE[1])

        ed.sp_open_trim = _dspin(0, 36000, 0)
        ed.sp_open_trim.setToolTip("Recorte inicial del video para igualar su duración con la pista musical")
        ed.sp_open_trim.valueChanged.connect(self.opening_trim_edited)
        ed.btn_open_auto = make_button(
            "Auto",
            "Ghost",
            self.auto_opening_trim,
            "Calcula el recorte inicial usando las duraciones reales del video y del audio",
        )
        open_row = QHBoxLayout()
        open_row.setSpacing(theme.SPACE[1])
        open_row.addWidget(ed.sp_open_trim, 1)
        open_row.addWidget(ed.btn_open_auto)
        form.addRow("Recorte inicial", open_row)

        ed.sp_iv_start, ed.sp_iv_end = _dspin(0, 36000, 0), _dspin(0.2, 36000, 4.5)
        ed.sp_vid_start, ed.sp_vid_end = _dspin(0, 36000, 0), _dspin(0, 36000, 4.5)
        ed.sp_fade_in, ed.sp_fade_out = _dspin(0, 15, 0.9, 0.1), _dspin(0, 15, 1.1, 0.1)
        for sp in (ed.sp_iv_start, ed.sp_iv_end, ed.sp_vid_start, ed.sp_vid_end, ed.sp_fade_in, ed.sp_fade_out):
            sp.valueChanged.connect(self.interval_edited)

        for label, a, b in (
            ("Canción", ed.sp_iv_start, ed.sp_iv_end),
            ("Video", ed.sp_vid_start, ed.sp_vid_end),
            ("Fundidos", ed.sp_fade_in, ed.sp_fade_out),
        ):
            r = QHBoxLayout()
            r.setSpacing(theme.SPACE[1])
            sep = QLabel("→")
            sep.setObjectName("Caption")
            r.addWidget(a, 1)
            r.addWidget(sep)
            r.addWidget(b, 1)
            form.addRow(label, r)

        ed._cine_timer = CoalescedAction(16, ed.preview.update_cinematic, ed)
        specs = (
            ("Opacidad", "sl_cine_op", "lb_cine_op", 20, 100, 100),
            ("Desenfoque", "sl_cine_blur", "lb_cine_blur", 0, 100, 35),
            ("Velo", "sl_cine_dim", "lb_cine_dim", 0, 75, 32),
            ("Viñeta", "sl_cine_vig", "lb_cine_vig", 0, 100, 45),
            ("Grano", "sl_cine_grain", "lb_cine_grain", 0, 35, 8),
            ("Saturación", "sl_cine_sat", "lb_cine_sat", 0, 140, 82),
            ("Contraste", "sl_cine_con", "lb_cine_con", 60, 130, 92),
        )
        sliders = []
        for title, sl_attr, lb_attr, lo, hi, val in specs:
            srow = SliderRow(lo, hi, val, lambda v: f"{v} %", min_label_width=42)
            setattr(ed, sl_attr, srow.slider)
            setattr(ed, lb_attr, srow.label)
            sliders.append((title, srow.slider, srow.label))
            form.addRow(title, srow)
            srow.valueChanged.connect(self.treatment_changed)
            srow.sliderReleased.connect(self.flush_treatment)
        ed._cine_sliders = tuple(sliders)

        lay.addLayout(form)
        lay.addStretch()
        return scrollable(inner)

    def selected_interval(self) -> CinematicInterval | None:
        ed = self.editor
        if not ed.project:
            return None
        row = ed.iv_list.currentRow()
        ivs = ed.project.cinematic.intervals
        return ivs[row] if 0 <= row < len(ivs) else None

    @staticmethod
    def _fmt_interval_row(idx: int, iv: CinematicInterval) -> str:
        return (
            f"{idx + 1} · {fmt_ms(iv.start_ms, tenths=True)}–{fmt_ms(iv.end_ms, tenths=True)}"
            f"  ←  video {fmt_ms(iv.video_start_ms, tenths=True)}–{fmt_ms(iv.video_end_ms, tenths=True)}"
        )

    def sync_auto_opening_trim(self):
        ed = self.editor
        p = ed.project
        if not p or not p.cinematic.primary_video or not p.cinematic.auto_opening_trim:
            return
        res = p.cinematic.primary_video
        p.cinematic.opening_trim_ms = compute_opening_trim_ms(res.duration_ms, ed._song_duration_ms(), resource=res)

    def opening_trim_edited(self, *_):
        ed = self.editor
        p = ed.project
        if not p or not p.cinematic.primary_video:
            return
        res = p.cinematic.primary_video
        max_open = max(0.0, float(res.duration_ms) - MIN_INTERVAL_MS) if res.duration_ms > 0 else 36_000_000.0
        p.cinematic.auto_opening_trim = False
        p.cinematic.opening_trim_ms = min(max_open, max(0.0, round(ed.sp_open_trim.value() * 1000.0, 1)))
        self.refresh()

    def auto_opening_trim(self):
        ed = self.editor
        p = ed.project
        if not p or not p.cinematic.primary_video:
            return
        p.cinematic.auto_opening_trim = True
        self.sync_auto_opening_trim()
        self.refresh()
        ed.notify(f"Recorte inicial ajustado a {fmt_ms(p.cinematic.opening_trim_ms, tenths=True)}.", "ok")

    def refresh(self, keep_id: str | None = None):
        ed = self.editor
        p = ed.project
        if not p:
            return
        cfg = p.cinematic
        with blocked_signals(ed.cb_cinematic):
            ed.cb_cinematic.setChecked(cfg.enabled)

        res = cfg.primary_video
        ed.btn_video.setText("Reemplazar video…" if res else "Importar video…")
        if res:
            fps_txt = f"{int(round(res.fps))} fps" if abs(res.fps - round(res.fps)) < 0.01 else f"{res.fps:.2f} fps"
            trim_txt = f" · inicio +{fmt_ms(cfg.opening_trim_ms, tenths=True)}" if cfg.opening_trim_ms >= 50.0 else ""
            missing = " · no encontrado" if not Path(res.path).is_file() else ""
            ed.lb_video_info.setText(
                f"{Path(res.path).name} · {fmt_ms(res.duration_ms, tenths=True)} · {res.width}×{res.height} · {fps_txt}{trim_txt}{missing}"
            )
        else:
            ed.lb_video_info.setText("Ningún video cargado. El audio del video se excluye siempre.")

        prev_id = keep_id or (self.selected_interval().id if self.selected_interval() else "")
        with blocked_signals(ed.iv_list):
            ed.iv_list.clear()
            sel_row = -1
            for i, iv in enumerate(cfg.intervals):
                ed.iv_list.addItem(self._fmt_interval_row(i, iv))
                if iv.id == prev_id:
                    sel_row = i
            if sel_row < 0 and cfg.intervals:
                sel_row = 0
            if sel_row >= 0:
                ed.iv_list.setCurrentRow(sel_row)

        has_video = res is not None
        for w in (ed.btn_suggest, ed.btn_add_iv, ed.sp_open_trim, ed.btn_open_auto):
            w.setEnabled(has_video)
        max_open_s = max(0.0, (res.duration_ms - MIN_INTERVAL_MS) / 1000.0) if (res and res.duration_ms > 0) else 36000.0
        with blocked_signals(ed.sp_open_trim):
            ed.sp_open_trim.setMaximum(max(0.2, max_open_s))
            ed.sp_open_trim.setValue(cfg.opening_trim_ms / 1000.0 if has_video else 0.0)
        self.sync_controls()

    def sync_controls(self):
        ed = self.editor
        p = ed.project
        iv = self.selected_interval()
        has_iv = iv is not None
        ed.btn_del_iv.setEnabled(has_iv)
        for sp in (ed.sp_iv_start, ed.sp_iv_end, ed.sp_vid_start, ed.sp_vid_end, ed.sp_fade_in, ed.sp_fade_out):
            sp.setEnabled(has_iv)
        res = p.cinematic.video_for(iv) if (p and iv) else (p.cinematic.primary_video if p else None)
        max_v = res.duration_ms / 1000.0 if (res and res.duration_ms > 0) else 36000.0
        if iv is not None:
            for sp, val, hi in (
                (ed.sp_iv_start, iv.start_ms / 1000.0, 36000.0),
                (ed.sp_iv_end, iv.end_ms / 1000.0, 36000.0),
                (ed.sp_vid_start, iv.video_start_ms / 1000.0, max_v),
                (ed.sp_vid_end, iv.video_end_ms / 1000.0, max_v),
                (ed.sp_fade_in, iv.fade_in_ms / 1000.0, 15.0),
                (ed.sp_fade_out, iv.fade_out_ms / 1000.0, 15.0),
            ):
                with blocked_signals(sp):
                    sp.setMaximum(max(0.2, hi))
                    sp.setValue(val)
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
        for (_, sl, lb), v in zip(ed._cine_sliders, vals):
            with blocked_signals(sl):
                sl.setValue(v)
            lb.setText(f"{v} %")

    def toggled(self, on: bool):
        ed = self.editor
        if not ed.project:
            return
        ed.project.cinematic.enabled = on
        ed.preview.update_cinematic()

    def choose_video(self):
        ed = self.editor
        if not ed.project or self._video_busy:
            return
        path, _ = QFileDialog.getOpenFileName(ed, "Elegir video musical", "", VIDEO_FILTER)
        if path:
            self.import_video(path)

    def import_video(self, path: str | Path):
        ed = self.editor
        if not ed.project or self._video_busy:
            return
        project = ed.project
        vid_id = project.cinematic.next_video_id()
        had_video = project.cinematic.primary_video is not None
        self._video_busy = True
        ed.btn_video.setEnabled(False)
        ed.notify("Inspeccionando el video…")

        def done(resource):
            self._video_busy = False
            ed.btn_video.setEnabled(True)
            if ed.project is not project:
                return
            project.cinematic.set_primary_video(resource)
            self.sync_auto_opening_trim()
            if not had_video:
                project.cinematic.enabled = True
            self.refresh()
            ed.preview.update_cinematic()
            ed.notify(f"Video listo: {Path(resource.path).name} ({resource.width}×{resource.height}).", "ok")

        def failed(err):
            self._video_busy = False
            ed.btn_video.setEnabled(True)
            msg = str(err) if isinstance(err, VideoError) else "No pude abrir ese archivo de video."
            ed.notify(msg, "error")

        pkg_attr("run_async", run_async)(lambda: probe_video(path, vid_id), done, failed)

    def suggest(self):
        ed = self.editor
        p = ed.project
        if not p or not p.cinematic.primary_video:
            ed.notify("Importa primero un video para sugerir momentos.", "error")
            return
        self.sync_auto_opening_trim()
        res = p.cinematic.primary_video
        samples = p.audio_samples if ed.cmb_source.currentData() == "local" else None
        song_dur = ed._song_duration_ms()
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
            ed.notify("No encontré momentos claros en esta canción. Puedes añadir uno manual con «+ Momento».", "info")
            return
        p.cinematic.intervals = suggested
        p.cinematic.enabled = True
        c_start, c_end = ed._clip_range()
        focus_iv = next((iv for iv in suggested if iv.end_ms > c_start and iv.start_ms < c_end), suggested[0])
        self.refresh(keep_id=focus_iv.id)
        ed.preview.update_cinematic()
        ed.notify(f"Se sugirieron {len(suggested)} momentos sincronizados con la canción.", "ok")

    def add_interval(self):
        ed = self.editor
        p = ed.project
        if not p or not p.cinematic.primary_video:
            return
        c_start, c_end = ed._clip_range()
        cur_ms = ed.preview.scene.engine.t * 1000.0 if ed.preview.scene else c_start
        start_ms = max(0.0, cur_ms)
        end_ms = min(max(c_end, start_ms + MIN_INTERVAL_MS), start_ms + DEFAULT_INTERVAL_MS)
        if end_ms - start_ms < MIN_INTERVAL_MS:
            start_ms = max(0.0, end_ms - DEFAULT_INTERVAL_MS)
        res = p.cinematic.primary_video
        dur = end_ms - start_ms
        open_ms = p.cinematic.opening_trim_ms
        song_dur = max(1.0, ed._song_duration_ms())
        v_max = max(0.0, float(res.duration_ms or DEFAULT_INTERVAL_MS))
        v_usable = max(0.0, v_max - open_ms - dur)
        v_offset = open_ms + min(1.0, start_ms / max(1.0, song_dur - dur)) * v_usable if v_max > dur else 0.0
        iv = p.cinematic.add_interval(start_ms, end_ms, video_start_ms=v_offset, video_end_ms=v_offset + dur)
        p.cinematic.enabled = True
        self.refresh(keep_id=iv.id)
        ed.preview.update_cinematic()

    def remove_interval(self):
        ed = self.editor
        p, iv = ed.project, self.selected_interval()
        if not p or not iv:
            return
        p.cinematic.remove_interval(iv.id)
        self.refresh()
        ed.preview.update_cinematic()

    def _on_interval_selected(self, _row: int):
        self.sync_controls()

    def _on_interval_double_clicked(self, _item):
        ed = self.editor
        iv = self.selected_interval()
        if not iv or not ed.project:
            return
        c_start, c_end = ed._clip_range()
        if c_start <= iv.start_ms <= c_end:
            ed.preview.seek(iv.start_ms - c_start)

    def interval_edited(self, *_):
        ed = self.editor
        p, iv = ed.project, self.selected_interval()
        if not p or not iv:
            return
        start_ms = round(ed.sp_iv_start.value() * 1000.0, 1)
        end_ms = round(ed.sp_iv_end.value() * 1000.0, 1)
        if end_ms < start_ms + 200.0:
            end_ms = start_ms + 200.0
            with blocked_signals(ed.sp_iv_end):
                ed.sp_iv_end.setValue(end_ms / 1000.0)
        res = p.cinematic.video_for(iv)
        max_v = float(res.duration_ms) if (res and res.duration_ms > 0) else 36_000_000.0
        v_start = min(max_v, round(ed.sp_vid_start.value() * 1000.0, 1))
        v_end = min(max_v, max(v_start, round(ed.sp_vid_end.value() * 1000.0, 1)))
        if abs(v_end - ed.sp_vid_end.value() * 1000.0) > 50:
            with blocked_signals(ed.sp_vid_end):
                ed.sp_vid_end.setValue(v_end / 1000.0)
        iv.start_ms, iv.end_ms = start_ms, end_ms
        iv.video_start_ms, iv.video_end_ms = v_start, v_end
        iv.fade_in_ms = round(ed.sp_fade_in.value() * 1000.0, 1)
        iv.fade_out_ms = round(ed.sp_fade_out.value() * 1000.0, 1)
        keep_id = iv.id
        p.cinematic.sort_intervals()
        self.refresh(keep_id=keep_id)
        ed.preview.update_cinematic()

    def treatment_changed(self, *_):
        ed = self.editor
        if not ed.project:
            return
        tr = CinematicTreatment(
            opacity=ed.sl_cine_op.value() / 100.0,
            blur=ed.sl_cine_blur.value() / 100.0,
            dim=ed.sl_cine_dim.value() / 100.0,
            vignette=ed.sl_cine_vig.value() / 100.0,
            grain=ed.sl_cine_grain.value() / 100.0,
            saturation=ed.sl_cine_sat.value() / 100.0,
            contrast=ed.sl_cine_con.value() / 100.0,
        ).clamped()
        for _, sl, lb in ed._cine_sliders:
            lb.setText(f"{sl.value()} %")
        ed.project.cinematic.default_treatment = copy.copy(tr)
        iv = self.selected_interval()
        if iv is not None:
            iv.treatment = tr
        if any(sl.isSliderDown() for _, sl, _ in ed._cine_sliders):
            if not ed._cine_timer.is_active():
                ed._cine_timer.schedule()
        else:
            ed.preview.update_cinematic()

    def flush_treatment(self):
        ed = self.editor
        if ed._cine_timer.is_active():
            ed._cine_timer.flush()
