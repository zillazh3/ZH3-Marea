"""The FX config is the single source of the word FX tunables: changing it must reach the code that reads it."""
import pytest

from marea.wordfx import config
from marea.wordfx.envelope import duration_mode, sample_envelope


@pytest.fixture
def restore_fx():
    prev = config.FX
    yield
    config.set_fx(prev)


def test_set_fx_returns_previous_and_activates(restore_fx):
    before = config.FX
    prev = config.set_fx(config.with_fx(max_fx_per_line=1))
    assert prev is before and config.FX.max_fx_per_line == 1


def test_duration_mode_reads_config(restore_fx):
    assert duration_mode(0.1) == ("accent", config.FX.accent_amp)
    config.set_fx(config.with_fx(accent_amp=0.7))
    assert duration_mode(0.1) == ("accent", 0.7)


def test_envelope_cache_follows_anticipation_amp(restore_fx):
    kw = dict(is_hero=True, anticipation_s=0.05)
    t = 0.99  # inside the anticipation window of a word that starts at 1.0
    a = sample_envelope(t, 1.0, 1.6, **kw).motion_amp
    config.set_fx(config.with_fx(anticipation_amp=0.5))
    b = sample_envelope(t, 1.0, 1.6, **kw).motion_amp
    assert b > a
