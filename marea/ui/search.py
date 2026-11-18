"""Buscar: resultados de Spotify o Deezer (o enlaces) con portada, artista, duración y etiquetas de letra."""
from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from marea.async_util import run_async
from marea.model.lyrics import VALID_SOURCE_TAGS
from marea.providers.track import Track
from marea.ui import lang, theme
from marea.ui.source_tag import paint_source_pill, source_label
from marea.ui.widgets import LYRICS_BADGES, EmptyState, fmt_ms, make_button, placeholder_pixmap, rounded_pixmap

DEBOUNCE_MS = 450
ROW_H = 68
THUMB = 48
TRACK_ROLE, KIND_ROLE, SOURCE_ROLE = Qt.UserRole, Qt.UserRole + 1, Qt.UserRole + 2


def fmt_duration(ms: int) -> str:
    return fmt_ms(ms, tenths=False)


class ResultDelegate(QStyledItemDelegate):
    def __init__(self, page: "SearchPage"):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_H)

    def paint(self, p: QPainter, opt, index):
        t, track, kind = theme.current, index.data(TRACK_ROLE), index.data(KIND_ROLE)
        source_tag = index.data(SOURCE_ROLE)
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
        badge = lang.t(LYRICS_BADGES[kind]) if kind in LYRICS_BADGES else "…"  # «…» mientras se consulta
        f.setPixelSize(theme.FONT_SIZES["xs"])
        f.setWeight(f.Weight.DemiBold)
        p.setFont(f)
        bw = p.fontMetrics().horizontalAdvance(badge) + 20
        pill = QRectF(right - bw, r.center().y() - 12, bw, 24)
        p.setBrush(theme.qc(t.pill))
        p.drawRoundedRect(pill, 12, 12)
        p.setPen(theme.qc(t.accent_icon if kind == "syllable" else t.text_muted if kind == "line" else t.text_faint))
        p.drawText(pill, Qt.AlignCenter, badge)
        left_edge = pill.left()

        if kind in ("syllable", "line") and source_tag in VALID_SOURCE_TAGS:
            s_text = source_label(source_tag)
            sw = p.fontMetrics().horizontalAdvance(s_text) + 24
            spill = QRectF(left_edge - 8 - sw, r.center().y() - 11, sw, 22)
            paint_source_pill(p, spill, source_tag, s_text, f)
            left_edge = spill.left()

        f.setWeight(f.Weight.Normal)
        f.setPixelSize(theme.FONT_SIZES["sm"])
        p.setFont(f)
        p.setPen(theme.qc(t.text_muted))
        dur = fmt_duration(track.duration_ms)
        dw = p.fontMetrics().horizontalAdvance(dur)
        p.drawText(QRectF(left_edge - 14 - dw, r.top(), dw, r.height()), Qt.AlignVCenter | Qt.AlignRight, dur)
        text_w = max(40.0, left_edge - 14 - dw - 14 - x)
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
        prov_label = "Deezer" if getattr(track, "provider", "spotify") == "deezer" else ""
        parts = [p_txt for p_txt in (track.artist, track.album, prov_label) if p_txt]
        sub = " · ".join(parts)
        p.drawText(QRectF(x, r.top() + 34, text_w, 20), Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(sub, Qt.ElideRight, int(text_w)))
        p.restore()


