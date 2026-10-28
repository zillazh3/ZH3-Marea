"""Sincronía automática de letras (experimental): panel de Ajustes y ventana de revisión.

Este módulo solo se importa cuando la persona activa la función en Ajustes → Funciones experimentales: con la función apagada la app no
carga nada de `marea.lab`. Todo el estilo sale de los tokens de `theme` (ningún color suelto).
"""
import dataclasses
import hashlib
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from marea.audio import local
from marea.export.ffmpeg import find_ffmpeg
from marea.export.job import unique_path
from marea.lab import alignment as A
from marea.lab import env
from marea.lab.align_job import apply_result, from_text_result, needs_review, request_lines, text_request_lines
from marea.lab.player import Player
from marea.lab.session import Session
from marea.lab.sidecar import SidecarClient, worker_argv
from marea.lab.ttml_in import from_ttml
from marea.paths import app_data
from marea.ui import theme
from marea.ui.net import run_async

FLAG = "experimental/align"
LAB_DIR_KEY = "experimental/lab_dir"
LANGS = [("Español", "es"), ("Inglés", "en")]
STAGE_NAMES = {"decode": "Leyendo el audio", "separate": "Separando la voz", "emissions": "Analizando el audio con el modelo",
               "energy": "Midiendo la energía de la voz", "align": "Alineando la letra", "asr": "Transcribiendo", "download": "Descargando"}
# (nombre, dónde se comprueba, id, orden del sidecar, parámetros, tamaño aproximado)
COMPONENTS = [
    ("Alineación · español", "hf", "jonatasgrosman/wav2vec2-large-xlsr-53-spanish", "fetch_model", {"lang": "es"}, "1.2 GB"),
    ("Alineación · inglés", "hf", "facebook/wav2vec2-base-960h", "fetch_model", {"lang": "en"}, "0.4 GB"),
    ("Transcripción · Whisper large-v3-turbo", "hf", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "fetch_asr", {}, "1.6 GB"),
    ("Separación de voz · BS-Roformer", "sep", env.SEPARATION_MODEL, "fetch_separator", {}, "0.6 GB"),
]


def enabled(settings) -> bool:
    return settings.value(FLAG, False, type=bool)


def lab_root(settings) -> Path:
    custom = settings.value(LAB_DIR_KEY, "")
    return Path(custom) if custom else env.lab_dir()


def stage_text(name: str, state: str, seconds: float = 0.0) -> str:
    label = STAGE_NAMES.get(name, name)
    return {"start": f"{label}…", "done": f"{label}: listo en {seconds:.1f} s", "cached": f"{label}: ya estaba hecho (caché)",
            "error": f"{label}: falló"}.get(state, f"{label}: {state}")


def prerequisites(root: Path, *, lang: str, separate: bool, audio: Path | None, asr: bool = False) -> str | None:
    """Lo que falta para poder alinear/transcribir, en palabras (None = todo listo). Solo mira el disco."""
    if audio is None or not audio.exists():
        return "Elige primero un archivo de audio."
    if not env.is_installed(root):
        return "Falta instalar el entorno de sincronía (Ajustes → Funciones experimentales → Instalar)."
    if not lang and not asr:
        return "Elige el idioma de la letra."
    for _, kind, ident, _cmd, params, _size in COMPONENTS:
        wanted = (kind == "hf" and params.get("lang") == lang and not asr) or (asr and ident.endswith("large-v3-turbo"))
        if wanted and not env.hf_model_ready(root, ident):
            return "Falta descargar el modelo de " + ("transcripción" if asr else f"alineación ({lang})") + " (Ajustes → Funciones experimentales)."
    if separate and not (env.separation_installed(root) and env.separator_model_ready(root)):
        return "Falta instalar o descargar la separación de voz (Ajustes → Funciones experimentales), o desactiva «Separar la voz»."
    return None


# ---------------- el proceso aparte ----------------

class Runner(QObject):
    """Un worker del sidecar con una orden a la vez. Sin red por defecto; `online=True` es el modo descarga (único que puede bajar modelos)."""
    stage = Signal(str)  # texto legible de lo que realmente está pasando
    done = Signal(str, dict)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, root: Path, online: bool = False, parent=None, client: SidecarClient | None = None):
        super().__init__(parent)
        self.root, self.online = root, online
        self.client = client or SidecarClient(self)
        self._queue: list[tuple[str, dict]] = []
        self._cmd = None
        self._ready = False
        c = self.client
        c.ready.connect(self._on_ready)
        c.stage.connect(lambda rid, name, state, sec: self.stage.emit(stage_text(name, state, sec)))
        c.result.connect(self._on_result)
        c.failed.connect(self._on_failed)
        c.cancelled.connect(self._on_cancelled)
        c.exited.connect(self._on_exit)

    @property
    def busy(self) -> bool:
        return self._cmd is not None or bool(self._queue)

    def ask(self, cmd: str, **params):
        self._queue.append((cmd, params))
        self._ensure_started()
        self._pump()

    def _ensure_started(self):
        if not self.client.running:
            self._ready = False
            extra = {"HF_HOME": str(self.root / "hf")}
            if self.online:
                extra["HF_HUB_OFFLINE"] = "0"
            self.client.start(worker_argv(str(env.env_python(self.root))), extra_env=extra)

    def cancel(self):
        self._queue.clear()
        if self._cmd is not None:
            self.client.cancel()

    def close(self):
        self._queue.clear()
        self.client.stop()

    def _pump(self):
        if self._ready and self._queue and self._cmd is None:
            cmd, params = self._queue.pop(0)
            self._cmd = cmd
            self.client.request(cmd, cache_dir=str(self.root / "cache"), **params)

    def _on_ready(self):
        self._ready = True
        self._pump()

    def _finish(self) -> str:
        cmd, self._cmd = self._cmd, None
        return cmd or ""

    def _on_result(self, _rid, data):
        cmd = self._finish()
        self.done.emit(cmd, data)
        self._pump()

    def _on_failed(self, _rid, message, _detail):
        cmd = self._finish()
        self.failed.emit(cmd, message)
        self._pump()

    def _on_cancelled(self, _rid):
        self._finish()
        self.cancelled.emit()

    def _on_exit(self, _code):
        self._ready = False
        if self._queue:  # el proceso terminó (p. ej. lo mataron al cancelar) con órdenes todavía en cola: arranca otro
            self._ensure_started()


