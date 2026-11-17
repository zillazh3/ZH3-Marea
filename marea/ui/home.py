"""Inicio, como en la web: tarjeta principal con las formas de empezar, «Sonando en Spotify» y proyectos recientes."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListView, QListWidget, QVBoxLayout,
                               QWidget)

from marea import library
from marea.async_util import run_async
from marea.providers.spotify import Playback
from marea.ui import lang, theme
from marea.ui.library import TILE, make_item, project_caption
from marea.ui.widgets import make_button, placeholder_pixmap, rounded_pixmap, scrollable

COVER = 200
LYRICS_STATUS = {"syllable": "home.lyricsSyllable", "line": "home.lyricsLine", "none": "home.lyricsNone",
                 "error": "home.lyricsError"}  # tipo de letra -> clave i18n


class NowPlayingCard(QWidget):
    def __init__(self, services, on_open_track, extra_actions=()):
        super().__init__()
        self.services, self.on_open_track, self.track, self._token, self._has_cover = services, on_open_track, None, 0, False
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
        self.btn = make_button("", "Primary", lambda: self.track and self.on_open_track(self.track))
        lang.bind(self.btn, "home.createClip")
        row = QHBoxLayout()
        row.setSpacing(theme.SPACE[3])
        row.addWidget(self.btn)
        for text, cb in extra_actions:
            row.addWidget(make_button(text, "Ghost", cb))
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
        if not self._has_cover:
            self.cover.setPixmap(placeholder_pixmap(COVER, theme.RADIUS["card"], 64))

    def set_playback(self, pb: Playback):
        t = pb.track
        if pb.is_playing:
            lang.bind(self.state, "home.nowPlaying")
        else:
            lang.bind(self.state, "home.paused")
        if self.track and self.track.id == t.id:
            return  # misma canción: solo cambió play/pausa
        self.track, self._has_cover = t, False
        self._token += 1
        token = self._token
        self.title.setText(t.title)
        self.artist.setText(f"{t.artist} · {t.album}" if t.album else t.artist)
        lang.bind(self.badge, "home.lyricsLooking")
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
            lang.bind(self.badge, LYRICS_STATUS[kind])
            self.btn.setEnabled(kind in ("syllable", "line"))


class RecentProjects(QListWidget):
    """Tira horizontal con los últimos proyectos de la Biblioteca; clic = abrirlo en el Editor."""
    SHOWN = 12

    def __init__(self, on_open_project):
        super().__init__()
        self.setObjectName("Library")
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setIconSize(QSize(TILE, TILE))
        self.setGridSize(QSize(TILE + theme.SPACE[5], TILE + 64))
        self.setFixedHeight(TILE + 64 + 14)  # tarjeta + barra de desplazamiento
        self.setWordWrap(True)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.itemClicked.connect(lambda it: on_open_project(it.data(Qt.UserRole)[1]))
        lang.signals.changed.connect(self.reload)  # los subtítulos de las tarjetas llevan texto traducido

    def reload(self) -> int:
        self.clear()
        entries = library.load()[:self.SHOWN]
        for e in entries:
            self.addItem(make_item("project", e, project_caption(e)))
        return len(entries)


class HomePage(QWidget):
    def __init__(self, services, on_open_track, on_open_ttml, on_open_project_dialog, on_demo, on_search,
                 on_open_project, on_settings, queue=None):
        super().__init__()
        self.setObjectName("Page")
        self.services, self._pb = services, None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        outer.addWidget(scrollable(body))
        lay = QVBoxLayout(body)
        lay.setContentsMargins(theme.SPACE[6], theme.SPACE[5], theme.SPACE[6], theme.SPACE[6])
        lay.setSpacing(theme.SPACE[4])

        hero = QFrame()
        hero.setObjectName("Card")
        row = QHBoxLayout(hero)
        row.setContentsMargins(theme.SPACE[5], theme.SPACE[5], theme.SPACE[5], theme.SPACE[5])
        row.setSpacing(theme.SPACE[6])
        self.hero_cover = QLabel()
        self.hero_cover.setFixedSize(COVER, COVER)
        info = QVBoxLayout()
        info.setSpacing(theme.SPACE[3])
        self.title = QLabel()
        lang.bind(self.title, "home.heroTitle")
        self.title.setObjectName("H1")
        self.title.setWordWrap(True)
        sub = QLabel()
        lang.bind(sub, "home.heroBody")
        sub.setObjectName("Body")
        sub.setWordWrap(True)
        info.addWidget(self.title)
        info.addWidget(sub)
        info.addSpacing(theme.SPACE[2])
        main_row = QHBoxLayout()
        main_row.setSpacing(theme.SPACE[3])
        self.btn_search = make_button("", "Primary", on_search)
        btn_ttml, btn_project = make_button("", "Ghost", on_open_ttml), make_button("", "Ghost", on_open_project_dialog)
        lang.bind(self.btn_search, "home.searchSong")
        lang.bind(btn_ttml, "home.openTtml")
        lang.bind(btn_project, "home.openProject")
        for w in (self.btn_search, btn_ttml, btn_project):
            main_row.addWidget(w)
        main_row.addStretch()
        demo_row = QHBoxLayout()
        btn_demo = make_button("", "Ghost", on_demo)
        lang.bind(btn_demo, "home.tryDemo")
        demo_row.addWidget(btn_demo)
        demo_row.addStretch()
        info.addLayout(main_row)
        info.addLayout(demo_row)
        info.addStretch()
        row.addWidget(self.hero_cover, alignment=Qt.AlignTop)
        row.addLayout(info, 1)
        lay.addWidget(hero)

        self.now = QFrame()  # «Sonando en Spotify»; sin conexión invita a conectar, conectado sin nada sonando se oculta
        self.now.setObjectName("Card")
        nl = QVBoxLayout(self.now)
        nl.setContentsMargins(theme.SPACE[5], theme.SPACE[4], theme.SPACE[5], theme.SPACE[5])
        nl.setSpacing(theme.SPACE[3])
        self.now_title = QLabel()
        self.now_title.setObjectName("H2")
        self.connect_hint = QWidget()
        hint = QHBoxLayout(self.connect_hint)
        hint.setContentsMargins(0, 0, 0, 0)
        hint_text = QLabel()
        lang.bind(hint_text, "home.connectHint")
        hint_text.setObjectName("Body")
        hint_text.setWordWrap(True)
        self.btn_connect = make_button("", "Ghost", on_settings)
        lang.bind(self.btn_connect, "home.connectSpotify")
        hint.addWidget(hint_text, 1)
        hint.addWidget(self.btn_connect)
        self.card = NowPlayingCard(services, on_open_track)
        nl.addWidget(self.now_title)
        nl.addWidget(self.connect_hint)
        nl.addWidget(self.card)
        lay.addWidget(self.now)

        self.recents_title = QLabel()
        lang.bind(self.recents_title, "home.recents")
        self.recents_title.setObjectName("H2")
        self.recents = RecentProjects(on_open_project)
        lay.addWidget(self.recents_title)
        lay.addWidget(self.recents)
        lay.addStretch()
        theme.signals.changed.connect(self._placeholder)
        self._placeholder()
        self.reload_recents()
        services.now_playing.connect(self._on_playback)
        services.connection_changed.connect(lambda _: self._sync())
        self._sync()

    def _placeholder(self):
        self.hero_cover.setPixmap(placeholder_pixmap(COVER, theme.RADIUS["card"], 64))

    def reload_recents(self):
        has = self.recents.reload() > 0
        self.recents_title.setVisible(has)
        self.recents.setVisible(has)

    def showEvent(self, e):
        super().showEvent(e)
        self.reload_recents()

    def _on_playback(self, pb):
        self._pb = pb
        if pb:
            self.card.set_playback(pb)
        self._sync()

    def _sync(self):
        connected = self.services.connected
        playing = connected and self._pb is not None
        if playing:
            lang.bind(self.now_title, "home.nowOnSpotify")
        else:
            lang.unbind(self.now_title)
            self.now_title.setText("Spotify")
        self.connect_hint.setVisible(not connected)
        self.card.setVisible(playing)
        self.now.setVisible(not connected or playing)
