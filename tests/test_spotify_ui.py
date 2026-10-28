import time

import pytest
from PySide6.QtCore import QEventLoop, QSettings, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from marea.demo_cover import demo_cover
from marea.model.lyrics import parse_lrc
from marea.providers.errors import ProviderError
from marea.providers.spotify import Playback, Track
from marea.ui.screens import SettingsBody
from marea.ui.search import KIND_ROLE, TRACK_ROLE
from marea.ui.services import Services

PNG = bytes(demo_cover(64).encodeToData())
LRC = "[00:05.00]primer verso de prueba\n[00:09.00]segundo verso de prueba\n[00:14.00]tercer verso\n[00:18.00]\n"


def T(i, title=None, artist="Artista", cover=True):
    return Track(f"id{i}", title or f"Canción {i}", [artist], "Disco", 180000 + i * 1000, f"cover{i}" if cover else None)


class FakeSpotify:
    def __init__(self, connected=True):
        self.connected, self.playing, self.results, self.fail = connected, None, {}, None
        self.delays, self.searched, self.authorize_error = {}, [], None
        self.commands, self.control_error = [], None

    def _command(self, name):
        if self.control_error:
            raise ProviderError(self.control_error)
        self.commands.append(name)

    def play(self, uri=None, position_ms=0):
        self._command("play")

    def pause(self):
        self._command("pause")

    def next_track(self):
        self._command("next")

    def previous_track(self):
        self._command("previous")

    def search(self, q):
        self.searched.append(q)
        time.sleep(self.delays.get(q, 0))
        return self.results.get(q, [])

    def now_playing(self):
        if self.fail:
            raise ProviderError(self.fail)
        return self.playing

    def cover(self, url):
        return PNG

    def authorize(self):
        if self.authorize_error:
            raise ProviderError(self.authorize_error)
        self.connected = True

    def disconnect(self):
        self.connected = False


class FakeLyrics:
    CREDIT = "Prueba · uploaded by <b>alguien</b>"

    def __init__(self):
        self.missing, self.broken, self.queries = set(), set(), []

    def fetch(self, q):
        self.queries.append(q)
        if q.title in self.broken:
            raise ProviderError("LRCLIB no respondió.")
        if q.title in self.missing:
            return None
        ly = parse_lrc(LRC)
        ly.credit, ly.writers = self.CREDIT, ["Ana", "Beto"]
        return ly


@pytest.fixture
def env(make_window, qapp, tmp_path):
    sp, ly = FakeSpotify(), FakeLyrics()
    services = Services(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat), spotify=sp, lyrics=ly, poll_ms=60_000)
    services.settings.setValue("spotify/client_id", "cid")
    win = make_window(services=services)
    win.show()
    return win, sp, ly, services


