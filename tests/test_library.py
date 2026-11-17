import json

from PySide6.QtCore import Qt

from marea import library
from marea.project import demo_project, save_project
from marea.ui.library import LibraryPage


def test_registry_add_dedupe_remove_and_missing_files(tmp_path):
    idx = tmp_path / "library.json"
    a, b = tmp_path / "a.marea.json", tmp_path / "b.marea.json"
    a.write_text("{}")
    b.write_text("{}")
    library.add({"path": str(a), "title": "A"}, idx)
    library.add({"path": str(b), "title": "B"}, idx)
    items = library.add({"path": str(a), "title": "A2"}, idx)
    assert [e["title"] for e in items] == ["A2", "B"]  # deduped, newest first
    b.unlink()
    assert [e["title"] for e in library.load(idx)] == ["A2"]  # missing files are dropped
    library.remove(str(a), idx)
    assert library.load(idx) == []
    assert a.exists()  # the registry never deletes the project file


def test_registry_caps_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "MAX_ENTRIES", 3)
    idx = tmp_path / "library.json"
    for i in range(5):
        f = tmp_path / f"{i}.marea.json"
        f.write_text("{}")
        library.add({"path": str(f), "title": str(i)}, idx)
    assert [e["title"] for e in library.load(idx)] == ["4", "3", "2"]


def test_registry_survives_corrupt_index(tmp_path):
    idx = tmp_path / "library.json"
    idx.write_text("{not json")
    assert library.load(idx) == []


def test_register_project_writes_metadata_and_thumb(tmp_path):
    p = demo_project()
    out = save_project(p, tmp_path / "demo.marea.json")
    idx = tmp_path / "library.json"
    entry = library.register(p, out, idx, tmp_path / "thumbs")[0]
    assert entry["title"] == p.title and entry["verses"] == len(p.lyrics.lines) and entry["kind"] in ("syllable", "line")
    assert json.loads(idx.read_text(encoding="utf-8"))[0]["path"] == entry["path"]
    assert entry["thumb"] and (tmp_path / "thumbs").glob("proj-*.png")


def test_library_page_lists_filters_and_opens(qapp, tmp_path, monkeypatch):
    p = demo_project()
    out = save_project(p, tmp_path / "demo.marea.json")
    idx = tmp_path / "library.json"
    library.register(p, out, idx, tmp_path / "thumbs")
    monkeypatch.setattr(library, "_file", lambda: idx)
    monkeypatch.setattr("marea.ui.library.history.load", lambda: [{"path": str(out), "title": "Clip", "date": "2000-01-01T00:00:00"}])
    opened = []
    page = LibraryPage(opened.append)
    assert page.list.count() == 2 and page.stack.currentWidget() is page.list
    page.set_filter("project")
    assert page.list.count() == 1 and page.list.item(0).data(Qt.UserRole)[0] == "project"
    page.set_filter("clip")
    assert page.list.count() == 1 and page.list.item(0).data(Qt.UserRole)[0] == "clip"
    page.set_filter("project")
    page._open(page.list.item(0))
    assert opened == [str(out.resolve())]
    library.remove(str(out.resolve()), idx)
    page.reload()
    assert page.stack.currentWidget() is page.empty_filtered
    page.set_filter("all")
    assert page.list.count() == 1  # the clip from history remains
