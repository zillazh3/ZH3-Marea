"""Explicit model downloads and navigation preserve the open review."""
import pytest
from PySide6.QtWidgets import QLabel

from marea.lab import env
from marea.ui import lab
from tests.test_lab_ui import FakeRunner, aligned, panel, review


def ready_panel(panel, monkeypatch):
    p, notes, root = panel
    monkeypatch.setattr(env, "is_installed", lambda root: True)
    monkeypatch.setattr(env, "separation_installed", lambda root: True)
    runner = FakeRunner()
    created = []
    monkeypatch.setattr(lab, "Runner", lambda root, **kwargs: (created.append((root, kwargs)), runner)[1])
    p.refresh()
    return p, notes, root, runner, created


def test_model_row_downloads_only_on_click_into_configured_lab(panel, monkeypatch):
    p, _, root, runner, created = ready_panel(panel, monkeypatch)
    assert created == []
    labels = "\n".join(label.text() for label in p.findChildren(QLabel))
    assert "0.9 GB" in labels and "unresolved" in labels and str(root / "separator-models") in labels
    p.rows[env.KARAOKE_MODEL][1].click()
    assert created[0][0] == root and created[0][1]["online"] is True
    assert runner.calls == [("fetch_separator", {"model": env.KARAOKE_MODEL})]
    assert not p.rows[env.KARAOKE_MODEL][1].isEnabled()


def test_cancellation_before_worker_ready_unlocks_the_download(qapp, tmp_path, monkeypatch):
    runner = lab.Runner(tmp_path, online=True)
    events = []
    monkeypatch.setattr(runner, "_ensure_started", lambda: None)
    monkeypatch.setattr(runner.client, "stop", lambda: events.append("stopped"))
    runner.cancelled.connect(lambda: events.append("cancelled"))
    runner.ask("fetch_separator", model=env.KARAOKE_MODEL)
    runner.cancel()
    assert not runner.busy and events == ["stopped", "cancelled"]


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
def test_completion_refreshes_and_failure_or_cancellation_allows_retry(panel, monkeypatch, outcome):
    p, notes, _, runner, _ = ready_panel(panel, monkeypatch)
    state, button = p.rows[env.KARAOKE_MODEL]
    button.click()
    if outcome == "done":
        monkeypatch.setattr(env, "separator_model_ready", lambda *args: True)
        runner.deliver("fetch_separator", {})
        assert state.text() == "Listo" and not button.isEnabled()
    else:
        runner.busy = False
        if outcome == "failed":
            runner.failed.emit("fetch_separator", "connection interrupted")
            assert notes[-1] == ("connection interrupted", "error")
        else:
            p.cancel()
            assert runner.calls[-1][0] == "cancel"
            runner.cancelled.emit()
            assert notes[-1][1] == "info"
        assert button.isEnabled() and state.text() == "Pendiente"
        button.click()
        assert runner.calls[-1] == ("fetch_separator", {"model": env.KARAOKE_MODEL})


def test_optional_package_installation_precedes_model_request(panel, monkeypatch):
    p, _, root, runner, _ = ready_panel(panel, monkeypatch)
    installed, jobs = [], []
    monkeypatch.setattr(env, "separation_installed", lambda root: bool(installed))
    monkeypatch.setattr(env, "install_separation", lambda destination, progress, cancel: installed.append(destination))
    monkeypatch.setattr(lab, "run_async", lambda fn, ok, err: jobs.append((fn, ok)))
    p.rows[env.KARAOKE_MODEL][1].click()
    assert runner.calls == [] and len(jobs) == 1
    fn, ok = jobs.pop()
    ok(fn())
    assert installed == [root]
    assert runner.calls == [("fetch_separator", {"model": env.KARAOKE_MODEL})]


def test_model_settings_navigation_preserves_draft_and_session(review, qapp):
    rw, runner, _, win, _ = review
    aligned(rw, runner)
    session = rw.session
    rw.ed_text.setPlainText("Keep my corrected lyric draft")
    win.settings_body.cb_align.setChecked(True)
    rw.btn_layer_models.click()
    qapp.processEvents()
    assert win.pages.currentIndex() == 5
    assert rw.ed_text.toPlainText() == "Keep my corrected lyric draft" and rw.session is session
    assert rw.isVisible() and not rw.cb_layers.isChecked()
    panel = win.settings_body._lab_panel
    button = panel.rows[env.KARAOKE_MODEL][1]
    assert win.settings_scroll.viewport().rect().intersects(button.rect().translated(button.mapTo(win.settings_scroll.viewport(), button.rect().topLeft())))
    assert panel._runner is None
