"""Semantic Word FX controller and file watcher for the Editor."""
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject

from marea.async_util import run_async
from marea.audio import local
from marea.ui.editor._pkg import pkg_attr
from marea.ui.widgets import CoalescedAction


class WordFXWatcher(QObject):
    """Watch editable WordFX JSON files via QFileSystemWatcher with a timer-like interface."""

    def __init__(self, on_changed, parent: QObject | None = None):
        super().__init__(parent)
        self._active = False
        self._watcher = QFileSystemWatcher(self)
        self._watched_files: set[str] = set()
        self._watched_dirs: set[str] = set()
        self._debounce = CoalescedAction(150, self._emit_if_active, self)
        self._watcher.fileChanged.connect(self._on_fs_event)
        self._watcher.directoryChanged.connect(self._on_fs_event)
        self._on_changed = on_changed

    def isActive(self) -> bool:
        return self._active

    def start(self, paths: tuple[Path, ...] = ()):
        self._active = True
        if paths:
            self.set_paths(paths)

    def stop(self):
        self._active = False
        self._debounce.cancel()

    def set_paths(self, paths: tuple[Path, ...]):
        want_files = {str(p) for p in paths if p.exists()}
        want_dirs = {str(p.parent) for p in paths if p.parent.exists()}
        rem_files = sorted(self._watched_files - want_files)
        if rem_files:
            self._watcher.removePaths(rem_files)
        add_files = sorted(want_files - self._watched_files)
        if add_files:
            self._watcher.addPaths(add_files)
        rem_dirs = sorted(self._watched_dirs - want_dirs)
        if rem_dirs:
            self._watcher.removePaths(rem_dirs)
        add_dirs = sorted(want_dirs - self._watched_dirs)
        if add_dirs:
            self._watcher.addPaths(add_dirs)
        self._watched_files = want_files
        self._watched_dirs = want_dirs

    def _on_fs_event(self, _path: str):
        if self._active:
            self._debounce.schedule()

    def _emit_if_active(self):
        if self._active and self.parent() is not None:
            w = self.parent()
            if not hasattr(w, "isVisible") or w.isVisible():
                self._on_changed()


