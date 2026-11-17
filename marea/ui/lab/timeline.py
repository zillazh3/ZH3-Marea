"""Timeline widget for alignment inspection and manual boundary adjustment."""
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QPainter, QPen
from PySide6.QtWidgets import QWidget

from marea.audio import local
from marea.lab.session import Session
from marea.ui import lang, theme

RULER_H, WAVE_H, LANE_H, GAP = 18, 64, 24, 4
LANES = ("line", "word", "syllable", "vowel")
PEAK_MS = 10
EDGE_PX = 5
STEPS = [100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000]


def conf_token(conf: float) -> str:
    t = theme.current
    return t.ok if conf >= 0.3 else t.accent_icon if conf >= 0.1 else t.danger


class TimelineWidget(QWidget):
    """Forma de onda y bloques de línea / palabra / sílaba / vocal con su confianza; pausas rayadas."""
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
        lang.signals.changed.connect(self.update)

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
        groups = self.lane_groups()
        regions = bool(self.session and self.session.alignment.source_lyrics.get("overlaps"))
        height = RULER_H + WAVE_H + (max(1, len(groups)) * 4 + int(regions)) * (LANE_H + GAP) + 8
        self.setMinimumHeight(height)
        self.update()

    def lane_groups(self) -> dict:
        if not self.session:
            return {}
        return {key: i for i, key in enumerate(dict.fromkeys((l.agent, l.role, l.layer) for l in self.session.alignment.lines))}

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
        groups = self.lane_groups()
        for line in self.session.alignment.lines:
            base = groups[(line.agent, line.role, line.layer)] * 4
            out.append(dict(lane=base, id=line.id, start=line.start, end=line.end, text=line.text, conf=line.confidence, approx=line.approximate,
                            manual=line.manual_override, pause=False))
            for p in line.pauses:
                out.append(dict(lane=base + 1, id="", start=p.start, end=p.end, text="", conf=1.0, approx=False, manual=False, pause=True))
            for w in line.words:
                out.append(dict(lane=base + 1, id=w.id, start=w.start, end=w.end, text=w.text, conf=w.confidence, approx=w.approximate,
                                manual=w.manual_override, pause=False))
                for s in w.syllables:
                    out.append(dict(lane=base + 2, id=s.id, start=s.start, end=s.end, text=s.text, conf=s.confidence, approx=s.approximate,
                                    manual=s.manual_override, pause=False))
                    for v in s.vowels:
                        out.append(dict(lane=base + 3, id="", start=v.start, end=v.end, text=v.text, conf=v.confidence, approx=True, manual=False, pause=False))
        for region in self.session.alignment.source_lyrics.get("overlaps", []):
            out.append(dict(lane=len(groups) * 4, id="", start=region["start"], end=region["end"],
                            text=lang.t("lab.timeline.overlap"), conf=region.get("role_confidence", 0.0),
                            approx=True, manual=False, pause=False))
        return out

    def unit_at(self, x: float, y: float) -> tuple[dict, str] | None:
        """(bloque, 'start'|'end'|'') bajo el cursor; los bordes tienen prioridad para poder arrastrarlos."""
        lane = (y - (RULER_H + WAVE_H + GAP)) // (LANE_H + GAP)
        if lane < 0 or lane % 4 == 3:
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
                self._draw_block(p, blk)
        p.setPen(QPen(theme.qc(t.text_muted)))
        unknown_agent = lang.t("lab.review.unknownAgent")
        for (agent, role, take), lane in self.lane_groups().items():
            p.drawText(QPointF(4, self._lane_top(lane * 4) - 2), f"{role} / {agent or unknown_agent} / {take + 1}")
        if self._playhead is not None:
            p.setPen(QPen(theme.qc(t.text), 1.5))
            x = self._x(self._playhead)
            p.drawLine(QPointF(x, RULER_H), QPointF(x, self.height()))

    def _draw_block(self, p: QPainter, blk: dict):
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
