"""Generated forms must exercise independent references and remain reproducible."""
import json
import pytest
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests import autocases
from tests.harness import autocases as adapters, lifted
from tests.harness.case import Cases


def test_factored_legal_forms():
    row = dict(cls='example', combinations=dict(labels={'mode':['A','B'], 'flag':['unset','set']},
               decides=['mode'], table=[['B','native']], free=2))
    assert list(autocases.forms(row)) == [({'mode':'B','flag':'unset'}, 'native'),
                                         ({'mode':'B','flag':'set'}, 'native')]


@pytest.mark.parametrize('cls,form', [
    ('popc__RRR_RRR', {'Rb@invert':'set'}),
    ('popc__RCR_RCR', {'Sb@invert':'set'}),
    ('sgxt__RRR_RRR', {'fmt':'S32','cw':'W'}),
    ('brev__RCxR_RCxR', {}),
    ('prmt__RRCx_RRCx', {'pmode':'IDX'}),
    ('prmt__RCxR_RCxR', {'pmode':'F4E'}),
])
def test_generated_reference_cases(cls, form):
    cases = adapters.generate('SM89', cls, form)
    assert cases
    for case in cases:
        assert settings('SM89', pydecode.decode('SM89', bytes.fromhex(case.request['word']))) == form
        status, problems = lifted.run('SM89', 'exact', case)
        assert status == 'native' and not problems, (status, problems)
    # A wrong reference must be detected, rather than accepted as coverage.
    case = cases[0]
    wrong = case._replace(expected={'R2':case.expected['R2'] ^ 1})
    assert lifted.run('SM89', 'exact', wrong)[1]


def test_unsupported_form_never_enters_suite():
    with pytest.raises(adapters.Unsupported, match='unhandled modifiers'):
        adapters.generate('SM89', 'popc__RRR_RRR', {'Rb@invert':'set', 'mystery':'set'})


def test_saved_plan_joins_existing_family(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, 'PLANS', tmp_path)
    (tmp_path / 'SM89.json').write_text(json.dumps(dict(version=1, forms=[
        dict(family='int_alu', cls='popc__RRR_RRR', settings={'Rb@invert':'set'})])))
    c = Cases('SM89')
    adapters.append(c, 'memory')
    assert not c
    adapters.append(c, 'int_alu')
    assert len(c) == len(adapters.VALUES)


def test_plan_reproducible_and_reports_blocked(monkeypatch):
    monkeypatch.setattr(autocases, 'emulator_cases', lambda *a, **kw: {})
    rows = [dict(cls='popc__RRR_RRR', opcode='POPC', combinations=dict(
        labels={'Rb@invert':['unset','set']}, decides=[], table=[['native']], free=2)),
        dict(cls='other', opcode='OTHER', combinations=dict(
            labels={}, decides=[], table=[['native']], free=1))]
    first, report = autocases.plan('SM89', rows)
    assert first == autocases.plan('SM89', rows)[0]
    assert len(first['forms']) == 2
    assert report['counts']['generated'] == 2
    assert report['counts']['blocked'] == 1
    assert report['gaps'][0]['cls'] == 'other'


def test_manual_forms_are_not_regenerated(monkeypatch):
    cases = adapters.generate('SM89', 'popc__RRR_RRR', {'Rb@invert':'set'})
    monkeypatch.setattr(autocases, 'emulator_cases', lambda *a, **kw: {'int_alu':cases})
    row = dict(cls='popc__RRR_RRR', opcode='POPC', combinations=dict(
        labels={'Rb@invert':['unset','set']}, decides=[], table=[['native']], free=2))
    generated, report = autocases.plan('SM89', [row])
    assert [e['settings'] for e in generated['forms']] == [{'Rb@invert':'unset'}]
    assert report['counts']['manual'] == report['counts']['generated'] == 1


def test_unresolved_forms_are_reported_without_generating(monkeypatch):
    monkeypatch.setattr(autocases, 'emulator_cases', lambda *a, **kw: {})
    row = dict(cls='flo__RRR_RRR', opcode='FLO', combinations=dict(
        labels={}, decides=[], table=[['partially native']], free=1))
    generated, report = autocases.plan('SM89', [row])
    assert not generated['forms']
    assert report['counts']['unresolved_semantics'] == 1
    assert report['gaps'] == [dict(cls='flo__RRR_RRR', settings={}, reason='unresolved semantics')]