class WordFXController:
    """Coordinate WordFX classification, caching, off-thread audio stats, and live preview updates."""

    def __init__(self, editor):
        self.editor = editor
        self.watch = WordFXWatcher(editor._wordfx_sync, editor)
        self.state = None
        self.plan = None
        self.profile = None
        self._palette = (None, ())
        self._audio = (None, None)
        self._cached_base = None
        self._rev_seq = 0
        self._revs: dict[str, tuple[int, object]] = {}

    def _rev(self, slot: str, obj) -> int | None:
        if obj is None:
            self._revs.pop(slot, None)
            return None
        cur = self._revs.get(slot)
        if cur is not None and cur[1] is obj:
            return cur[0]
        self._rev_seq += 1
        self._revs[slot] = (self._rev_seq, obj)
        return self._rev_seq

    def reset_cached_base(self):
        self.state = None
        self._cached_base = None

    def _watched_paths(self) -> tuple[Path, ...]:
        from marea.wordfx import library, rules, songs
        from marea.wordfx.plan import track_key

        p = self.editor.project
        return (
            library.user_path(),
            library.BUNDLED,
            rules.user_path(),
            rules.BUNDLED,
            songs.path_for(track_key(p.lyrics, p.track.id if p.track else "")),
        )

    def _files_key(self) -> tuple:
        out = []
        for f in self._watched_paths():
            try:
                out.append(f.stat().st_mtime_ns)
            except OSError:
                out.append(None)
        return tuple(out)

    def _audio_stats(self):
        p = self.editor.project
        if p.audio_samples is None:
            return None
        key = self._rev("audio_samples", p.audio_samples)
        if self._audio[0] == key:
            return self._audio[1]
        self._audio = (key, None)
        samples, audio_file = p.audio_samples, p.audio_file

        def work():
            from marea.model.cinematic import analyze_audio
            from marea.wordfx import audiofeat

            stem = audiofeat.find_cached_stem(audio_file) if audio_file else None
            return audiofeat.analyze(samples, local.RATE, stem=stem, analysis=analyze_audio(samples, local.RATE))

        def done(stats):
            if self.editor.project is p and self._audio[0] == key:
                self._audio = (key, stats)
                self.sync()

        pkg_attr("run_async", run_async)(work, done, lambda _e: None)
        return None

    def sync(self, rebuild: bool = True):
        ed = self.editor
        on = ed.settings.value("experimental/wordfx", False, type=bool)
        for w in (ed.cb_fxdebug, ed.cb_fxbg, ed.fx_row, ed.fx_label):
            w.setVisible(on)
        p = ed.project
        if not on:
            self.watch.stop()
        stats = self._audio_stats() if on and p else None
        if on and p:
            self.watch.start(self._watched_paths())
        elif on:
            self.watch.start()
        state = (
            on,
            on and ed.cb_fxdebug.isChecked(),
            ed.sl_fx.value(),
            ed.cb_fxbg.isChecked(),
            self._rev("project", p),
            self._rev("lyrics", p.lyrics) if p else None,
            self._rev("cover", p.cover) if p else None,
            self._rev("audio_samples", p.audio_samples) if p else None,
            self._rev("stats", stats),
            self._files_key() if on and p else None,
            float(p.audio_offset_ms) if p else 0.0,
        )
        if p is None or (rebuild and state == self.state):
            return
        prev_state = self.state
        if not on:
            self.state = state
            self.plan = self.profile = self._cached_base = None
        elif (
            prev_state is not None
            and self.plan is not None
            and prev_state[0] == state[0]
            and prev_state[2:10] == state[2:10]
        ):
            if prev_state[10] != state[10]:
                from dataclasses import replace

                self.plan = replace(self.plan, audio_offset_ms=state[10])
            self.state = state
        else:
            from marea.model.cinematic import cached_audio_analysis
            from marea.wordfx import library, rules, songs
            from marea.wordfx.matcher import match_lyrics
            from marea.wordfx.palette import dominant_colors
            from marea.wordfx.plan import build_plan, track_key
            from marea.wordfx.profile import ProfileError, classify

            cover_rev = state[6]
            if self._palette[0] != cover_rev:
                self._palette = (cover_rev, dominant_colors(p.cover))
            try:
                analysis = cached_audio_analysis(p.audio_samples, local.RATE) if p.audio_samples is not None else None
                key = track_key(p.lyrics, p.track.id if p.track else "")
                base_key = (state[5], key, state[9], state[8])
                if self._cached_base is not None and self._cached_base[0] == base_key:
                    _, lib, rls, raw_matches, profile = self._cached_base
                else:
                    lib = library.load()
                    rls = rules.load()
                    raw_matches = match_lyrics(p.lyrics, lib, rules=rls)
                    profile = songs.load_profile(key) or classify(p.lyrics, lib, stats, track_id=key)
                    self._cached_base = (base_key, lib, rls, raw_matches, profile)
                self.plan = build_plan(
                    p.lyrics,
                    lib,
                    intensity=ed.sl_fx.value() / 100,
                    track_id=p.track.id if p.track else "",
                    palette=self._palette[1],
                    analysis=analysis,
                    profile=profile,
                    audio_stats=stats,
                    adapt_bg=ed.cb_fxbg.isChecked(),
                    rules=rls,
                    matches=raw_matches,
                    audio_offset_ms=float(p.audio_offset_ms),
                )
                self.profile = profile
                self.state = state
            except (library.LibraryError, rules.RulesError, songs.SongError, ProfileError) as err:
                self._cached_base = None
                ed.notify(f"Efectos por palabra: {err} Se mantiene el plan anterior.", "error")
        ed.preview.set_wordfx(self.plan, state[1], rebuild=rebuild)

    def open_gallery(self):
        """Debug window: one forced-FX sample word in a loop with live tuning sliders (modal, so the editor never shows its values)."""
        from marea.ui.wordfx_gallery import WordFxGallery
        from marea.wordfx import library

        try:
            dlg = WordFxGallery(library.load(), self.editor)
        except library.LibraryError as err:
            self.editor.notify(f"Biblioteca de efectos no válida: {err}", "error")
            return
        dlg.exec()

    def open_profile_dialog(self):
        from PySide6.QtWidgets import QDialog

        from marea.ui.wordfx_profile import ProfileDialog
        from marea.wordfx import library, songs
        from marea.wordfx.plan import track_key
        from marea.wordfx.profile import ProfileError, classify

        ed = self.editor
        p = ed.project
        if p is None:
            return
        self.sync()
        if self.profile is None:
            ed.notify("Activa los efectos por palabra en Ajustes para editar el perfil.", "info")
            return
        key = track_key(p.lyrics, p.track.id if p.track else "")
        stats = self._audio[1] if p.audio_samples is not None else None
        try:
            lib = library.load()
        except library.LibraryError as err:
            ed.notify(f"Biblioteca de efectos no válida: {err}", "error")
            return
        end = max((l.end for l in p.lyrics.lines), default=1000) / 1000
        arc_end = self.profile.arc[-1].end if self.profile.arc else 0.0
        dlg = ProfileDialog(
            self.profile,
            max(end, arc_end),
            lambda notes: classify(p.lyrics, lib, stats, notes=notes, track_id=key),
            p.title,
            ed,
        )
        if dlg.exec() == QDialog.Accepted:
            try:
                songs.save_profile(key, dlg.result_profile())
            except (songs.SongError, ProfileError, OSError) as err:
                ed.notify(f"No pude guardar el perfil: {err}", "error")
                return
            self.reset_cached_base()
            self.sync()