def pump(ms):
    """Espera con un bucle de eventos real. QTest.qWait no suelta el GIL y deja sin turno a los hilos que lanzan subprocess."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def until(cond, timeout=5000):
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        pump(20)
        if cond():
            return True
    return False


def search_page(win):
    return win.pages.widget(1)


def type_query(win, text):
    page = search_page(win)
    page.input.setText(text)
    page._search_now()  # sin esperar el debounce
    return page


def test_search_shows_results_with_lyrics_kind_and_opens_track(env):
    win, sp, ly, _ = env
    sp.results["hola"] = [T(1), T(2, "Sin Letra"), T(3)]
    ly.missing = {"Sin Letra"}
    page = type_query(win, "hola")
    assert until(lambda: page.list.count() == 3 and all(page.list.item(i).data(KIND_ROLE) for i in range(3)))
    assert [page.list.item(i).data(KIND_ROLE) for i in range(3)] == ["line", "none", "line"]
    assert page.stack.currentWidget() is page.list
    assert until(lambda: len(page._thumbs) == 3)  # portadas descargadas en segundo plano
    shot = page.list.viewport().grab().toImage()  # fuerza el dibujo de las filas (una excepción aquí rompe el test)
    assert shot.width() > 100 and len({shot.pixel(x, 30) for x in range(0, shot.width(), 7)}) > 3
    page.list.itemClicked.emit(page.list.item(0))  # elegir el resultado
    assert until(lambda: win.editor.project is not None)
    assert win.editor.project.title == "Canción 1" and win.pages.currentIndex() == 3 and win.editor.list.count() == 3


def test_lyrics_lookup_carries_the_spotify_id_and_the_editor_shows_the_credit_as_plain_text(env):
    win, sp, ly, _ = env
    sp.results["hola"] = [T(1)]
    page = type_query(win, "hola")
    assert until(lambda: page.list.count() == 1 and ly.queries)
    assert ly.queries[0].spotify_id == "id1"  # Spicy Lyrics pide por id de pista, no por título
    page.list.itemClicked.emit(page.list.item(0))
    assert until(lambda: win.editor.project is not None)
    credit = win.editor.credit
    assert credit.isVisibleTo(win.editor) and credit.textFormat() == Qt.PlainText  # sin interpretar el <b>
    assert credit.text() == f"Written by Ana, Beto\nProvided by {ly.CREDIT}"
    win.load_demo()
    assert not win.editor.credit.isVisibleTo(win.editor)  # una letra propia no lleva crédito de terceros


def test_debounce_waits_and_stale_results_are_dropped(env):
    win, sp, _, _ = env
    sp.results.update({"lento": [T(1, "VIEJO")], "rapido": [T(2, "NUEVO")]})
    sp.delays["lento"] = 0.4
    page = search_page(win)
    page._timer.setInterval(120)
    page.input.setText("rap")
    page.input.setText("rapido")
    assert not sp.searched  # aún dentro del debounce
    assert until(lambda: sp.searched)  # la búsqueda corre en un hilo: hay que bombear eventos para que avance (ver pump)
    pump(200)  # y ninguna otra debe salir después
    assert sp.searched == ["rapido"]  # una sola consulta, la última
    page.input.setText("lento")
    page._search_now()
    page.input.setText("rapido")
    page._search_now()
    assert until(lambda: page.list.count() == 1 and "lento" in sp.searched)  # la lenta ya salió y sigue dormida
    pump(600)  # su respuesta llega después y no debe pisar a la nueva
    assert page.list.item(0).data(TRACK_ROLE).title == "NUEVO"


def test_search_empty_and_cleared_states(env):
    win, sp, _, _ = env
    page = type_query(win, "nada")
    assert until(lambda: page.stack.currentWidget() is page.none)
    sp.results["x"] = [T(1)]
    type_query(win, "x")
    assert until(lambda: page.list.count() == 1)
    page.input.setText("")
    assert page.list.count() == 0 and page.stack.currentWidget() is page.hint


def test_search_error_is_reported_without_losing_the_page(env):
    win, sp, _, _ = env
    sp.search = lambda q: (_ for _ in ()).throw(ProviderError("Spotify pide esperar 5 segundos."))
    type_query(win, "boom")
    assert until(lambda: win.toasts.count() == 1)


def test_not_connected_guides_to_settings(make_window, qapp, tmp_path):
    services = Services(QSettings(str(tmp_path / "n.ini"), QSettings.IniFormat), spotify=FakeSpotify(connected=False))
    win = make_window(services=services)
    win.show()
    page = search_page(win)
    assert page.stack.currentWidget() is page.off and win.pages.widget(0).stack.currentWidget() is win.pages.widget(0).off
    btn = next(b for b in page.off.findChildren(QPushButton) if b.text() == "Ir a Ajustes")
    btn.click()
    assert win.pages.currentIndex() == 5 and win.sidebar.current() == 5


def test_home_follows_what_is_playing(env):
    win, sp, ly, services = env
    home = win.pages.widget(0)
    assert home.stack.currentWidget() is home.idle
    sp.playing = Playback(T(7, "La que suena"), 5000, True, 1700000000000)
    services.poll()
    assert until(lambda: home.stack.currentWidget() is home.card)
    assert home.card.title.text() == "La que suena" and home.card.state.text() == "SONANDO AHORA"
    assert until(lambda: home.card.badge.text() == "Letra por línea" and home.card._has_cover)
    assert win.sidebar.player.title.text().startswith("La que suena") and win.sidebar.player.playing
    sp.playing = Playback(T(7, "La que suena"), 9000, False, 1700000001000)
    services._polling = False
    services.poll()
    assert until(lambda: home.card.state.text() == "EN PAUSA")
    sp.playing = None
    services._polling = False
    services.poll()
    assert until(lambda: home.stack.currentWidget() is home.idle)
    sp.playing = Playback(T(8, "Sin Letra"), 0, True, 1)
    ly.missing = {"Sin Letra"}
    services._polling = False
    services.poll()
    assert until(lambda: home.card.badge.text() == "Sin letra sincronizada")
    assert not home.card.btn.isEnabled()  # sin letra no se puede crear el clip


def test_mini_player_buttons_control_spotify_and_follow_its_state(env):
    win, sp, _, services = env
    player = win.sidebar.player
    assert not (player.prev.isEnabled() or player.play.isEnabled() or player.next.isEnabled())  # nada sonando
    sp.playing = Playback(T(7, "La que suena"), 5000, True, 1)
    services.poll()
    assert until(lambda: player.has_track and player.playing)

    QTest.mouseClick(player.play, Qt.LeftButton)  # suena -> pausa
    assert until(lambda: sp.commands == ["pause"])
    sp.playing = Playback(T(7, "La que suena"), 5000, False, 2)  # lo que Spotify reportará tras el comando
    assert until(lambda: not player.playing)  # el mini-player se refresca solo, sin esperar al sondeo de 5 s

    QTest.mouseClick(player.play, Qt.LeftButton)  # en pausa -> reanuda
    assert until(lambda: sp.commands == ["pause", "play"])
    QTest.mouseClick(player.next, Qt.LeftButton)
    assert until(lambda: sp.commands[-1] == "next")
    QTest.mouseClick(player.prev, Qt.LeftButton)
    assert until(lambda: sp.commands[-1] == "previous")


def test_mini_player_command_failure_is_reported_and_does_not_block_the_next_one(env):
    win, sp, _, services = env
    sp.playing = Playback(T(7), 5000, True, 1)
    services.poll()
    assert until(lambda: win.sidebar.player.has_track)
    sp.control_error = "Controlar la reproducción requiere Spotify Premium."
    before = win.toasts.count()
    QTest.mouseClick(win.sidebar.player.next, Qt.LeftButton)
    assert until(lambda: win.toasts.count() == before + 1)
    sp.control_error = None
    QTest.mouseClick(win.sidebar.player.next, Qt.LeftButton)
    assert until(lambda: sp.commands == ["next"])


def test_mini_player_controls_hide_when_the_sidebar_collapses(env):
    win, *_ = env
    assert not win.sidebar.player.controls.isHidden()
    win.sidebar.set_collapsed(True, animate=False)
    assert win.sidebar.player.controls.isHidden()
    win.sidebar.set_collapsed(False, animate=False)
    assert not win.sidebar.player.controls.isHidden()


def test_home_create_clip_opens_editor(env):
    win, sp, _, services = env
    sp.playing = Playback(T(5, "Para Clip"), 0, True, 1)
    services.poll()
    home = win.pages.widget(0)
    assert until(lambda: home.card.btn.isEnabled() and home.card.track)
    home.card.btn.click()
    assert until(lambda: win.editor.project is not None)
    assert win.editor.project.title == "Para Clip" and win.editor.project.artist == "Artista"


def test_track_without_lyrics_or_broken_provider_shows_a_clear_error(env):
    win, _, ly, _ = env
    ly.missing, ly.broken = {"Muda"}, {"Rota"}
    win.open_track(T(1, "Muda"))
    assert until(lambda: win.toasts.count() == 2)  # «Preparando…» + el error
    win.open_track(T(2, "Rota"))
    assert until(lambda: win.toasts.count() == 4)
    assert win.editor.project is None


def test_cover_failure_falls_back_to_placeholder(env):
    win, sp, _, services = env
    sp.cover = lambda url: (_ for _ in ()).throw(ProviderError("No pude descargar la portada."))
    win.open_track(T(1, "Sin Portada"))
    assert until(lambda: win.editor.project is not None)
    assert win.editor.project.cover is not None


def test_settings_connect_flow_and_errors(make_window, qapp, tmp_path):
    sp = FakeSpotify(connected=False)
    services = Services(QSettings(str(tmp_path / "c.ini"), QSettings.IniFormat), spotify=sp)
    win = make_window(services=services)
    win.show()
    body = win.pages.widget(5).findChild(SettingsBody)
    assert body.status.text() == "Sin conectar" and not body.btn_disconnect.isEnabled()
    body.btn_connect.click()  # sin Client ID
    assert win.toasts.count() == 1 and not body._waiting
    body.client_id.setText("mi-client-id")
    sp.authorize_error = "Spotify no autorizó la conexión."
    services.spotify = sp  # el cliente real se reconstruye con el ID; aquí se conserva el falso
    services._make_client = lambda: None
    body.btn_connect.click()
    assert until(lambda: win.toasts.count() >= 3 and not body._waiting)  # aviso de inicio + error
    sp.authorize_error = None
    body.btn_connect.click()
    assert until(lambda: body.status.text() == "Conectado")
    assert services.settings.value("spotify/client_id") == "mi-client-id"
    assert win.sidebar.spotify.dot.ok and body.btn_disconnect.isEnabled() and not body.btn_connect.isEnabled()
    body.btn_disconnect.click()
    assert body.status.text() == "Sin conectar" and not win.sidebar.spotify.dot.ok and not sp.connected


def test_poll_failure_warns_once_per_streak(env):
    win, sp, _, services = env
    sp.fail = "Spotify no respondió."
    for _ in range(3):
        services._polling = False
        services.poll()
        assert until(lambda: not services._polling)  # el sondeo terminó (el callback baja la bandera)
    assert win.toasts.count() == 1  # un aviso, no uno cada 5 s
    sp.fail = None
    services._polling = False
    services.poll()
    assert until(lambda: not services._polling and not services._failing)  # terminó bien: la racha se reinicia
    sp.fail = "otra vez"
    services._polling = False
    services.poll()
    assert until(lambda: win.toasts.count() == 2)  # una racha nueva sí avisa


def test_changing_client_id_drops_the_old_session(env):
    _, sp, _, services = env
    services.set_client_id("otro-id")
    assert not sp.connected  # el token era del Client ID anterior
