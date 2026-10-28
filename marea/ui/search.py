"""Buscar: resultados de Spotify con portada, artista, duración y tipo de letra disponible."""
from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QStackedWidget, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

from marea.providers.spotify import Track
from marea.ui import theme
from marea.ui.net import run_async
from marea.ui.screens import EmptyState
from marea.ui.widgets import LYRICS_BADGES, placeholder_pixmap, rounded_pixmap

DEBOUNCE_MS = 450
ROW_H = 68
THUMB = 48
TRACK_ROLE, KIND_ROLE = Qt.UserRole, Qt.UserRole + 1


def fmt_duration(ms: int) -> str:
    return f"{ms // 60000}:{ms // 1000 % 60:02d}"


class ResultDelegate(QStyledItemDelegate):
    def __init__(self, page: "SearchPage"):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_H)

    def paint(self, p: QPainter, opt, index):
        t, track, kind = theme.current, index.data(TRACK_ROLE), index.data(KIND_ROLE)
        r = QRectF(opt.rect).adjusted(2, 2, -2, -2)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qc(t.hover) if opt.state & QStyle.State_MouseOver else Qt.NoBrush)
        p.drawRoundedRect(r, theme.RADIUS["card"], theme.RADIUS["card"])
        thumb = self.page.thumb(track)
        p.drawPixmap(int(r.left() + 10), int(r.top() + (r.height() - THUMB) / 2), thumb)
        f = opt.font
        x = r.left() + 10 + THUMB + 14
        right = r.right() - 14
        badge = LYRICS_BADGES.get(kind, "…")  # «…» mientras se consulta
        f.setPixelSize(theme.FONT_SIZES["xs"])
        f.setWeight(f.Weight.DemiBold)
        p.setFont(f)
        bw = p.fontMetrics().horizontalAdvance(badge) + 20
        pill = QRectF(right - bw, r.center().y() - 12, bw, 24)
        p.setBrush(theme.qc(t.pill))
        p.drawRoundedRect(pill, 12, 12)
        p.setPen(theme.qc(t.accent_icon if kind == "syllable" else t.text_muted if kind == "line" else t.text_faint))
        p.drawText(pill, Qt.AlignCenter, badge)
        f.setWeight(f.Weight.Normal)
        f.setPixelSize(theme.FONT_SIZES["sm"])
        p.setFont(f)
        p.setPen(theme.qc(t.text_muted))
        dur = fmt_duration(track.duration_ms)
        dw = p.fontMetrics().horizontalAdvance(dur)
        p.drawText(QRectF(pill.left() - 16 - dw, r.top(), dw, r.height()), Qt.AlignVCenter | Qt.AlignRight, dur)
        text_w = pill.left() - 16 - dw - 16 - x
        f.setPixelSize(theme.FONT_SIZES["md"])
        f.setWeight(f.Weight.DemiBold)
        p.setFont(f)
        p.setPen(theme.qc(t.text))
        p.drawText(QRectF(x, r.top() + 12, text_w, 22), Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(track.title, Qt.ElideRight, int(text_w)))
        f.setWeight(f.Weight.Normal)
        f.setPixelSize(theme.FONT_SIZES["sm"])
        p.setFont(f)
        p.setPen(theme.qc(t.text_muted))
        sub = f"{track.artist} · {track.album}" if track.album else track.artist
        p.drawText(QRectF(x, r.top() + 34, text_w, 20), Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(sub, Qt.ElideRight, int(text_w)))
        p.restore()


