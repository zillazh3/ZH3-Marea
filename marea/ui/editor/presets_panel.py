"""Corrientes en el Editor: elegir, aplicar (completa o por secciones), guardar, actualizar, importar y exportar looks."""
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QListView,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QWidget)

from marea.async_util import run_async
from marea.presets.schema import SECTION_LABELS, SECTIONS, PresetError, apply, changed_sections, current_values
from marea.presets.store import PresetStore
from marea.presets.thumbs import ensure_thumb
from marea.ui.editor._pkg import pkg_attr
from marea.ui.editor.wordfx_controller import WordFXWatcher
from marea.ui.widgets import make_button

THUMB_ICON = QSize(72, 128)


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
        title = QLabel("Corrientes")
        title.setObjectName("H2")
        form.addRow(title)
        self.list = QListWidget()
        self.list.setViewMode(QListView.IconMode)
        self.list.setFlow(QListView.LeftToRight)
        self.list.setWrapping(False)
        self.list.setMovement(QListView.Static)
        self.list.setIconSize(THUMB_ICON)
        self.list.setFixedHeight(THUMB_ICON.height() + 52)
        self.list.setHorizontalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setSelectionMode(QListWidget.SingleSelection)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.itemDoubleClicked.connect(lambda _i: self.apply_selected())
        form.addRow(self.list)

        row = QHBoxLayout()
        self.btn_apply = make_button("Aplicar", "Primary", self.apply_selected)
        self.btn_save = make_button("Guardar como…", "Ghost", self.save_as, "Guarda el look actual como una Corriente nueva")
        self.btn_update = make_button("Actualizar", "Ghost", self.update_selected, "Reemplaza la Corriente elegida con el look actual")
        self.btn_more = make_button("⋯", "Ghost", tooltip="Renombrar, duplicar, eliminar, importar y exportar")
        menu = QMenu(self.btn_more)
        self.act_rename = menu.addAction("Renombrar…", self.rename_selected)
        self.act_dup = menu.addAction("Duplicar", self.duplicate_selected)
        self.act_del = menu.addAction("Eliminar", self.delete_selected)
        menu.addSeparator()
        menu.addAction("Importar .json…", self.import_file)
        self.act_export = menu.addAction("Exportar .json…", self.export_selected)
        self.btn_more.setMenu(menu)
        for b in (self.btn_apply, self.btn_save, self.btn_update, self.btn_more):
            row.addWidget(b)
        form.addRow(row)

        grid = QGridLayout()
        self.cb_sections = {}
        for i, sec in enumerate(SECTIONS):
            cb = QCheckBox(SECTION_LABELS[sec])
            cb.setChecked(True)
            cb.setToolTip("Marca solo lo que quieres que cambie al aplicar")
            self.cb_sections[sec] = cb
            grid.addWidget(cb, i // 3, i % 3)
        holder = QWidget()
        holder.setLayout(grid)
        grid.setContentsMargins(0, 0, 0, 0)
        form.addRow(holder)
        self.lb_state = QLabel("")
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
            it.setSizeHint(QSize(THUMB_ICON.width() + 24, THUMB_ICON.height() + 40))
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
                    it.setIcon(QIcon(QPixmap(str(path))))

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
            self.ed.notify(f"No pude guardar la Corriente: {e}", "error")
        return None

    def save_as(self):
        name, ok = QInputDialog.getText(self.ed, "Guardar Corriente", "Nombre de la Corriente:")
        if not ok:
            return
        info = self._report(self.store.save, name, self._capture())
        if info:
            self.applied = info.name
            self.refresh_list()
            self._select(info.name)
            self.ed.notify(f"Corriente «{info.name}» guardada.", "ok")

    def update_selected(self):
        info = self.selected()
        if info is None or info.builtin:
            return
        if self._report(self.store.update, info.name, self._capture()):
            self.applied = info.name
            self.refresh_list()
            self.ed.notify(f"Corriente «{info.name}» actualizada.", "ok")

    def rename_selected(self):
        info = self.selected()
        if info is None or info.builtin:
            return
        name, ok = QInputDialog.getText(self.ed, "Renombrar Corriente", "Nuevo nombre:", text=info.name)
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
        if QMessageBox.question(self.ed, "Eliminar Corriente", f"¿Eliminar «{info.name}»? No se puede deshacer.") != QMessageBox.Yes:
            return
        self._report(self.store.delete, info.name)
        if self.applied == info.name:
            self.applied = ""
        self.refresh_list()

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self.ed, "Importar Corriente", "", "Corrientes (*.json);;Todos los archivos (*.*)")
        if not path:
            return
        info = self._report(self.store.import_file, Path(path), self._fonts())
        if info:
            self.refresh_list()
            self._select(info.name)
            self.ed.notify(" ".join([*info.flags, f"Corriente «{info.name}» importada."]), "info" if info.flags else "ok")

    def export_selected(self):
        info = self.selected()
        if info is None:
            return
        path, _ = QFileDialog.getSaveFileName(self.ed, "Exportar Corriente", f"{info.name}.json", "Corrientes (*.json)")
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
            self.lb_state.setText("" if not self.applied else f"Última Corriente: {self.applied}")
            return
        diff = changed_sections(info.preset, current_values(*ed.look()))
        self.lb_state.setText(f"{info.name} · modificado ({', '.join(SECTION_LABELS[s].lower() for s in diff)})" if diff
                              else f"{info.name} · aplicada")
