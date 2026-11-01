"""Worker orchestration with synthetic emissions and no model downloads."""
from types import SimpleNamespace
import subprocess
import sys
import types

import numpy as np
import pytest

from sidecar import stages
from sidecar.stages import Ctx, StageError
from marea.lab.align_job import request_layers
from marea.lab.alignment import Alignment
from tests.test_vocal_layers import performance
from tests.test_lab_pipeline import VOCAB, emissions


@pytest.fixture
def worker_audio(tmp_path, monkeypatch):
    pcm, lp, eg = tmp_path / 'pcm.npy', tmp_path / 'lp.npy', tmp_path / 'energy.npy'
    np.save(pcm, np.zeros(16000 * 6, np.float32))
    np.save(lp, emissions(300, [((50, 55), 'a'), ((80, 85), 'b')]))
    np.save(eg, np.ones(300))
    monkeypatch.setattr(stages, 'decode', lambda *a: {'audio_sha256': 'abc', 'pcm_path': str(pcm), 'duration_ms': 6000})
    monkeypatch.setattr(stages, 'emissions', lambda *a: lp)
    monkeypatch.setattr(stages, 'energy', lambda *a: eg)
    from sidecar import models
    monkeypatch.setattr(models, 'load_vocab', lambda *a: (VOCAB, 0))
    return Ctx('r', lambda **kw: None, tmp_path / 'cache', set()), pcm


def test_adding_and_reordering_layers_cannot_change_other_boundaries(worker_audio):
    ctx, _ = worker_audio
    lead = {'id': 'L', 'words': [['a']], 'hint': [1000, 2200]}
    back = {'id': 'B', 'words': [['b']], 'hint': [1600, 2400], 'bg': True, 'parent': 'L'}
    def run(lines):
        result = stages.align(ctx, '', '', 'es', lines, device='cpu')
        return {l['id']: (l['start'], l['end'], l['words'][0]['syllables'][0]['start'],
                           l['words'][0]['syllables'][0]['end']) for l in result['lines']}
    alone = run([lead])
    assert run([lead, back])['L'] == alone['L']
    assert run([back, lead]) == run([lead, back])


def test_stem_routing_is_independent_of_classification(worker_audio, monkeypatch):
    ctx, pcm = worker_audio
    calls = []
    monkeypatch.setattr(stages, 'separate_lead_backing', lambda *a: (pcm, pcm))
    original = stages.emissions
    monkeypatch.setattr(stages, 'emissions', lambda *a: (calls.append(a[-1]), original(*a))[1])
    line = {'id': 'B', 'words': [['b']], 'hint': [1600, 2400], 'role': 'duet', 'source_stem': 'backing'}
    result = stages.align(ctx, '', '', 'es', [line], device='cpu', karaoke='local.ckpt', window=[1000, 3000])
    assert any(c.startswith('backing:') for c in calls)
    assert result['lines'][0]['source_stem'] == 'backing'
    assert result['karaoke'] == 'local.ckpt'


def test_missing_local_separator_models_fail_before_heavy_imports(tmp_path):
    ctx = Ctx('r', lambda **kw: None, tmp_path / 'cache', set())
    with pytest.raises(StageError, match='descargado'):
        stages.separate_lead_backing(ctx, 'sha', '', '', 'missing.ckpt', 'missing2.ckpt', {'device': 'cpu'})


@pytest.mark.parametrize('labels', [('Vocals', 'Instrumental'), ('Other', 'Other'), ('unknown', 'backing_only')])
def test_ambiguous_stem_labels_are_errors(labels):
    with pytest.raises(StageError, match='unambiguous'):
        stages.karaoke_stem_names(*labels)


def test_karaoke_target_and_residual_are_not_reversed():
    assert stages.karaoke_stem_names('karaoke', 'other') == {'karaoke': 'backing', 'other': 'lead'}
    assert stages.karaoke_stem_names('backing_only', 'lead_only') == {'backing_only': 'backing', 'lead_only': 'lead'}