class SearchPage(QWidget):
    def __init__(self, services, on_open_track, on_settings, notify, debounce_ms: int = DEBOUNCE_MS):
        super().__init__()
        self.setObjectName("Page")
        self.services, self.on_open_track, self.notify = services, on_open_track, notify
        self._req = 0
        self._thumbs: dict[str, QPixmap] = {}
        self._ph: dict[str, QPixmap] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[6])
        lay.setSpacing(theme.SPACE[4])
        h1 = QLabel("Buscar")
        h1.setObjectName("H1")
        self.input = QLineEdit()
        self.input.setObjectName("Search")
        self.input.setPlaceholderText("Busca una canción por título o artista")
        self.input.textChanged.connect(self._on_text)
        self.input.returnPressed.connect(self._search_now)
        lay.addWidget(h1)
        lay.addWidget(self.input)

        self.stack = QStackedWidget()
        self.off = EmptyState("search", "Conecta Spotify para buscar",
                              "Necesitas conectar tu cuenta para buscar canciones. Se hace una sola vez.", [("Ir a Ajustes", on_settings, True)])
        self.hint = EmptyState("search", "Busca una canción", "Verás qué letra hay disponible: por sílaba, por línea o sin letra.")
        self.none = EmptyState("search", "No encontré resultados", "Prueba con otro título o agrega el nombre del artista.")
        self.list = QListWidget()
        self.list.setItemDelegate(ResultDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setCursor(Qt.PointingHandCursor)
        self.list.itemClicked.connect(lambda it: self.on_open_track(it.data(TRACK_ROLE)))
        for w in (self.off, self.hint, self.none, self.list):
            self.stack.addWidget(w)
        frame = QFrame()
        frame.setObjectName("Card")
        QVBoxLayout(frame).addWidget(self.stack)
        frame.layout().setContentsMargins(theme.SPACE[3], theme.SPACE[3], theme.SPACE[3], theme.SPACE[3])
        lay.addWidget(frame, 1)

        self._timer = QTimer(self, singleShot=True, interval=debounce_ms)
        self._timer.timeout.connect(self._search_now)
        services.connection_changed.connect(lambda _: self._sync())
        self._sync()

    def _sync(self):
        if not self.services.connected:
            self.stack.setCurrentWidget(self.off)
        elif self.stack.currentWidget() is self.off:
            self.stack.setCurrentWidget(self.list if self.list.count() else self.hint)

    # ---- búsqueda ----
    def _on_text(self, text: str):
        if not text.strip():
            self._req += 1  # descarta resultados en vuelo
            self.list.clear()
            self._sync()
            if self.services.connected:
                self.stack.setCurrentWidget(self.hint)
            return
        self._timer.start()

    def _search_now(self):
        self._timer.stop()
        q = self.input.text().strip()
        if not q or not self.services.connected:
            return
        self._req += 1
        req = self._req
        run_async(lambda: self.services.spotify.search(q),
                  lambda tracks: self._show(req, tracks), lambda e: self._failed(req, e))

    def _failed(self, req: int, error):
        if req == self._req:
            self.notify(str(error) if hasattr(error, "args") and error.args else "No pude buscar. Intenta de nuevo.", "error")

    def _show(self, req: int, tracks: list[Track]):
        if req != self._req:
            return  # llegó tarde: ya se escribió otra cosa
        self.list.clear()
        for t in tracks:
            it = QListWidgetItem()
            it.setData(TRACK_ROLE, t)
            it.setData(KIND_ROLE, None)
            it.setSizeHint(QSize(0, ROW_H))
            self.list.addItem(it)
            self._load_kind(req, it, t)
            self._load_thumb(req, t)
        self.stack.setCurrentWidget(self.list if tracks else self.none)

    def _load_kind(self, req: int, item: QListWidgetItem, t: Track):
        def done(kind):
            if req == self._req:
                item.setData(KIND_ROLE, kind)
                self.list.viewport().update()
        run_async(lambda: self.services.lyrics_kind(t), done, lambda e: done("error"))

    def _load_thumb(self, req: int, t: Track):
        if not t.cover_url or t.cover_url in self._thumbs:
            return

        def done(data):
            px = QPixmap()
            if px.loadFromData(data):
                self._thumbs[t.cover_url] = rounded_pixmap(px, THUMB, theme.RADIUS["control"])
                self.list.viewport().update()
        run_async(lambda: self.services.cover_bytes(t.cover_url), done, lambda e: None)

    def thumb(self, t: Track) -> QPixmap:
        return self._thumbs.get(t.cover_url) or self._placeholder()

    def _placeholder(self) -> QPixmap:
        key = theme.current.name  # una por tema: no se redibuja en cada fila
        if key not in self._ph:
            self._ph[key] = placeholder_pixmap(THUMB, theme.RADIUS["control"])
        return self._ph[key]
