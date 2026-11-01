"""Review integration for overlapping vocal performances."""
import numpy as np

from marea.lab import alignment as A
from marea.lab.session import Session
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
