"""Inicio: «usar lo que suena ahora» con su portada y el tipo de letra disponible."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from marea.export import history
from marea.paths import reveal_in_folder
from marea.providers.spotify import Playback
from marea.ui import theme
from marea.ui.net import run_async
from marea.ui.screens import EmptyState
from marea.ui.widgets import ElidedLabel, placeholder_pixmap, rounded_pixmap

COVER = 200


class NowPlayingCard(QWidget):
    def __init__(self, services, on_open_track, extra_actions):
        super().__init__()
        self.services, self.on_open_track, self.track, self._token = services, on_open_track, None, 0
        lay = QHBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(theme.SPACE[6])
        self.cover = QLabel()
        self.cover.setFixedSize(COVER, COVER)
        info = QVBoxLayout()
        info.setSpacing(theme.SPACE[2])
        self.state = QLabel()
        self.state.setObjectName("Caption")
        self.title, self.artist, self.badge = QLabel(), QLabel(), QLabel()
        self.title.setObjectName("H2")
        self.artist.setObjectName("Body")
        self.badge.setObjectName("Muted")
        for lb in (self.title, self.artist):
            lb.setWordWrap(True)
            lb.setMaximumWidth(420)
        self.btn = QPushButton("Crear clip con esta canción")
        self.btn.setObjectName("Primary")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(lambda: self.track and self.on_open_track(self.track))
        row = QHBoxLayout()
        row.setSpacing(theme.SPACE[3])
        row.addWidget(self.btn)
        for text, cb in extra_actions:
            b = QPushButton(text)
            b.setObjectName("Ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            row.addWidget(b)
        row.addStretch()
        for w in (self.state, self.title, self.artist, self.badge):
            info.addWidget(w)
        info.addSpacing(theme.SPACE[3])
        info.addLayout(row)
        info.addStretch()
        lay.addWidget(self.cover, alignment=Qt.AlignTop)
        lay.addLayout(info)
        theme.signals.changed.connect(self._placeholder)
        self._placeholder()

    def _placeholder(self):
        if not getattr(self, "_has_cover", False):
            self.cover.setPixmap(placeholder_pixmap(COVER, theme.RADIUS["card"], 64))

    def set_playback(self, pb: Playback):
        t = pb.track
        self.state.setText("SONANDO AHORA" if pb.is_playing else "EN PAUSA")
        if self.track and self.track.id == t.id:
            return  # misma canción: solo cambió play/pausa
        self.track, self._has_cover = t, False
        self._token += 1
        token = self._token
        self.title.setText(t.title)
        self.artist.setText(f"{t.artist} · {t.album}" if t.album else t.artist)
        self.badge.setText("Buscando letra…")
        self._placeholder()
        if t.cover_url:
            run_async(lambda: self.services.cover_bytes(t.cover_url), lambda data: self._set_cover(token, data), lambda e: None)
        run_async(lambda: self.services.lyrics_kind(t), lambda k: self._set_kind(token, k), lambda e: self._set_kind(token, "error"))

    def _set_cover(self, token: int, data: bytes):
        px = QPixmap()
        if token == self._token and px.loadFromData(data):
            self._has_cover = True
            self.cover.setPixmap(rounded_pixmap(px, COVER, theme.RADIUS["card"]))

    def _set_kind(self, token: int, kind: str):
        if token == self._token:
            self.badge.setText({"syllable": "Letra por sílaba", "line": "Letra por línea",
                                "none": "Sin letra sincronizada", "error": "No pude comprobar la letra"}[kind])
            self.btn.setEnabled(kind in ("syllable", "line"))


class RecentClips(QWidget):
    """Miniaturas 9:16 de los últimos clips exportados; clic = abrir su carpeta."""
    THUMB_W, THUMB_H, SHOWN = 108, 192, 6

    def __init__(self):
        super().__init__()
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(theme.SPACE[4])
        self.row.addStretch()

    def reload(self) -> int:
        while self.row.count() > 1:
            w = self.row.takeAt(0).widget()
            if w:
                w.deleteLater()
        entries = history.load()[:self.SHOWN]
        for i, e in enumerate(entries):
            col = QVBoxLayout()
            col.setSpacing(theme.SPACE[1])
            btn = QPushButton()
            btn.setObjectName("IconBtn")
            btn.setFixedSize(self.THUMB_W, self.THUMB_H)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(f"{e.get('artist', '')} – {e.get('title', '')}".strip(" –"))
            px = QPixmap()
            if e.get("thumb") and px.load(e["thumb"]):
                btn.setIcon(rounded_pixmap(px.scaled(self.THUMB_W * 2, self.THUMB_H * 2, Qt.KeepAspectRatioByExpanding,
                                                    Qt.SmoothTransformation), self.THUMB_W, theme.RADIUS["control"]))
                btn.setIconSize(QSize(self.THUMB_W, self.THUMB_H))
            btn.clicked.connect(lambda _=False, p=e["path"]: reveal_in_folder(p))
            name = ElidedLabel(e.get("title", ""))
            name.setObjectName("Caption")
            name.setFixedWidth(self.THUMB_W)
            col.addWidget(btn)
            col.addWidget(name)
            holder = QWidget()
            holder.setLayout(col)
            self.row.insertWidget(i, holder)
        return len(entries)


class HomePage(QWidget):
    def __init__(self, services, on_open_track, on_open_ttml, on_demo, on_settings, queue=None):
        super().__init__()
        self.setObjectName("Page")
        self.services, self._pb = services, None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[6])
        lay.setSpacing(theme.SPACE[4])
        h1 = QLabel("Inicio")
        h1.setObjectName("H1")
        lay.addWidget(h1)
        files = [("Abrir .ttml…", on_open_ttml), ("Probar con la canción demo", on_demo)]
        self.stack = QStackedWidget()
        self.off = EmptyState("audio-lines", "Conecta Spotify o abre un .ttml para empezar",
                              "Cuando haya algo sonando aparecerá aquí para armar un clip con su letra.",
                              [("Conectar Spotify", on_settings, True)] + [(t, c, False) for t, c in files])
        self.idle = EmptyState("audio-lines", "No hay nada sonando",
                               "Dale play en Spotify y aparecerá aquí con su letra.", [(t, c, False) for t, c in files])
        self.card = NowPlayingCard(services, on_open_track, files)
        for w in (self.off, self.idle, self.card):
            self.stack.addWidget(w)
        frame = QFrame()
        frame.setObjectName("Card")
        QVBoxLayout(frame).addWidget(self.stack)
        frame.layout().setContentsMargins(theme.SPACE[5], theme.SPACE[5], theme.SPACE[5], theme.SPACE[5])
        lay.addWidget(frame, 1)
        self.recents_title = QLabel("Clips recientes")
        self.recents_title.setObjectName("H2")
        self.recents = RecentClips()
        lay.addWidget(self.recents_title)
        lay.addWidget(self.recents)
        if queue:
            queue.item_done.connect(lambda _: self.reload_recents())
        self.reload_recents()
        services.now_playing.connect(self._on_playback)
        services.connection_changed.connect(lambda _: self._sync())
        self._sync()

    def reload_recents(self):
        has = self.recents.reload() > 0
        self.recents_title.setVisible(has)
        self.recents.setVisible(has)

    def _on_playback(self, pb):
        self._pb = pb
        if pb:
            self.card.set_playback(pb)
        self._sync()

    def _sync(self):
        if not self.services.connected:
            self.stack.setCurrentWidget(self.off)
        elif self._pb:
            self.stack.setCurrentWidget(self.card)
        else:
            self.stack.setCurrentWidget(self.idle)
