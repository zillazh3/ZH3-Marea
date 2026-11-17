"""Corrientes en el Editor: elegir, aplicar (completa o por secciones), guardar, actualizar, importar y exportar looks."""
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QListView,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QWidget, QWidgetAction)

from marea.async_util import run_async
from marea.presets.schema import SECTIONS, PresetError, apply, changed_sections, current_values
from marea.presets.store import PresetStore
from marea.presets.thumbs import ensure_thumb
from marea.ui.editor._pkg import pkg_attr
from marea.ui import lang, theme
from marea.ui.editor.wordfx_controller import WordFXWatcher
from marea.ui.widgets import make_button

SECTION_KEYS = {  # preset section -> i18n key
    "tipografia": "presetsPanel.section.typography",
    "color": "presetsPanel.section.color",
    "fondo": "presetsPanel.section.background",
    "efectos": "presetsPanel.section.effects",
    "marca_de_agua": "presetsPanel.section.watermark",
}
assert set(SECTION_KEYS) == set(SECTIONS)
THUMB_ICON = QSize(60, 75)  # recorte 4:5 del centro del cuadro 9:16: se ve el verso, no el teléfono entero
THUMB_CELL = QSize(THUMB_ICON.width() + 16, THUMB_ICON.height() + 34)


def crop_thumb(px: QPixmap) -> QPixmap:
    """Recorta el cuadro vertical a 4:5 centrado."""
    h = round(px.width() * THUMB_ICON.height() / THUMB_ICON.width())
    return px.copy(0, max(0, (px.height() - h) // 2), px.width(), h)


class PresetsController:
    """Panel «Corrientes» al inicio de la pestaña Estilo."""

    def __init__(self, editor, store: PresetStore | None = None):
        self.ed = editor
        self.store = store or PresetStore()
        self.applied: str = ""  # nombre de la última Corriente aplicada (se guarda en el proyecto)
        self._infos = {}
        self._thumb_gen = 0

    # ---------------- construcción ----------------
    def build(self, form: QFormLayout):
        ed = self.ed
        title = QLabel()
        lang.bind(title, "presetsPanel.title")
        title.setObjectName("H2")
        form.addRow(title)
        self.list = QListWidget()
        self.list.setObjectName("Presets")
        self.list.setViewMode(QListView.IconMode)
        self.list.setFlow(QListView.LeftToRight)
        self.list.setWrapping(False)
        self.list.setMovement(QListView.Static)
        self.list.setIconSize(THUMB_ICON)
        self.list.setSpacing(theme.SPACE[1])
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.setUniformItemSizes(True)
        self.list.setFixedHeight(THUMB_CELL.height() + 2 * theme.SPACE[1] + 14)  # + scrollbar horizontal
        self.list.setHorizontalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setSelectionMode(QListWidget.SingleSelection)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.itemDoubleClicked.connect(lambda _i: self.apply_selected())
        form.addRow(self.list)

        row = QHBoxLayout()
        self.btn_apply = make_button("", "Primary", self.apply_selected)
        lang.bind(self.btn_apply, "presetsPanel.apply")
        self.btn_save = make_button("", "Ghost", self.save_as)
        lang.bind(self.btn_save, "presetsPanel.saveAs")
        lang.bind(self.btn_save, "presetsPanel.saveAsTip", "setToolTip")
        self.btn_update = make_button("", "Ghost", self.update_selected)
        lang.bind(self.btn_update, "presetsPanel.update")
        lang.bind(self.btn_update, "presetsPanel.updateTip", "setToolTip")
        self.btn_more = make_button("", "Ghost")
        lang.bind(self.btn_more, "presetsPanel.moreTip", "setToolTip")
        self._more_icon()
        theme.signals.changed.connect(self._more_icon)
        menu = QMenu(self.btn_more)
        self.cb_sections = {}
        header = menu.addAction("")
        lang.bind(header, "presetsPanel.applyOnly")
        header.setEnabled(False)
        for sec in SECTIONS:  # QWidgetAction: el menú sigue abierto mientras se marcan varias secciones
            cb = QCheckBox()
            lang.bind(cb, SECTION_KEYS[sec])
            cb.setChecked(True)
            cb.toggled.connect(self._sections_changed)
            holder = QWidget()
            QHBoxLayout(holder).addWidget(cb)
            holder.layout().setContentsMargins(theme.SPACE[3], theme.SPACE[1], theme.SPACE[3], theme.SPACE[1])
            wa = QWidgetAction(menu)
            wa.setDefaultWidget(holder)
            menu.addAction(wa)
            self.cb_sections[sec] = cb
        menu.addSeparator()
        self.act_rename = menu.addAction("", self.rename_selected)
        lang.bind(self.act_rename, "presetsPanel.rename")
        self.act_dup = menu.addAction("", self.duplicate_selected)
        lang.bind(self.act_dup, "presetsPanel.duplicate")
        self.act_del = menu.addAction("", self.delete_selected)
        lang.bind(self.act_del, "presetsPanel.delete")
        menu.addSeparator()
        act_import = menu.addAction("", self.import_file)
        lang.bind(act_import, "presetsPanel.import")
        self.act_export = menu.addAction("", self.export_selected)
        lang.bind(self.act_export, "presetsPanel.export")
        self.btn_more.setMenu(menu)
        for b in (self.btn_apply, self.btn_save, self.btn_update, self.btn_more):
            row.addWidget(b)
        form.addRow(row)

        self.lb_state = QLabel()
        self.lb_state.setObjectName("Caption")
        self.lb_state.setWordWrap(True)
        form.addRow(self.lb_state)

        self._watch = WordFXWatcher(self.refresh_list, ed)
        self._modified_timer = QTimer(ed, interval=700)
        self._modified_timer.timeout.connect(self.update_modified)
        self._modified_timer.start()
        self.refresh_list()
        self._selection_changed()

    # ---------------- lista ----------------
    def _fonts(self) -> set[str]:
        return set(QFontDatabase.families())

    def refresh_list(self):
        keep = self.selected_name()
        self.store.user_dir.mkdir(parents=True, exist_ok=True)
        infos = self.store.list(self._fonts())
        self._infos = {i.name: i for i in infos}
        self.list.blockSignals(True)
        self.list.clear()
        for info in infos:
            it = QListWidgetItem(info.name + ("" if info.builtin else " ·"))
            it.setData(Qt.UserRole, info.name)
            it.setToolTip(info.preset.get("description", "") or info.name)
            it.setSizeHint(THUMB_CELL)
            self.list.addItem(it)
            if info.name == keep or (not keep and info.name == self.applied):
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        self._watch.start(tuple(self.store.files_to_watch()))
        self._thumb_gen += 1
        gen = self._thumb_gen
        for info in infos:
            self._load_thumb(info, gen)
        self._selection_changed()

    def _load_thumb(self, info, gen):
        def done(path):
            if gen != self._thumb_gen:
                return
            for i in range(self.list.count()):
                it = self.list.item(i)
                if it.data(Qt.UserRole) == info.name:
                    it.setIcon(QIcon(crop_thumb(QPixmap(str(path)))))

        pkg_attr("run_async", run_async)(lambda: ensure_thumb(info, self.store.user_dir / "thumbs"), done, lambda _e: None)

    def selected_name(self) -> str:
        it = self.list.currentItem() if hasattr(self, "list") else None
        return it.data(Qt.UserRole) if it else ""

    def selected(self):
        return self._infos.get(self.selected_name())

    def _selection_changed(self):
        info = self.selected()
        have = info is not None and self.ed.project is not None
        user = info is not None and not info.builtin
        self.btn_apply.setEnabled(have)
        self.btn_save.setEnabled(self.ed.project is not None)
        self.btn_update.setEnabled(user and self.ed.project is not None)
        self.act_rename.setEnabled(user)
        self.act_del.setEnabled(user)
        self.act_dup.setEnabled(info is not None)
        self.act_export.setEnabled(info is not None)
        self.update_modified()

    def sections(self) -> tuple[str, ...]:
        return tuple(s for s in SECTIONS if self.cb_sections[s].isChecked())

    def _more_icon(self):
        self.btn_more.setIcon(theme.icon("ellipsis", theme.current.text_muted, 16))

    def _sections_changed(self):
        n = len(self.sections())
        partial = n < len(SECTIONS)
        if partial:
            lang.bind(self.btn_apply, "presetsPanel.applyPartial", n=n, total=len(SECTIONS))
            lang.bind(self.btn_apply, "presetsPanel.applyPartialTip", "setToolTip")
        else:
            lang.bind(self.btn_apply, "presetsPanel.apply")
            lang.unbind(self.btn_apply, "setToolTip")
            self.btn_apply.setToolTip("")

    # ---------------- acciones ----------------
    def apply_selected(self):
        info, ed = self.selected(), self.ed
        if info is None or not ed.project:
            return
        style, params, bg, fx = apply(info.preset, *ed.look(), sections=self.sections())
        ed.set_look(style, params, bg, fx)
        self.applied = info.name
        if info.flags:
            ed.notify(" ".join(info.flags), "info")
        self.update_modified()

    def _capture(self):
        from marea.presets.schema import capture
        return capture(*self.ed.look())

    def _report(self, fn, *args):
        try:
            return fn(*args)
        except PresetError as e:
            self.ed.notify(str(e), "error")
        except OSError as e:
            self.ed.notify(lang.t("presetsPanel.saveFailed", error=e), "error")
        return None

    def save_as(self):
        name, ok = QInputDialog.getText(self.ed, lang.t("presetsPanel.saveTitle"), lang.t("presetsPanel.saveLabel"))
        if not ok:
            return
        info = self._report(self.store.save, name, self._capture())
        if info:
            self.applied = info.name
            self.refresh_list()
            self._select(info.name)
            self.ed.notify(lang.t("presetsPanel.saved", name=info.name), "ok")

    def update_selected(self):
        info = self.selected()
        if info is None or info.builtin:
            return
        if self._report(self.store.update, info.name, self._capture()):
            self.applied = info.name
            self.refresh_list()
            self.ed.notify(lang.t("presetsPanel.updated", name=info.name), "ok")

    def rename_selected(self):
        info = self.selected()
        if info is None or info.builtin:
            return
        name, ok = QInputDialog.getText(self.ed, lang.t("presetsPanel.renameTitle"), lang.t("presetsPanel.renameLabel"), text=info.name)
        if ok and self._report(self.store.rename, info.name, name):
            if self.applied == info.name:
                self.applied = name.strip()
            self.refresh_list()
            self._select(name.strip())

    def duplicate_selected(self):
        info = self.selected()
        if info is None:
            return
        new = self._report(self.store.duplicate, info.name)
        if new:
            self.refresh_list()
            self._select(new.name)

    def delete_selected(self):
        info = self.selected()
        if info is None or info.builtin:
            return
        if QMessageBox.question(self.ed, lang.t("presetsPanel.deleteTitle"), lang.t("presetsPanel.deleteConfirm", name=info.name)) != QMessageBox.Yes:
            return
        self._report(self.store.delete, info.name)
        if self.applied == info.name:
            self.applied = ""
        self.refresh_list()

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self.ed, lang.t("presetsPanel.importTitle"), "", lang.t("presetsPanel.importFilter"))
        if not path:
            return
        info = self._report(self.store.import_file, Path(path), self._fonts())
        if info:
            self.refresh_list()
            self._select(info.name)
            self.ed.notify(" ".join([*info.flags, lang.t("presetsPanel.imported", name=info.name)]), "info" if info.flags else "ok")

    def export_selected(self):
        info = self.selected()
        if info is None:
            return
        path, _ = QFileDialog.getSaveFileName(self.ed, lang.t("presetsPanel.exportTitle"), f"{info.name}.json", lang.t("presetsPanel.exportFilter"))
        if path:
            self._report(self.store.export_file, info.name, Path(path))

    def _select(self, name: str):
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == name:
                self.list.setCurrentRow(i)
                return

    # ---------------- indicador «modificado» ----------------
    def update_modified(self):
        ed = self.ed
        info = self._infos.get(self.applied)
        if ed.project is None or info is None:
            if self.applied:
                lang.bind(self.lb_state, "presetsPanel.lastApplied", name=self.applied)
            else:
                lang.unbind(self.lb_state)
                self.lb_state.setText("")
            return
        diff = changed_sections(info.preset, current_values(*ed.look()))
        if diff:
            lang.bind_fn(self.lb_state, "setText", lambda: self.lb_state.setText(lang.t(
                "presetsPanel.modified", name=info.name, sections=", ".join(lang.t(SECTION_KEYS[s]).lower() for s in diff))))
        else:
            lang.bind(self.lb_state, "presetsPanel.applied", name=info.name)
