import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from study_helper import clean_json, validate_suggestion


def make_q(t='single'):
    return {'type': t, 'options': [{'key':'A', 'text':'甲'},{'key':'B','text':'乙'},{'key':'C','text':'丙'}]}


def test_json_fence():
    assert clean_json('```json\n{"answer":["A"]}\n```')['answer'] == ['A']


def test_single():
    assert validate_suggestion(make_q(), {'answer':['B'],'confidence':.8})['answer'] == ['B']


def test_reject_bad_single():
    with pytest.raises(ValueError):
        validate_suggestion(make_q(), {'answer':['A','B']})


def test_reject_unknown_option():
    with pytest.raises(ValueError):
        validate_suggestion(make_q('multiple'), {'answer':['D']})


def test_multi():
    assert validate_suggestion(make_q('multiple'), {'answer':['A','C']})['answer'] == ['A','C']


def test_blank():
    assert validate_suggestion({'type':'blank','options':[]}, {'answer':['自由文本']})['answer'] == ['自由文本']
