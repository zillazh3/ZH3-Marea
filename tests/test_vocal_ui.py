"""Review integration for overlapping vocal performances."""
import numpy as np

from marea.lab import alignment as A
from marea.lab.session import Session
from marea.lab import env
from marea.ui import lab
from tests.test_lab_ui import aligned, review
from tests.test_vocal_layers import performance


def test_imported_backing_is_sent_as_a_separate_performance(review):
    rw, runner, _, _, _ = review
    params = aligned(rw, runner)
    assert any(line['bg'] for line in params['lines'])
    assert len(params['lines']) == len(rw.skeleton.lines)
    by_id = {b['id']: b for b in rw.timeline.blocks() if b['id']}
    main = next(l for l in rw.session.alignment.lines if not l.bg)
    back = next(l for l in rw.session.alignment.lines if l.bg)
    assert by_id[main.id]['lane'] != by_id[back.id]['lane']


def test_candidate_decisions_gate_export_and_do_not_lock_timing(review):
    rw, _, _, _, _ = review
    lines = [performance('L', 'a', 1000, 3000), performance('B', 'oh', 1500, 3500, bg=True,
             role='uncertain', source_stem='backing', review_state='pending', ref={'parent': 'L'})]
    rw.session = Session(A.Alignment(5000, lines))
    rw._populate()
    assert not rw.btn_approve.isEnabled()
    rw._select('B')
    rw.btn_accept.click()
    assert rw.btn_approve.isEnabled() and not rw.session.find('B')[1].manual_override
    rw.btn_reject.click()
    assert rw.session.find('B') is None
    rw.btn_undo.click()
    assert rw.session.find('B')


def test_section_recalculation_keeps_backing_stem_after_role_change(review):
    rw, runner, _, _, _ = review
    line = performance('B', 'oh', 1000, 3000, role='duet', source_stem='backing', review_state='pending')
    rw.session = Session(A.Alignment(5000, [line], language='es', pipeline={'separate': 'voice.ckpt', 'karaoke': 'karaoke.ckpt'}))
    rw._populate()
    rw._select('B')
    rw.btn_realign.click()
    _, req = runner.calls[-1]
    assert req['lines'][0]['source_stem'] == 'backing'
    assert req['karaoke'] == 'karaoke.ckpt' and req['separate'] == 'voice.ckpt'


def test_overlapping_lanes_render_and_remain_selectable(review, tmp_path):
    rw, _, _, _, _ = review
    rw.session = Session(A.Alignment(5000, [performance('L', 'lead', 1000, 3000),
                         performance('B', 'love', 1200, 3500, bg=True, ref={'parent': 'L'}),
                         performance('C', 'oh', 1400, 4000, role='adlib', bg=True, ref={'parent': 'L'})]))
    rw._populate()
    rw.timeline.set_samples(np.zeros((48000, 2), np.float32))
    blocks = {b['id']: b for b in rw.timeline.blocks() if b['id']}
    assert len({blocks[key]['lane'] for key in ('L', 'B', 'C')}) == 3
    for key in ('L', 'B', 'C'):
        block = blocks[key]
        hit = rw.timeline.unit_at(rw.timeline._x((block['start'] + block['end']) / 2), rw.timeline._lane_top(block['lane']) + 5)
        assert hit[0]['id'] == key
    assert rw.grab().save(str(tmp_path / 'vocal-review.png'))


def test_missing_layer_models_cannot_block_standard_transcription(review, tmp_path):
    rw, runner, _, _, _ = review
    assert not rw.cb_layers.isEnabled() and not rw.cb_layers.isChecked()
    assert 'lead/backing model' in rw.lb_layers_status.text()
    assert rw.grab().save(str(tmp_path / 'missing-layer-models.png'))
    rw._transcribe()
    command, params = runner.calls[-1]
    assert command == 'asr' and params['karaoke'] == ''


def test_layer_availability_updates_without_silently_changing_a_selected_mode(review, monkeypatch):
    rw, _, _, _, _ = review
    monkeypatch.setattr(env, 'separation_installed', lambda root: True)
    monkeypatch.setattr(env, 'separator_model_ready', lambda root, filename=env.SEPARATION_MODEL: True)
    rw._refresh_layers()
    assert rw.cb_layers.isEnabled() and not rw.cb_layers.isChecked()
    rw.cb_layers.setChecked(True)
    monkeypatch.setattr(env, 'separator_model_ready', lambda root, filename=env.SEPARATION_MODEL: filename != env.KARAOKE_MODEL)
    rw._refresh_layers()
    assert rw.cb_layers.isChecked() and rw.cb_layers.isEnabled()
    assert env.KARAOKE_MODEL in rw.cb_layers.toolTip()
    rw.cb_layers.setChecked(False)
    assert not rw.cb_layers.isEnabled()


def test_missing_layer_validation_names_the_asset_and_actual_lab_path(tmp_path, monkeypatch):
    root = tmp_path / 'custom-lab'
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'x')
    monkeypatch.setattr(env, 'is_installed', lambda root: True)
    monkeypatch.setattr(env, 'hf_model_ready', lambda *args: True)
    monkeypatch.setattr(env, 'separation_installed', lambda root: True)
    monkeypatch.setattr(env, 'separator_model_ready', lambda root, filename=env.SEPARATION_MODEL: filename != env.KARAOKE_MODEL)
    message = lab.prerequisites(root, lang='es', separate=True, audio=audio, karaoke=True, asr=True)
    assert env.KARAOKE_MODEL in message
    assert str(root / 'separator-models') in message
    assert 'Turn off' in message
    assert lab.prerequisites(root, lang='es', separate=True, audio=audio, karaoke=False, asr=True) is None