def test_cancelled_alignment_does_not_publish_cache(worker_audio):
    ctx, _ = worker_audio
    ctx = Ctx('r', lambda **kw: None, ctx.cache_dir, {'r'})
    with pytest.raises(stages.Cancelled):
        stages.align(ctx, '', '', 'es', [{'id': 'L', 'words': [['a']], 'hint': [1000, 2200]}], device='cpu')
    assert not list(ctx.cache_dir.rglob('align-*.json'))


def test_suspected_leakage_remains_a_pending_candidate_without_inventing_an_agent(worker_audio, monkeypatch):
    ctx, pcm = worker_audio
    monkeypatch.setattr(stages, 'separate_lead_backing', lambda *a: (pcm, pcm))
    from sidecar import layers
    monkeypatch.setattr(layers, 'has_active_backing', lambda *a: True)
    monkeypatch.setattr(layers, 'stem_metrics', lambda *a: {'is_bleed': True})
    monkeypatch.setattr(layers, 'classify_layer', lambda **kw: ('backing', 0.9))
    whisper = types.ModuleType('faster_whisper')
    utils = types.ModuleType('faster_whisper.utils')
    utils.download_model = lambda *a, **kw: 'local'
    segment = SimpleNamespace(text='oh', start=1.0, end=2.0, no_speech_prob=0.9, avg_logprob=-2.0)
    class Model:
        def __init__(self, *a, **kw):
            pass
        def transcribe(self, *a, **kw):
            return iter([segment]), SimpleNamespace(language='es', language_probability=0.9)
    whisper.WhisperModel = Model
    monkeypatch.setitem(sys.modules, 'faster_whisper', whisper)
    monkeypatch.setitem(sys.modules, 'faster_whisper.utils', utils)
    result = stages.asr(ctx, '', '', device='cpu', karaoke='local.ckpt')
    assert len(result['lines']) == 2
    candidate = result['lines'][1]
    assert candidate['role'] == 'uncertain' and candidate['review_state'] == 'pending'
    assert candidate['source_stem'] == 'backing' and candidate['agent'] == ''


def test_inference_worker_blocks_network_without_importing_heavy_models():
    script = '''import os, sys, socket
os.environ['HF_HUB_OFFLINE'] = '1'
import sidecar.worker
assert not any(name in sys.modules for name in ('torch', 'faster_whisper', 'audio_separator'))
try:
    socket.socket().connect(('127.0.0.1', 9))
except OSError as error:
    assert 'disabled during local inference' in str(error)
else:
    raise AssertionError('network was not blocked')
print('offline without heavy models')
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'offline without heavy models'


def test_unalignable_import_keeps_its_own_boundaries_and_remains_approximate(worker_audio):
    ctx, _ = worker_audio
    line = performance('B', '123', 1000, 2500, bg=True, ref={'standalone': True})
    result = stages.align(ctx, '', '', 'es', request_layers(Alignment(6000, [line])), device='cpu')['lines'][0]
    assert (result['start'], result['end']) == (1000, 2500)
    assert result['approximate'] and result['words'][0]['syllables'][0]['approximate']


def test_alignment_cache_changes_with_stem_provenance(worker_audio):
    ctx, _ = worker_audio
    line = {'id': 'L', 'words': [['a']], 'hint': [1000, 2000]}
    stages.align(ctx, '', '', 'es', [line], device='cpu')
    stages.align(ctx, '', '', 'es', [{**line, 'source_stem': 'vocals'}], device='cpu')
    assert len(list(ctx.cache_dir.rglob('align-*.json'))) == 2


def test_imported_layers_acquire_actual_stem_provenance(worker_audio, monkeypatch):
    ctx, pcm = worker_audio
    monkeypatch.setattr(stages, 'separate_lead_backing', lambda *a: (pcm, pcm))
    lines = [{'id': 'L', 'words': [['a']], 'hint': [1000, 2000], 'source_stem': ''},
             {'id': 'B', 'words': [['b']], 'hint': [1500, 2500], 'bg': True, 'source_stem': ''}]
    result = stages.align(ctx, '', '', 'es', lines, device='cpu', karaoke='local.ckpt')
    assert [l['source_stem'] for l in result['lines']] == ['lead', 'backing']
    with pytest.raises(StageError, match='Enable local'):
        stages.align(ctx, '', '', 'es', [{'id': 'B', 'words': [['b']], 'source_stem': 'backing'}], device='cpu')
