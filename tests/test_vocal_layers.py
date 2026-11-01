"""Independent vocal performances, persistence, and review without heavy models."""
import copy

import numpy as np
import pytest

from marea.lab import alignment as A
from marea.lab.align_job import request_layers, text_request_lines
from marea.lab.session import Session
from marea.lab.ttml_in import from_ttml
from marea.lab.ttml_out import export_ttml
from sidecar.layers import layer_window
from sidecar.pipeline import align_line
from tests.test_lab_pipeline import VOCAB, emissions


def performance(uid, text, start, end, **kw):
    return A.Line(start, end, [A.Word(text, start, end, id=uid + '.W0',
                                    syllables=[A.Syl(text, start, end, id=uid + '.W0.S0')])], id=uid, **kw)


def test_other_performance_cannot_shorten_sustain():
    logp = emissions(100, [((10, 15), 'a')])
    energy = np.ones(100)
    mask = np.zeros(100, dtype=bool)
    mask[30:70] = True
    alone = align_line(logp, [['a']], VOCAB, energy=energy)
    together = align_line(logp, [['a']], VOCAB, energy=energy, overlap_mask=mask)
    assert (alone['start'], alone['end']) == (together['start'], together['end'])
    assert together['words'][0]['approximate']


def test_parent_is_not_an_alignment_window():
    assert layer_window({'parent': 'lead'}, {'lead': {'start': 1000, 'end': 2000}}, 0, 20000) is None


def test_v2_round_trip_and_v1_defaults():
    line = performance('L0', 'oh', 100, 500, bg=True, source_stem='backing', review_state='pending')
    al = A.Alignment(1000, [line])
    data = A.to_dict(al)
    assert data['version'] == 2
    assert A.from_dict(data) == al
    data['version'] = 1
    data['lines'][0].pop('source_stem')
    data['lines'][0].pop('review_state')
    old = A.from_dict(data).lines[0]
    assert old.review_state == 'accepted' and old.source_stem == ''


def test_request_and_realign_preserve_stem_independently_of_role():
    line = performance('B', 'oh', 1000, 3000, role='duet', source_stem='backing', review_state='pending')
    session = Session(A.Alignment(5000, [line]))
    assert request_layers(session.alignment)[0]['source_stem'] == 'backing'
    assert session.realign_request('B')['lines'][0]['source_stem'] == 'backing'


def test_secondary_draft_metadata_survives_text_correction():
    draft = [{'text': 'give love', 'start': 900, 'end': 3200, 'role': 'backing', 'agent': '',
              'bg': True, 'source_stem': 'backing', 'review_state': 'pending', 'role_confidence': 0.3}]
    result = text_request_lines(['[backing] give me love'], draft)[0]
    assert result['hint'] == [900, 3200]
    assert result['source_stem'] == 'backing' and result['review_state'] == 'pending'
    assert result['agent'] == ''


@pytest.mark.parametrize('start,end', [(500, 1500), (500, 3500), (1500, 2000), (1500, 3500), (3200, 4000)])
def test_overlapping_ttml_preserves_each_performance(start, end):
    lead = performance('L', 'lead', 1000, 3000, agent='person1')
    bg = performance('B', 'love', start, end, bg=True, agent='person1', ref={'parent': 'L'})
    al = A.Alignment(5000, [lead, bg])
    data = export_ttml(al)
    imported = from_ttml(data, 5000)
    assert sorted((l.text, l.start, l.end, l.agent, l.bg) for l in imported.lines) == sorted(
        (l.text, l.start, l.end, l.agent, l.bg) for l in al.lines)
    assert b'leadlove' not in data


def test_candidate_decisions_do_not_lock_timing_and_are_undoable(tmp_path):
    line = performance('B', 'oh', 100, 500, bg=True, role='uncertain', review_state='pending',
                       ref={'standalone': True})
    session = Session(A.Alignment(1000, [line]))
    with pytest.raises(A.AlignmentError, match='review'):
        session.export(tmp_path, 'new.aligned.ttml')
    with pytest.raises(A.AlignmentError, match='review'):
        export_ttml(session.alignment)
    assert session.preview_lyrics().lines[0].bg[0].syllables[0].text == 'oh'
    assert session.accept('B') == []
    assert not session.find('B')[1].manual_override
    session.export(tmp_path, 'new.aligned.ttml')
    assert session.undo() and session.find('B')[1].review_state == 'pending'
    assert session.reject('B') == [] and not session.alignment.lines
    assert session.alignment.source_lyrics['rejected'][0]['line']['text'] == 'oh'
    assert session.undo() and session.find('B')


def test_reclassification_never_changes_agent_stem_or_other_performance():
    first = performance('L', 'a', 100, 900)
    second = performance('B', 'b', 200, 800, bg=True, source_stem='backing', ref={'parent': 'L'})
    session = Session(A.Alignment(1000, [first, second]))
    before = copy.deepcopy(first)
    assert session.set_identity('B', 'harmony', 'sameSinger') == []
    assert session.find('L')[1] == before
    assert session.find('B')[1].source_stem == 'backing'
    assert session.undo() and session.find('B')[1].agent == 'v1'


