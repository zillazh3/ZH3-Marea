"""Optional model installation without network access or heavy model imports."""
import os
import sys
import types
from pathlib import Path

import pytest

from marea.lab import env
from sidecar import stages

MODEL = env.KARAOKE_MODEL
CONFIG = Path(MODEL).stem + "_config.yaml"
YAML = "model:\n  dim: 512\ntraining:\n  instruments: [karaoke, other]\n"


@pytest.fixture
def download(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    calls = []
    behavior = {"error": None, "config": YAML}
    package = types.ModuleType("audio_separator")
    module = types.ModuleType("audio_separator.separator")

    class Separator:
        def __init__(self, model_file_dir, **kwargs):
            self.directory = Path(model_file_dir)

        def download_model_and_data(self, model):
            calls.append(self.directory)
            (self.directory / model).write_bytes(b"complete test weights")
            assert not env.separator_model_ready(tmp_path, model)
            if behavior["error"]:
                raise behavior["error"]
            (self.directory / CONFIG).write_text(behavior["config"], encoding="utf-8")
            (self.directory / "download_checks.json").write_text("{}", encoding="utf-8")

    module.Separator = Separator
    monkeypatch.setitem(sys.modules, "audio_separator", package)
    monkeypatch.setitem(sys.modules, "audio_separator.separator", module)
    ctx = stages.Ctx("download", lambda **event: None, tmp_path / "cache", set())
    return ctx, behavior, calls


def test_weights_are_published_last(download, monkeypatch):
    ctx, _, _ = download
    promoted = []
    replace = os.replace

    def record(source, target):
        assert not env.separator_model_ready(ctx.cache_dir.parent, MODEL)
        promoted.append(Path(target).name)
        replace(source, target)

    monkeypatch.setattr(os, "replace", record)
    stages.fetch_separator(ctx, MODEL)
    assert promoted == ["download_checks.json", CONFIG, MODEL]


def test_missing_optional_package_is_actionable(download, monkeypatch):
    ctx, _, calls = download
    monkeypatch.setitem(sys.modules, "audio_separator.separator", None)
    with pytest.raises(stages.StageError, match="optional audio-separator"):
        stages.fetch_separator(ctx, MODEL)
    assert not calls and not env.separator_model_ready(ctx.cache_dir.parent, MODEL)


def test_model_directory_override_cannot_bypass_staging(download, monkeypatch):
    ctx, _, calls = download
    monkeypatch.setenv("AUDIO_SEPARATOR_MODEL_DIR", str(ctx.cache_dir.parent / "elsewhere"))
    with pytest.raises(stages.StageError, match="AUDIO_SEPARATOR_MODEL_DIR"):
        stages.fetch_separator(ctx, MODEL)
    assert not calls


def test_only_complete_staged_downloads_become_ready(download):
    ctx, _, calls = download
    result = stages.fetch_separator(ctx, MODEL)
    root = ctx.cache_dir.parent
    assert calls[0] != root / "separator-models"
    assert env.separator_model_ready(root, MODEL)
    assert {MODEL, CONFIG} <= set(result["files"])


@pytest.mark.parametrize("error", [RuntimeError("interrupted"), stages.Cancelled()])
def test_interruption_keeps_partial_downloads_unavailable_and_retry_works(download, error):
    ctx, behavior, _ = download
    behavior["error"] = error
    with pytest.raises(stages.Cancelled if isinstance(error, stages.Cancelled) else stages.StageError):
        stages.fetch_separator(ctx, MODEL)
    assert not env.separator_model_ready(ctx.cache_dir.parent, MODEL)
    assert not (stages.models_dir(ctx) / MODEL).exists()
    behavior["error"] = None
    stages.fetch_separator(ctx, MODEL)
    assert env.separator_model_ready(ctx.cache_dir.parent, MODEL)


def test_invalid_configuration_is_not_published(download):
    ctx, behavior, _ = download
    behavior["config"] = "model: [unterminated"
    with pytest.raises(stages.StageError):
        stages.fetch_separator(ctx, MODEL)
    assert not env.separator_model_ready(ctx.cache_dir.parent, MODEL)


def test_existing_valid_assets_are_not_replaced(download):
    ctx, _, calls = download
    directory = stages.models_dir(ctx)
    directory.mkdir(parents=True)
    (directory / MODEL).write_bytes(b"original weights")
    (directory / CONFIG).write_text(YAML, encoding="utf-8")
    stages.fetch_separator(ctx, MODEL)
    assert not calls
    assert (directory / MODEL).read_bytes() == b"original weights"


def test_readiness_requires_configuration_and_no_pending_download(tmp_path):
    directory = tmp_path / "separator-models"
    directory.mkdir()
    (directory / MODEL).write_bytes(b"weights")
    assert not env.separator_model_ready(tmp_path, MODEL)
    (directory / CONFIG).write_text(YAML, encoding="utf-8")
    assert env.separator_model_ready(tmp_path, MODEL)
    (directory / f".{MODEL}.downloading").touch()
    assert not env.separator_model_ready(tmp_path, MODEL)


def test_offline_worker_cannot_download(download, monkeypatch):
    ctx, _, calls = download
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(stages.StageError, match="sin red"):
        stages.fetch_separator(ctx, MODEL)
    assert not calls


def test_model_filename_cannot_escape_the_model_directory(download):
    ctx, _, _ = download
    with pytest.raises(stages.StageError):
        stages.fetch_separator(ctx, "../outside.ckpt")
