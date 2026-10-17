"""Generated forms must exercise independent references and remain reproducible."""
import json
import pytest
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
import tests.autocases
import tests.harness.autocases
from tests.harness import lifted
from tests.harness.case import Cases


def test_factored_legal_forms():
    row = dict(cls='example', combinations=dict(labels={'mode':['A','B'], 'flag':['unset','set']},
               decides=['mode'], table=[['B','native']], free=2))
    assert list(tests.autocases.forms(row)) == [({'mode':'B','flag':'unset'}, 'native'),
                                         ({'mode':'B','flag':'set'}, 'native')]


@pytest.mark.parametrize('cls,form', [
    ('popc__RRR_RRR', {'Rb@invert':'set'}),
    ('popc__RCR_RCR', {'Sb@invert':'set'}),
    ('sgxt__RRR_RRR', {'fmt':'S32','cw':'W'}),
    ('brev__RCxR_RCxR', {}),
    ('prmt__RRCx_RRCx', {'pmode':'IDX'}),
    ('prmt__RCxR_RCxR', {'pmode':'F4E'}),
    ('isetp__RCxR_RCxR_EX', {'icmp':'NE', 'fmt':'S32', 'bop':'XOR', 'ex':'EX', 'Pp@not':'set', 'Pr@not':'set'}),
    ('uisetp_optional_upr__URsIUR_URIR', {'icmp':'LT', 'fmt':'U32', 'bop':'AND', 'ex':'EX', 'UPp@not':'unset', 'UPr@not':'set'}),
    ('imnmx__RCxR_RCxR', {'fmt':'S32', 'Pp@not':'set'}),
    ('sel__RCR_RCR', {'Pp@not':'set'}),
    ('usel__URuIUR_URIR', {'UPp@not':'set'}),
    ('idp_2a__CXb', {'mode':'2A.HI', 'SrcAFmt':'S16', 'SrcBFmt':'S8', 'Rc@negate':'set'}),
    ('idp_4a__CXb', {'mode':'4A', 'SrcAFmt':'S8', 'SrcBFmt':'U8', 'Rc@negate':'set'}),
    ('shf__RRCx_RRCx', {'fmt':'S64', 'dir':'R', 'cw':'C', 'hilo':'HI'}),
    ('ushf__URuIUR_URIR', {'fmt':'U32', 'dir':'L', 'cw':'W', 'hilo':'LO'}),
])
def test_generated_reference_cases(cls, form):
    cases = tests.harness.autocases.generate('SM89', cls, form)
    assert cases
    for case in cases:
        assert settings('SM89', pydecode.decode('SM89', bytes.fromhex(case.request['word']))) == form
        status, problems = lifted.run('SM89', 'exact', case)
        assert status == 'native' and not problems, (status, problems)
    # A wrong reference must be detected, rather than accepted as coverage.
    case = cases[0]
    name = next(iter(case.expected))
    wrong = case._replace(expected={**case.expected, name:case.expected[name] ^ 1})
    assert lifted.run('SM89', 'exact', wrong)[1]


def test_unsupported_form_never_enters_suite():
    with pytest.raises(tests.harness.autocases.Unsupported, match='unhandled modifiers'):
        tests.harness.autocases.generate('SM89', 'popc__RRR_RRR', {'Rb@invert':'set', 'mystery':'set'})


def test_saved_plan_joins_existing_family(tmp_path, monkeypatch):
    monkeypatch.setattr(tests.harness.autocases, 'PLANS', tmp_path)
    (tmp_path / 'SM89.json').write_text(json.dumps(dict(version=1, forms=[
        dict(family='int_alu', cls='popc__RRR_RRR', settings={'Rb@invert':'set'})])))
    c = Cases('SM89')
    tests.harness.autocases.append(c, 'memory')
    assert not c
    tests.harness.autocases.append(c, 'int_alu')
    assert len(c) == len(tests.harness.autocases.VALUES)


def test_plan_reproducible_and_reports_blocked(monkeypatch):
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *a, **kw: {})
    rows = [dict(cls='popc__RRR_RRR', opcode='POPC', combinations=dict(
        labels={'Rb@invert':['unset','set']}, decides=[], table=[['native']], free=2)),
        dict(cls='other', opcode='OTHER', combinations=dict(
            labels={}, decides=[], table=[['native']], free=1))]
    first, report = tests.autocases.plan('SM89', rows)
    assert first == tests.autocases.plan('SM89', rows)[0]
    assert len(first['forms']) == 2
    assert report['counts']['generated'] == 2
    assert report['counts']['blocked'] == 1
    assert report['gaps'][0]['cls'] == 'other'


def test_manual_forms_are_not_regenerated(monkeypatch):
    cases = tests.harness.autocases.generate('SM89', 'popc__RRR_RRR', {'Rb@invert':'set'})
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *a, **kw: {'int_alu':cases})
    row = dict(cls='popc__RRR_RRR', opcode='POPC', combinations=dict(
        labels={'Rb@invert':['unset','set']}, decides=[], table=[['native']], free=2))
    generated, report = tests.autocases.plan('SM89', [row])
    assert [e['settings'] for e in generated['forms']] == [{'Rb@invert':'unset'}]
    assert report['counts']['manual'] == report['counts']['generated'] == 1


def test_unresolved_forms_are_reported_without_generating(monkeypatch):
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *a, **kw: {})
    row = dict(cls='flo__RRR_RRR', opcode='FLO', combinations=dict(
        labels={}, decides=[], table=[['partially native']], free=1))
    generated, report = tests.autocases.plan('SM89', [row])
    assert not generated['forms']
    assert report['counts']['unresolved_semantics'] == 1
    assert report['gaps'] == [dict(cls='flo__RRR_RRR', settings={}, reason='unresolved semantics')]


def test_simd_plan_uses_its_own_family(monkeypatch):
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *a, **kw: {})
    row = dict(cls='idp_4a__CXb', opcode='IDP', combinations=dict(
        labels={'mode':['4A'], 'SrcAFmt':['S8'], 'SrcBFmt':['U8'], 'Rc@negate':['set']},
        decides=[], table=[['native']], free=1))
    plan, report = tests.autocases.plan('SM89', [row])
    assert report['counts']['generated'] == 1
    assert plan['forms'][0]['family'] == 'int_simd'