def test_unknown_agent_and_independent_line_envelope_round_trip():
    lead = performance('L', 'lead', 1000, 3000, agent='')
    lead.start, lead.end = 800, 3200
    bg = performance('B', 'oh', 500, 4000, agent='', bg=True, role='uncertain', ref={'parent': 'L'})
    result = from_ttml(export_ttml(A.Alignment(5000, [lead, bg])), 5000)
    assert [(l.agent, l.start, l.end) for l in result.lines] == [('', 800, 3200), ('', 500, 4000)]


def test_identical_text_in_different_stems_retains_the_right_candidate():
    draft = [{'text': 'oh', 'start': 100, 'end': 700, 'role': 'lead', 'source_stem': 'lead'},
             {'text': 'oh', 'start': 300, 'end': 900, 'role': 'backing', 'source_stem': 'backing', 'review_state': 'pending'}]
    result = text_request_lines(['[backing] oh', 'oh'], draft)
    assert [l['source_stem'] for l in result] == ['backing', 'lead']
    assert result[0]['review_state'] == 'pending'
    with pytest.raises(ValueError, match='pending'):
        text_request_lines(['oh'], draft)


def test_syllable_text_correction_preserves_other_units_and_inline_attributes():
    data = b'''<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><head><metadata>
      <ttm:agent xml:id="person1" type="person"/></metadata></head><body><div><p begin="1s" end="3s" ttm:agent="person1" xml:id="original">
      <span begin="1s" end="1.5s" xml:id="s1">hel</span><span begin="1.5s" end="2s">lo</span>
      <span ttm:role="x-translation">translation</span></p></div></body></tt>'''
    session = Session(from_ttml(data, 5000), data)
    line = session.alignment.lines[0]
    other = copy.deepcopy(line.words[0].syllables[1])
    assert session.set_text(line.words[0].syllables[0].id, 'hal') == []
    assert line.words[0].syllables[1] == other
    out = export_ttml(session.alignment, data)
    assert b'xml:id="s1"' in out and b'xml:id="original"' in out and b'translation' in out
    assert session.undo() and session.alignment.lines[0].text == 'hello'


def test_manual_split_and_reject_preserve_other_performances():
    lead = performance('L', 'lead', 100, 900)
    bg = A.Line(200, 800, [A.Word('give', 200, 400, id='B.W0'), A.Word('love', 500, 800, id='B.W1')],
                id='B', bg=True, source_stem='backing', ref={'parent': 'L'})
    session = Session(A.Alignment(1000, [lead, bg]))
    assert session.split_performance('B', 1) == []
    assert session.find('L')[1] == lead
    assert session.find('B')[1].text == 'give' and session.find('B.split')[1].text == 'love'
    assert session.find('B.split')[1].source_stem == 'backing'
    export_ttml(session.alignment)
    assert session.undo() and session.find('B')[1].text == 'give love'


def test_rejecting_a_lead_keeps_its_backing_as_an_independent_performance():
    lead = performance('L', 'lead', 100, 900)
    bg = performance('B', 'oh', 200, 800, bg=True, ref={'parent': 'L'})
    session = Session(A.Alignment(1000, [lead, bg]))
    assert session.reject('L') == []
    assert session.find('B')[1].ref == {'standalone': True}
    assert from_ttml(export_ttml(session.alignment), 1000).lines[0].text == 'oh'


def test_exported_layers_survive_existing_project_persistence():
    from marea.model.lyrics import parse_ttml
    from marea.project import lyrics_from_dict, lyrics_to_dict
    lead = performance('L', 'lead', 1000, 3000, agent='person1')
    layers = [lead, performance('B', 'love', 500, 3500, bg=True, role='harmony', agent='person1', ref={'parent': 'L'}),
              performance('C', 'oh', 2500, 4000, bg=True, role='adlib', agent='person2', ref={'parent': 'L'})]
    lyrics = parse_ttml(export_ttml(A.Alignment(5000, layers)))
    assert lyrics_from_dict(lyrics_to_dict(lyrics)) == lyrics


def test_editing_a_background_envelope_adds_its_own_timing_without_moving_syllables():
    data = b'''<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body><div>
      <p begin="1s" end="3s"><span begin="1s" end="3s">lead</span>
      <span ttm:role="x-bg"><span begin="1.2s" end="1.4s">(oh)</span></span></p></div></body></tt>'''
    session = Session(from_ttml(data, 5000), data)
    backing = next(l for l in session.alignment.lines if l.bg)
    assert session.set_times(backing.id, 1100, 1700) == []
    imported = from_ttml(export_ttml(session.alignment, data), 5000)
    backing = next(l for l in imported.lines if l.bg)
    assert (backing.start, backing.end) == (1100, 1700)
    assert (backing.leaves[0].start, backing.leaves[0].end) == (1200, 1400)