class SearchPage(QWidget):
    def __init__(self, services, on_open_track, on_settings, notify, debounce_ms: int = DEBOUNCE_MS):
        super().__init__()
        self.setObjectName("Page")
        self.services, self.on_open_track, self.on_settings, self.notify = services, on_open_track, on_settings, notify
        self._req = 0
        self._prefer_deezer = False
        self._active_provider = ""
        self._thumbs: dict[str, QPixmap] = {}
        self._ph: dict[str, QPixmap] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[6])
        lay.setSpacing(theme.SPACE[4])
        head = QHBoxLayout()
        h1 = QLabel()
        lang.bind(h1, "search.title")
        h1.setObjectName("H1")
        head.addWidget(h1)
        head.addStretch()
        self.mode_note = QLabel()
        self.mode_note.setObjectName("Caption")
        self.btn_settings = make_button("", "Ghost", on_settings)
        lang.bind(self.btn_settings, "search.goSettings")
        self.btn_deezer = make_button("", "Chip", self._toggle_deezer)
        self.btn_deezer.setCheckable(True)
        lang.bind(self.btn_deezer, "search.useDeezer")
        head.addWidget(self.mode_note)
        head.addWidget(self.btn_deezer)
        head.addWidget(self.btn_settings)
        lay.addLayout(head)

        self.input = QLineEdit()
        self.input.setObjectName("Search")
        lang.bind(self.input, "search.placeholder", "setPlaceholderText")
        self.input.textChanged.connect(self._on_text)
        self.input.returnPressed.connect(self._search_now)
        lay.addWidget(self.input)

        self.stack = QStackedWidget()
        self.off = EmptyState("search", "", "", [("", on_settings, True)])
        lang.bind(self.off.head, "search.offTitle")
        lang.bind(self.off.body, "search.offBody")
        lang.bind(self.off.buttons[0], "search.goSettings")
        self.hint = EmptyState("search", "", "")
        lang.bind(self.hint.head, "search.hintTitle")
        lang.bind(self.hint.body, "search.hintBody")
        self.none = EmptyState("search", "", "")
        lang.bind(self.none.head, "search.noneTitle")
        lang.bind(self.none.body, "search.noneBody")
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
        lang.signals.changed.connect(self._retranslate_dynamic)
        services.connection_changed.connect(lambda _: self._sync())
        self._sync()

    def _retranslate_dynamic(self):
        self._sync()
        self.list.viewport().update()

    def _toggle_deezer(self, checked: bool):
        self._prefer_deezer = checked
        if self.input.text().strip():
            self._search_now()
        else:
            self._sync()

    def _sync(self):
        connected = self.services.connected
        self.btn_settings.setVisible(not connected)
        self.btn_deezer.setVisible(connected)
        if not connected:
            lang.bind(self.mode_note, "search.deezerFallbackNote")
        elif self._active_provider == "deezer" or self._prefer_deezer:
            lang.bind(self.mode_note, "search.deezerActiveNote")
        else:
            self.mode_note.setText("")
        if self.stack.currentWidget() is self.off:
            self.stack.setCurrentWidget(self.list if self.list.count() else self.hint)

    # ---- búsqueda ----
    def _on_text(self, text: str):
        if not text.strip():
            self._req += 1  # descarta resultados en vuelo
            self._active_provider = ""
            self.list.clear()
            self._sync()
            self.stack.setCurrentWidget(self.hint)
            return
        self._timer.start()

    def _search_now(self):
        self._timer.stop()
        q = self.input.text().strip()
        if not q:
            return
        self._req += 1
        req = self._req
        run_async(lambda: self.services.search_tracks(q, prefer_deezer=self._prefer_deezer),
                  lambda res: self._show(req, *res), lambda e: self._failed(req, e))

    def _failed(self, req: int, error):
        if req == self._req:
            self.notify(str(error) if hasattr(error, "args") and error.args else lang.t("search.failed"), "error")

    def _show(self, req: int, tracks: list[Track], provider: str = ""):
        if req != self._req:
            return  # llegó tarde: ya se escribió otra cosa
        self._active_provider = provider
        self._sync()
        self.list.clear()
        for t in tracks:
            it = QListWidgetItem()
            it.setData(TRACK_ROLE, t)
            it.setData(KIND_ROLE, None)
            it.setData(SOURCE_ROLE, None)
            it.setSizeHint(QSize(0, ROW_H))
            self.list.addItem(it)
            self._load_kind(req, it, t)
            self._load_thumb(req, t)
        self.stack.setCurrentWidget(self.list if tracks else self.none)

    def _load_kind(self, req: int, item: QListWidgetItem, t: Track):
        def done(probe):
            if req == self._req:
                item.setData(KIND_ROLE, probe.kind)
                item.setData(SOURCE_ROLE, probe.source_tag)
                self.list.viewport().update()

        def fail(_e):
            if req == self._req:
                item.setData(KIND_ROLE, "error")
                item.setData(SOURCE_ROLE, None)
                self.list.viewport().update()

        run_async(lambda: self.services.lyrics_probe(t), done, fail)

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