# ---------------- línea de tiempo ----------------

RULER_H, WAVE_H, LANE_H, GAP = 18, 64, 24, 4
LANES = ("line", "word", "syllable", "vowel")
PEAK_MS = 10
EDGE_PX = 5
STEPS = [100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000]


def conf_token(conf: float) -> str:
    t = theme.current
    return t.ok if conf >= 0.3 else t.accent_icon if conf >= 0.1 else t.danger


class TimelineWidget(QWidget):
    """Forma de onda y bloques de línea / palabra / sílaba / vocal con su confianza; pausas rayadas. Sin mutar el modelo: emite señales."""
    unit_selected = Signal(str)
    unit_edited = Signal(str, int, int)
    play_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(RULER_H + WAVE_H + 4 * LANE_H + 3 * GAP + 8)
        self.setMouseTracking(True)
        self.session = None
        self._peaks = np.zeros(0, np.float32)
        self._duration = 1.0
        self._view = [0.0, 20000.0]
        self._sel, self._playhead, self._review = "", None, set()
        self._drag = None
        theme.signals.changed.connect(self.update)

    # ---- datos ----
    def set_session(self, session: Session):
        self.session = session
        self._duration = max(1.0, float(session.alignment.duration_ms))
        self._view = [0.0, min(self._duration, 20000.0)]
        self.refresh()

    def set_samples(self, samples: np.ndarray | None):
        if samples is None or not len(samples):
            self._peaks = np.zeros(0, np.float32)
        else:
            per = round(local.RATE * PEAK_MS / 1000)
            m = np.abs(samples).max(axis=1) if samples.ndim > 1 else np.abs(samples)
            n = len(m) // per * per
            self._peaks = m[:n].reshape(-1, per).max(axis=1) if n else np.zeros(0, np.float32)
        self.update()

    def refresh(self):
        self._review = set(self.session.review_ids()) if self.session else set()
        self.update()

    def set_selected(self, uid: str):
        self._sel = uid
        self.update()

    def set_playhead(self, ms: float | None):
        self._playhead = ms
        self.update()

    def center_on(self, ms: float):
        span = self._view[1] - self._view[0]
        a = min(max(0.0, ms - span / 2), max(0.0, self._duration - span))
        self._view = [a, a + span]
        self.update()

    # ---- geometría ----
    def _x(self, ms: float) -> float:
        return (ms - self._view[0]) / (self._view[1] - self._view[0]) * self.width()

    def _ms(self, x: float) -> float:
        return self._view[0] + x / max(1, self.width()) * (self._view[1] - self._view[0])

    def _lane_top(self, lane: int) -> int:
        return RULER_H + WAVE_H + GAP + lane * (LANE_H + GAP)

    def blocks(self) -> list[dict]:
        """Todo lo que se dibuja, por carril: {lane, id, start, end, text, conf, approx, manual, pause}."""
        out = []
        if not self.session:
            return out
        for line in self.session.alignment.lines:
            out.append(dict(lane=0, id=line.id, start=line.start, end=line.end, text=line.text, conf=line.confidence, approx=line.approximate,
                            manual=line.manual_override, pause=False))
            for p in line.pauses:
                out.append(dict(lane=1, id="", start=p.start, end=p.end, text="", conf=1.0, approx=False, manual=False, pause=True))
            for w in line.words:
                out.append(dict(lane=1, id=w.id, start=w.start, end=w.end, text=w.text, conf=w.confidence, approx=w.approximate,
                                manual=w.manual_override, pause=False))
                for s in w.syllables:
                    out.append(dict(lane=2, id=s.id, start=s.start, end=s.end, text=s.text, conf=s.confidence, approx=s.approximate,
                                    manual=s.manual_override, pause=False))
                    for v in s.vowels:
                        out.append(dict(lane=3, id="", start=v.start, end=v.end, text=v.text, conf=v.confidence, approx=True, manual=False, pause=False))
        return out

    def unit_at(self, x: float, y: float) -> tuple[dict, str] | None:
        """(bloque, 'start'|'end'|'') bajo el cursor; los bordes tienen prioridad para poder arrastrarlos."""
        lane = (y - (RULER_H + WAVE_H + GAP)) // (LANE_H + GAP)
        if not 0 <= lane <= 2:
            return None
        hits = [b for b in self.blocks() if b["lane"] == lane and b["id"] and self._x(b["start"]) - EDGE_PX <= x <= self._x(b["end"]) + EDGE_PX]
        if not hits:
            return None
        for b in hits:
            if abs(x - self._x(b["start"])) <= EDGE_PX:
                return b, "start"
            if abs(x - self._x(b["end"])) <= EDGE_PX:
                return b, "end"
        return min(hits, key=lambda b: b["end"] - b["start"]), ""

    # ---- pintura ----
    def paintEvent(self, _e):
        t, p = theme.current, QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qc(t.surface))
        p.drawRoundedRect(QRectF(self.rect()), theme.RADIUS["control"], theme.RADIUS["control"])
        a, b = self._view
        step = next((s for s in STEPS if (b - a) / s <= 14), STEPS[-1])
        p.setPen(QPen(theme.qc(t.text_faint)))
        for k in range(int(a // step) + 1, int(b // step) + 1):
            x = self._x(k * step)
            p.drawLine(QPointF(x, RULER_H - 5), QPointF(x, RULER_H))
            p.drawText(QPointF(x + 3, RULER_H - 6), f"{int(k * step // 60000)}:{k * step % 60000 / 1000:04.1f}")
        mid = RULER_H + WAVE_H / 2
        if len(self._peaks):
            p.setPen(QPen(theme.qc(t.text_muted), 1))
            for px in range(0, w, 2):
                i0, i1 = int(self._ms(px) / PEAK_MS), max(int(self._ms(px + 2) / PEAK_MS), int(self._ms(px) / PEAK_MS) + 1)
                seg = self._peaks[max(0, i0):i1]
                if len(seg):
                    h = min(1.0, float(seg.max())) * (WAVE_H / 2 - 2)
                    p.drawLine(QPointF(px, mid - h), QPointF(px, mid + h))
        for blk in self.blocks():
            if blk["end"] >= a and blk["start"] <= b:
                self._draw_block(p, blk, w)
        if self._playhead is not None:
            p.setPen(QPen(theme.qc(t.text), 1.5))
            x = self._x(self._playhead)
            p.drawLine(QPointF(x, RULER_H), QPointF(x, self.height()))

    def _draw_block(self, p: QPainter, blk: dict, width: int):
        t = theme.current
        drag = self._drag if self._drag and self._drag["id"] == blk["id"] else None
        s, e = (drag["start"], drag["end"]) if drag else (blk["start"], blk["end"])
        x0, x1 = self._x(s), self._x(e)
        rect = QRectF(x0, self._lane_top(blk["lane"]), max(1.5, x1 - x0), LANE_H)
        if blk["pause"]:
            p.setPen(QPen(theme.qc(t.text_faint), 1))
            p.setBrush(QBrush(theme.qc(t.text_faint), Qt.BDiagPattern))
            p.drawRect(rect)
            return
        col = theme.qc(conf_token(blk["conf"]))
        fill = theme.qc(conf_token(blk["conf"]))
        selected = blk["id"] and blk["id"] == self._sel
        fill.setAlpha(150 if selected else 70)
        pen = QPen(theme.qc(t.accent) if blk["manual"] else col, 2 if blk["manual"] or selected else 1)
        if selected:
            pen = QPen(theme.qc(t.accent_icon), 2)
        elif blk["approx"] and not blk["manual"]:
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(fill)
        p.drawRoundedRect(rect, 4, 4)
        if rect.width() > 22 and blk["text"]:
            p.setPen(QPen(theme.qc(t.text)))
            p.drawText(rect.adjusted(4, 0, -3, 0), Qt.AlignVCenter | Qt.AlignLeft, p.fontMetrics().elidedText(blk["text"], Qt.ElideRight, int(rect.width() - 7)))

    # ---- ratón ----
    def wheelEvent(self, e):
        a, b = self._view
        span = b - a
        if e.modifiers() & Qt.ControlModifier:
            anchor = self._ms(e.position().x())
            new = min(self._duration, max(500.0, span * (0.8 if e.angleDelta().y() > 0 else 1.25)))
            frac = (anchor - a) / span
            a = min(max(0.0, anchor - frac * new), max(0.0, self._duration - new))
            self._view = [a, a + new]
        else:
            shift = span * 0.1 * (-1 if e.angleDelta().y() > 0 else 1)
            a = min(max(0.0, a + shift), max(0.0, self._duration - span))
            self._view = [a, a + span]
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        hit = self.unit_at(e.position().x(), e.position().y())
        if hit is None:
            self.unit_selected.emit("")
            return
        blk, edge = hit
        self.unit_selected.emit(blk["id"])
        if edge:
            self._drag = {"id": blk["id"], "edge": edge, "start": blk["start"], "end": blk["end"], "orig": (blk["start"], blk["end"])}

    def mouseMoveEvent(self, e):
        if self._drag:
            ms = round(self._ms(e.position().x()) / 10) * 10
            d = self._drag
            if d["edge"] == "start":
                d["start"] = int(min(ms, d["end"] - 10))
            else:
                d["end"] = int(max(ms, d["start"] + 10))
            self.update()
            return
        hit = self.unit_at(e.position().x(), e.position().y())
        self.setCursor(Qt.SizeHorCursor if hit and hit[1] else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        d, self._drag = self._drag, None
        if d and (d["start"], d["end"]) != d["orig"]:
            self.unit_edited.emit(d["id"], d["start"], d["end"])
        self.update()

    def mouseDoubleClickEvent(self, e):
        self.play_requested.emit(self._ms(e.position().x()))


# ---------------- Ajustes ----------------

class LabSettingsPanel(QWidget):
    """Instalar/desinstalar el entorno pesado y descargar modelos, con el tamaño por delante y solo lo que realmente pasa."""

    def __init__(self, settings, notify, parent=None):
        super().__init__(parent)
        self.settings, self.notify = settings, notify
        self.confirm = lambda text: QMessageBox.question(self, "ZH3 Marea", text) == QMessageBox.Yes
        self._cancel = None
        self._runner = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACE[2])
        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        self.progress = QLabel()
        self.progress.setObjectName("Caption")
        self.progress.setWordWrap(True)
        row = QHBoxLayout()
        self.btn_install = QPushButton("Instalar entorno (≈ 4.5 GB)")
        self.btn_install.setObjectName("Primary")
        self.btn_uninstall = QPushButton("Desinstalar todo")
        self.btn_uninstall.setObjectName("Ghost")
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setObjectName("Ghost")
        for b in (self.btn_install, self.btn_uninstall, self.btn_cancel):
            b.setCursor(Qt.PointingHandCursor)
            row.addWidget(b)
        row.addStretch()
        self.btn_install.clicked.connect(self.install)
        self.btn_uninstall.clicked.connect(self.uninstall)
        self.btn_cancel.clicked.connect(self.cancel)
        lay.addWidget(self.status)
        lay.addLayout(row)
        self.rows = {}
        for name, kind, ident, cmd, params, size in COMPONENTS:
            r = QHBoxLayout()
            label = QLabel(f"{name} ({size})")
            label.setObjectName("Body")
            state = QLabel()
            state.setObjectName("Caption")
            btn = QPushButton("Descargar")
            btn.setObjectName("Ghost")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, c=(kind, ident, cmd, params, name): self.download(*c))
            r.addWidget(label, 1)
            r.addWidget(state)
            r.addWidget(btn)
            lay.addLayout(r)
            self.rows[ident] = (state, btn)
        note = QLabel("Todo se instala dentro de la carpeta «lab» de la app: borrarla lo desinstala. Los pesos de separación de voz vienen de un "
                      "repositorio comunitario sin licencia declarada: úsalos solo para tus propias pruebas. Nada se descarga sin que lo pidas.")
        note.setObjectName("Caption")
        note.setWordWrap(True)
        lay.addWidget(self.progress)
        lay.addWidget(note)
        self.refresh()

    @property
    def root(self) -> Path:
        return lab_root(self.settings)

    def refresh(self):
        root, busy = self.root, self._cancel is not None or (self._runner is not None and self._runner.busy)
        installed = env.is_installed(root)
        self.status.setText("Entorno: instalado." if installed else "Entorno: sin instalar. Es una descarga grande (≈ 4.5 GB: Python, torch y librerías).")
        self.btn_install.setEnabled(not installed and not busy)
        self.btn_uninstall.setEnabled(root.exists() and not busy)
        self.btn_cancel.setVisible(busy)
        for name, kind, ident, cmd, params, size in COMPONENTS:
            state, btn = self.rows[ident]
            ready = env.hf_model_ready(root, ident) if kind == "hf" else env.separator_model_ready(root, ident) and env.separation_installed(root)
            state.setText("Listo" if ready else "Pendiente")
            btn.setEnabled(installed and not ready and not busy)

    def install(self):
        if not self.confirm("Se va a descargar e instalar el entorno de sincronía (≈ 4.5 GB: Python, torch y librerías) dentro de la carpeta «lab» de la app. ¿Continuar?"):
            return
        import threading
        self._cancel = threading.Event()
        bridge = _Bridge()
        bridge.text.connect(self.progress.setText)
        self.progress.setText("Preparando…")
        run_async(lambda: env.install(self.root, lambda stage, text: bridge.text.emit(f"{stage}: {text}"), self._cancel),
                  lambda _: self._finished("Entorno instalado.", "ok"), self._failed)
        self._bridge = bridge
        self.refresh()

    def uninstall(self):
        if not self.confirm("Se borrará la carpeta «lab» completa: entorno, modelos descargados y cachés de alineación. Tus TTML y audios no se tocan. ¿Continuar?"):
            return
        try:
            env.uninstall(self.root)
        except (OSError, env.InstallError) as e:
            self.notify(f"No pude desinstalar: {e}", "error")
        else:
            self.notify("Sincronía automática desinstalada.", "ok")
        self.refresh()

    def download(self, kind: str, ident: str, cmd: str, params: dict, name: str):
        if kind == "sep" and not env.separation_installed(self.root):
            if not self.confirm("La separación de voz instala además la librería audio-separator y sus dependencias. ¿Continuar?"):
                return
            import threading
            self._cancel = threading.Event()
            bridge = _Bridge()
            bridge.text.connect(self.progress.setText)
            self._bridge = bridge
            run_async(lambda: env.install_separation(self.root, lambda s, t: bridge.text.emit(f"{s}: {t}"), self._cancel),
                      lambda _: (self._finished("Librería de separación instalada.", "ok"), self.download(kind, ident, cmd, params, name)), self._failed)
            self.refresh()
            return
        self._runner = Runner(self.root, online=True, parent=self)
        self._runner.stage.connect(self.progress.setText)
        self._runner.done.connect(lambda c, d: (self._finished(f"{name}: listo.", "ok"), self._runner.close()))
        self._runner.failed.connect(lambda c, msg: (self._failed(RuntimeError(msg)), self._runner.close()))
        self.progress.setText(f"Descargando {name}… (puede tardar varios minutos)")
        self._runner.ask(cmd, **params)
        self.refresh()

    def cancel(self):
        if self._cancel is not None:
            self._cancel.set()
        if self._runner is not None:
            self._runner.cancel()
        self.progress.setText("Cancelando…")

    def _finished(self, text: str, kind: str):
        self._cancel = None
        self.progress.setText("")
        self.notify(text, kind)
        self.refresh()

    def _failed(self, e):
        cancelled = isinstance(e, env.InstallCancelled)
        self._cancel = None
        self.progress.setText("")
        self.notify("Cancelado." if cancelled else str(e), "info" if cancelled else "error")
        self.refresh()


class _Bridge(QObject):
    """Para llevar texto desde un hilo de trabajo al hilo de la UI."""
    text = Signal(str)


# ---------------- ventana de revisión ----------------

def fmt(ms: float) -> str:
    return f"{int(ms // 60000)}:{ms % 60000 / 1000:05.2f}"


class LabReviewWindow(QWidget):
    """Prepara la alineación (Modo A con el TTML del proyecto, o Modo B con texto) y la revisa: línea de tiempo, lista de lo dudoso, edición
    con arrastre o números, desfase global, recálculo de una sección, vista previa en el Editor y exportación a un TTML NUEVO."""

    def __init__(self, editor, runner: Runner | None = None, player: Player | None = None):
        super().__init__(None, Qt.Window)
        self.setObjectName("LabWindow")
        self.setWindowTitle("Sincronizar con audio (experimental)")
        self.setWindowIcon(theme.app_icon())
        self.resize(1240, 800)
        self.editor, self.settings, self.notify = editor, editor.settings, editor.notify
        self.project = self._orig_project = editor.project
        self.root = lab_root(self.settings)
        self.runner = runner or Runner(self.root, parent=self)
        self.player = player or Player(self)
        self.session: Session | None = None
        self.skeleton = None  # Alignment de partida (Modo A)
        self._original = None  # bytes del TTML de origen
        self._samples = None
        self._selected = ""
        self._realign_id = ""
        self._from_asr = False
        self._detect_source()

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE[5], theme.SPACE[4], theme.SPACE[5], theme.SPACE[4])
        root.setSpacing(theme.SPACE[3])
        head = QHBoxLayout()
        title = QLabel("Sincronizar con audio")
        title.setObjectName("H1")
        tag = QLabel("EXPERIMENTAL")
        tag.setObjectName("Caption")
        self.btn_prepare = QPushButton("← Preparar")
        self.btn_prepare.setObjectName("Ghost")
        self.btn_prepare.setVisible(False)
        self.btn_prepare.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        head.addWidget(title)
        head.addWidget(tag, 0, Qt.AlignBottom)
        head.addStretch()
        head.addWidget(self.btn_prepare)
        root.addLayout(head)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_prepare())
        self.pages.addWidget(self._build_review())
        root.addWidget(self.pages, 1)
        self.lb_progress = QLabel()
        self.lb_progress.setObjectName("Caption")
        self.lb_progress.setWordWrap(True)
        root.addWidget(self.lb_progress)

        self.runner.stage.connect(self._progress)
        self.runner.done.connect(self._on_done)
        self.runner.failed.connect(self._on_failed)
        self.runner.cancelled.connect(self._on_cancelled)
        self.player.position.connect(self.timeline.set_playhead)
        self.player.finished.connect(lambda: self.timeline.set_playhead(None))
        theme.signals.changed.connect(self._palette)
        self._palette()
        self._refresh_audio()

    # ---- construcción ----
    def _palette(self):
        pal = self.palette()
        pal.setColor(QPalette.Window, theme.qc(theme.current.bg))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

    def _detect_source(self):
        src = getattr(self.project, "source", None)
        self.mode = "text"
        if src and Path(src).suffix.lower() in (".ttml", ".xml") and Path(src).exists():
            data = Path(src).read_bytes()
            skeleton = from_ttml(data, 0)
            if request_lines(skeleton):  # trae sílabas/palabras con tiempo que conservar
                skeleton.source_lyrics = {"kind": "ttml", "path_hint": str(src), "sha256": hashlib.sha256(data).hexdigest()}
                self.mode, self.skeleton, self._original = "ttml", skeleton, data

    def _build_prepare(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(theme.SPACE[5], theme.SPACE[4], theme.SPACE[5], theme.SPACE[4])
        cl.setSpacing(theme.SPACE[3])
        self.lb_mode = QLabel("Se alineará la letra de este proyecto (TTML con sílabas): conserva su texto, agentes y voces de fondo." if self.mode == "ttml" else
                              "Este proyecto no tiene un TTML con sílabas. Escribe o pega la letra (una línea por verso) o pide un borrador a Whisper.")
        self.lb_mode.setObjectName("Body")
        self.lb_mode.setWordWrap(True)
        self.lb_mode.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        cl.addWidget(self.lb_mode)
        row = QHBoxLayout()
        self.lb_audio = QLabel()
        self.lb_audio.setObjectName("Muted")
        self.btn_audio = QPushButton("Elegir audio…")
        self.btn_audio.setObjectName("Ghost")
        self.btn_audio.clicked.connect(self._choose_audio)
        row.addWidget(self.lb_audio, 1)
        row.addWidget(self.btn_audio)
        cl.addLayout(row)
        row = QHBoxLayout()
        lb = QLabel("Idioma de la letra")
        lb.setObjectName("Body")
        self.cb_lang = QComboBox()
        self.cb_lang.addItem("Detectar (solo para el borrador)", "")
        for name, code in LANGS:
            self.cb_lang.addItem(name, code)
        guess = self.skeleton.language[:2] if self.skeleton and self.skeleton.language else ""
        self.cb_lang.setCurrentIndex(max(0, self.cb_lang.findData(guess)))
        row.addWidget(lb)
        row.addStretch()
        row.addWidget(self.cb_lang)
        cl.addLayout(row)
        self.cb_sep = QCheckBox("Separar la voz antes de alinear (más preciso en música con mucho acompañamiento; ~1.5 min por canción)")
        self.cb_sep.setChecked(env.separator_model_ready(self.root) and env.separation_installed(self.root))
        cl.addWidget(self.cb_sep)
        self.ed_text = QPlainTextEdit()
        self.ed_text.setPlaceholderText("Una línea por verso…")
        self.ed_text.setMinimumHeight(220)
        self.ed_text.setPlainText("\n".join(l.text for l in getattr(self.project.lyrics, "lines", [])) if self.mode == "text" else "")
        self.ed_text.setVisible(self.mode == "text")
        cl.addWidget(self.ed_text)
        row = QHBoxLayout()
        self.btn_open = QPushButton("Abrir alineación guardada…")
        self.btn_open.setObjectName("Ghost")
        self.btn_asr = QPushButton("Transcribir un borrador (Whisper)")
        self.btn_asr.setObjectName("Ghost")
        self.btn_asr.setVisible(self.mode == "text")
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setObjectName("Ghost")
        self.btn_cancel.setVisible(False)
        self.btn_align = QPushButton("Alinear con el audio")
        self.btn_align.setObjectName("Primary")
        for b in (self.btn_open, self.btn_asr, self.btn_cancel, self.btn_align):
            b.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.btn_open)
        row.addStretch()
        row.addWidget(self.btn_asr)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_align)
        cl.addLayout(row)
        self.btn_open.clicked.connect(self._open_json)
        self.btn_asr.clicked.connect(self._transcribe)
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_align.clicked.connect(self._align)
        if self.mode == "text":  # el texto de la letra es lo que crece; sin él la tarjeta mide solo lo que contiene
            lay.addWidget(card, 1)
            cl.setStretchFactor(self.ed_text, 1)
        else:
            card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            lay.addWidget(card)
            lay.addStretch(1)
        return page

    def _build_review(self) -> QWidget:
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACE[4])
        left = QVBoxLayout()
        self.timeline = TimelineWidget()
        self.timeline.unit_selected.connect(self._select)
        self.timeline.unit_edited.connect(self._dragged)
        self.timeline.play_requested.connect(lambda ms: self._play(ms, ms + 8000))
        hint = QLabel("Clic: seleccionar · arrastra los bordes para ajustar · Ctrl+rueda: zoom · rueda: desplazar · doble clic: escuchar desde ahí. "
                      "Verde = confianza alta, azul = media, rojo = baja; borde punteado = tiempo aproximado; borde de acento = fijado por ti.")
        hint.setObjectName("Caption")
        hint.setWordWrap(True)
        left.addWidget(self.timeline, 1)
        left.addWidget(hint)
        bottom = QHBoxLayout()
        lb = QLabel("Desfase global")
        lb.setObjectName("Muted")
        self.sp_offset = QSpinBox()
        self.sp_offset.setRange(-20000, 20000)
        self.sp_offset.setSingleStep(10)
        self.sp_offset.setSuffix(" ms")
        self.sp_offset.editingFinished.connect(self._offset_edited)
        self.lb_offset = QLabel()
        self.lb_offset.setObjectName("Caption")
        self.btn_undo = QPushButton("Deshacer")
        self.btn_preview = QPushButton("Vista previa en el Editor")
        self.btn_back = QPushButton("Volver al original")
        self.btn_approve = QPushButton("Aprobar y exportar TTML nuevo")
        self.btn_approve.setObjectName("Primary")
        for b in (self.btn_undo, self.btn_preview, self.btn_back):
            b.setObjectName("Ghost")
        for b in (self.btn_undo, self.btn_preview, self.btn_back, self.btn_approve):
            b.setCursor(Qt.PointingHandCursor)
        self.btn_undo.clicked.connect(self._undo)
        self.btn_preview.clicked.connect(self._preview)
        self.btn_back.clicked.connect(self._back)
        self.btn_approve.clicked.connect(self._approve)
        bottom.addWidget(lb)
        bottom.addWidget(self.sp_offset)
        bottom.addWidget(self.lb_offset, 1)
        for b in (self.btn_undo, self.btn_preview, self.btn_back, self.btn_approve):
            bottom.addWidget(b)
        left.addLayout(bottom)
        lay.addLayout(left, 1)

        right = QFrame()
        right.setObjectName("Card")
        right.setFixedWidth(340)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(theme.SPACE[3], theme.SPACE[3], theme.SPACE[3], theme.SPACE[3])
        self.lb_review = QLabel()
        self.lb_review.setObjectName("H2")
        self.review_list = QListWidget()
        self.review_list.itemClicked.connect(lambda it: self._select(it.data(Qt.UserRole), center=True))
        self.review_list.itemDoubleClicked.connect(lambda it: (self._select(it.data(Qt.UserRole), center=True), self._play_selected()))
        rl.addWidget(self.lb_review)
        rl.addWidget(self.review_list, 1)
        self.lb_sel = QLabel("Selecciona una unidad")
        self.lb_sel.setObjectName("Body")
        self.lb_sel.setWordWrap(True)
        rl.addWidget(self.lb_sel)
        row = QHBoxLayout()
        self.sp_start, self.sp_end = QSpinBox(), QSpinBox()
        for sp in (self.sp_start, self.sp_end):
            sp.setRange(0, 3_600_000)
            sp.setSingleStep(10)
            sp.setSuffix(" ms")
            row.addWidget(sp)
        rl.addLayout(row)
        row = QHBoxLayout()
        self.btn_apply = QPushButton("Aplicar tiempos")
        self.btn_apply.setObjectName("Primary")
        self.btn_play = QPushButton("▶ Escuchar")
        self.btn_play.setObjectName("Ghost")
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_play)
        rl.addLayout(row)
        self.ed_line = QLineEdit()
        self.ed_line.setPlaceholderText("Texto de la línea")
        self.btn_realign = QPushButton("Recalcular esta sección")
        self.btn_realign.setObjectName("Ghost")
        rl.addWidget(self.ed_line)
        rl.addWidget(self.btn_realign)
        self.btn_apply.clicked.connect(self._apply_times)
        self.btn_play.clicked.connect(self._play_selected)
        self.btn_realign.clicked.connect(self._realign)
        self._set_unit_controls(False)
        lay.addWidget(right)
        return page

    # ---- audio ----
    def _refresh_audio(self):
        f = getattr(self.project, "audio_file", None)
        self.lb_audio.setText(f"Audio: {Path(f).name}" if f else "Audio: ninguno (elige el archivo de la canción)")
        self._samples = getattr(self.project, "audio_samples", None)

    def _choose_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, "Elegir audio", "", "Audio (*.mp3 *.wav *.flac *.m4a *.ogg)")
        if not path:
            return
        self._progress("Leyendo el audio…")

        def done(samples):
            self.project.audio_file, self.project.audio_samples = Path(path), samples
            self._refresh_audio()
            self._progress("")
            if hasattr(self.editor, "_refresh_audio_tab"):
                self.editor._refresh_audio_tab()

        run_async(lambda: local.decode(path), done, lambda e: self._progress(f"No pude leer ese audio: {e}"))

    def _ensure_samples(self, then):
        if self._samples is not None:
            then()
            return
        f = getattr(self.project, "audio_file", None)
        if not f:
            return

        def got(s):
            self._samples = s
            self.timeline.set_samples(s)
            then()

        run_async(lambda: local.decode(str(f)), got, lambda e: self._progress(f"No pude leer el audio: {e}"))

    # ---- progreso ----
    def _progress(self, text: str):
        self.lb_progress.setText(text)

    def _busy(self, on: bool):
        for b in (self.btn_align, self.btn_asr, self.btn_open, self.btn_audio):
            b.setEnabled(not on)
        self.btn_cancel.setVisible(on)

    def _cancel(self):
        self.runner.cancel()
        self._progress("Cancelando…")

    # ---- preparar / alinear ----
    def _lang_code(self) -> str:
        return self.cb_lang.currentData() or ""

    def _sep_model(self) -> str:
        return env.SEPARATION_MODEL if self.cb_sep.isChecked() else ""

    def _transcribe(self):
        problem = prerequisites(self.root, lang=self._lang_code(), separate=False, audio=getattr(self.project, "audio_file", None), asr=True)
        if problem:
            self._progress(problem)
            return
        self._busy(True)
        self.runner.ask("asr", audio=str(self.project.audio_file), ffmpeg=find_ffmpeg(), model=env.ASR_MODEL, lang=self._lang_code())

    def _align(self):
        lang = self._lang_code() or (self.skeleton.language[:2] if self.skeleton else "")
        problem = prerequisites(self.root, lang=lang, separate=self.cb_sep.isChecked(), audio=getattr(self.project, "audio_file", None))
        if problem:
            self._progress(problem)
            return
        if self.mode == "ttml":
            lines = request_lines(self.skeleton)
        else:
            lines = text_request_lines(self.ed_text.toPlainText().splitlines())
            if not lines:
                self._progress("Escribe o transcribe primero la letra.")
                return
        self._busy(True)
        self._realign_id = ""
        self.runner.ask("align", audio=str(self.project.audio_file), ffmpeg=find_ffmpeg(), lang=lang, lines=lines, separate=self._sep_model())

    def _on_done(self, cmd: str, data: dict):
        self._busy(False)
        if cmd == "asr":
            self._from_asr = True
            self.ed_text.setPlainText("\n".join(l["text"] for l in data["lines"]))
            i = self.cb_lang.findData(data["language"])
            if not self._lang_code() and i >= 0:
                self.cb_lang.setCurrentIndex(i)
            sus = sum(l["suspect"] for l in data["lines"])
            self._progress(f"Borrador de Whisper ({data['language']}): {len(data['lines'])} líneas"
                           + (f", {sus} sospechosas (poca voz o poca confianza)" if sus else "") + ". Revísalo y corrígelo antes de alinear.")
        elif cmd == "align" and self._realign_id:
            errors = self.session.apply_realign(self._realign_id, data)
            self._progress("Sección recalculada." if not errors else "No pude integrar el recálculo: " + errors[0])
            self._after_edit()
        elif cmd == "align":
            if self.mode == "ttml":
                al, original = apply_result(self.skeleton, data), self._original
            else:
                al, original = from_text_result(data, "asr" if self._from_asr else "text"), None
            self.session = Session(al, original)
            self._progress(f"Alineado ({data['device']['device']}). {len(self.session.review_ids())} unidades requieren revisión.")
            self._populate()

    def _on_failed(self, cmd: str, message: str):
        self._busy(False)
        self._progress(message or "Algo falló en la sincronía automática.")

    def _on_cancelled(self):
        self._busy(False)
        self._progress("Cancelado.")

    # ---- revisar ----
    def _populate(self):
        self.timeline.set_session(self.session)
        self._ensure_samples(lambda: self.timeline.set_samples(self._samples))
        go = self.session.alignment.global_offset
        self.sp_offset.setValue(int(go.get("ms", 0)))
        self.lb_offset.setText(f"{go.get('method', '')} · confianza {go.get('confidence', 0):.0%}" + ("  (sin desfase fiable: ajústalo si lo notas)"
                                                                                                         if go.get("method", "").endswith("rejected") else ""))
        self._selected = ""
        self._set_unit_controls(False)  # nada seleccionado todavía: ningún control de edición activo
        self.btn_prepare.setVisible(True)
        self.pages.setCurrentIndex(1)
        self._fill_review()

    def _fill_review(self):
        self.review_list.clear()
        ids = self.session.review_ids()
        total = sum(len(w.syllables or [w]) for l in self.session.alignment.lines for w in l.words)
        self.lb_review.setText(f"Requieren revisión: {len(ids)} de {total}")
        for uid in ids:
            kind, unit, _line = self.session.find(uid)
            it = QListWidgetItem(f"{fmt(unit.start)}  {unit.text}   ·  conf {unit.confidence:.2f}")
            it.setData(Qt.UserRole, uid)
            self.review_list.addItem(it)
        self.timeline.refresh()

    def _set_unit_controls(self, on: bool):
        for w in (self.sp_start, self.sp_end, self.btn_apply, self.btn_play, self.btn_realign):
            w.setEnabled(on)
        self.ed_line.setEnabled(on and bool(self.session and self.session.text_editable))

    def _select(self, uid: str, center: bool = False):
        self._selected = uid
        self.timeline.set_selected(uid)
        found = self.session.find(uid) if (uid and self.session) else None
        self._set_unit_controls(found is not None)
        if not found:
            self.lb_sel.setText("Selecciona una unidad")
            return
        kind, unit, line = found
        names = {"line": "Línea", "word": "Palabra", "syllable": "Sílaba"}
        self.lb_sel.setText(f"{names[kind]}: «{unit.text}» · confianza {unit.confidence:.2f}" + (" · aproximada" if unit.approximate else "")
                            + (" · fijada por ti" if unit.manual_override else ""))
        self.sp_start.setValue(int(unit.start))
        self.sp_end.setValue(int(unit.end))
        self.ed_line.setText(line.text)
        if center:
            self.timeline.center_on((unit.start + unit.end) / 2)

    def _after_edit(self):
        self.timeline.refresh()
        self._fill_review()
        if self._selected and self.session.find(self._selected):
            self._select(self._selected)

    def _apply_times(self):
        errors = self.session.set_times(self._selected, self.sp_start.value(), self.sp_end.value())
        if errors:
            self._progress("No se aplicó: " + errors[0])
            self._select(self._selected)
        else:
            self._progress("")
            self._after_edit()

    def _dragged(self, uid: str, start: int, end: int):
        errors = self.session.set_times(uid, start, end)
        self._progress("No se aplicó: " + errors[0] if errors else "")
        self._after_edit()

    def _undo(self):
        if self.session and self.session.undo():
            self.sp_offset.setValue(int(self.session.alignment.global_offset.get("ms", 0)))
            self._after_edit()

    def _offset_edited(self):
        if self.session and self.sp_offset.value() != int(self.session.alignment.global_offset.get("ms", 0)):
            self.session.set_offset(self.sp_offset.value())
            self.lb_offset.setText("manual · ajustado por ti")

    # ---- escuchar ----
    def _play(self, start_ms: float, end_ms: float):
        def go():
            try:
                self.player.play(self._samples, start_ms, end_ms)
            except OSError:
                self._progress("No hay dispositivo de salida de audio para reproducir.")

        self._ensure_samples(go)

    def _play_selected(self):
        found = self.session.find(self._selected) if self.session else None
        if found:
            self._play(max(0, found[1].start - 300), found[1].end + 500)

    # ---- recalcular ----
    def _realign(self):
        line = self.session.find(self._selected)[2]
        text = self.ed_line.text().strip()
        try:
            req = self.session.realign_request(line.id, text=text if self.session.text_editable and text and text != line.text else None)
        except ValueError as e:
            self._progress(str(e))
            return
        src = self.session.alignment.pipeline.get("source", "mix")
        self._realign_id = line.id
        self._busy(True)
        self.runner.ask("align", audio=str(self.project.audio_file), ffmpeg=find_ffmpeg(), lang=self.session.alignment.language,
                        separate=src.split(":", 1)[1] if src.startswith("vocals:") else "", **req)

    # ---- salida ----
    def _preview(self):
        try:
            lyrics = self.session.preview_lyrics()
        except A.AlignmentError as e:
            self._progress("La vista previa no es válida: " + str(e))
            return
        self.editor.set_project(dataclasses.replace(self._orig_project, lyrics=lyrics))
        self.notify("Vista previa cargada en el Editor. El archivo original no se tocó.", "info")

    def _back(self):
        self.editor.set_project(self._orig_project)
        self.notify("Proyecto original restaurado en el Editor.", "info")

    def _approve(self):
        src = getattr(self._orig_project, "source", None)
        folder = Path(src).parent if src else app_data() / "letras"  # junto a la app, no en Videos (ahí van los clips)
        name = (Path(src).stem if src else (self._orig_project.title or "letra")) + ".aligned.ttml"
        try:
            ttml, js = self.session.export(folder, name)
        except A.AlignmentError as e:
            self._progress("No se exportó: " + str(e))
            return
        except OSError:
            ttml, js = self.session.export(app_data() / "letras", name)
        from marea.paths import reveal_in_folder
        self.notify(f"Exportado {ttml.name} (el original no se tocó).", "ok", ("Abrir carpeta", lambda: reveal_in_folder(str(ttml))))

    def _open_json(self):
        path, _ = QFileDialog.getOpenFileName(self, "Abrir alineación", "", "Alineación (*.json)")
        if path:
            self.load_alignment(path)

    def load_alignment(self, path):
        try:
            al = A.load(path)
        except (A.AlignmentError, OSError, ValueError) as e:
            self._progress(f"No pude abrir esa alineación: {e}")
            return
        original = None
        hint = al.source_lyrics.get("path_hint")
        if al.source_lyrics.get("kind") == "ttml" and hint and Path(hint).exists():
            data = Path(hint).read_bytes()
            if hashlib.sha256(data).hexdigest() == al.source_lyrics.get("sha256"):
                original = data
            else:
                self._progress("El TTML de origen cambió desde que se guardó esta alineación: se exportará como TTML nuevo sin él.")
        self.mode = "ttml" if original else "text"
        self._original = original
        self.session = Session(al, original)
        self._populate()

    def closeEvent(self, e):
        self.runner.close()
        self.player.close()
        super().closeEvent(e)


def open_review(editor) -> LabReviewWindow:
    """Abre (o trae al frente) la ventana de revisión del proyecto actual del Editor."""
    win = getattr(editor, "_lab_window", None)
    if win is None or not win.isVisible() or win.project is not editor.project:
        if win is not None:
            win.close()
        win = editor._lab_window = LabReviewWindow(editor)
    win.show()
    win.raise_()
    win.activateWindow()
    return win
