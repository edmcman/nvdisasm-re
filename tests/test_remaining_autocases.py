"""Independent checks of collective masks, memory guards and explicit environment limits."""
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness import autocases
from tests.harness.case import Cases
from tests.harness.checks import problems


def generated(arch,cls,fields,fp):
    k=Cases(arch).classes[cls]
    defaults={n:0 for n,t in k.operand_types.items() if t.type in ('SImm','UImm','RSImm','F32Imm')}
    word=encoding.encode(arch,cls,**(defaults|fields))
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(word)))
    return autocases.generate(arch,cls,form,fp,native=k.mnemonic in ('LD','LDS','LDL','ST','STG','STS','STL'))


@pytest.mark.parametrize('fp',['exact','readable'])
def test_vote_partial_members_and_inversion(fp):
    cases=generated('SM89','vote_',{'voteop':'ANY','Pp@not':1},fp)
    for case in cases:
        w=case.request['context']['warp']
        ballot=sum(1<<i for i,p in enumerate(w['predicates']) if w['active']>>i&1 and not p)
        assert case.expected=={'R8':ballot,'P0':int(bool(ballot))}


@pytest.mark.parametrize('fp',['exact','readable'])
def test_generic_access_requires_only_named_memory_effect(fp):
    cases=generated('SM89','ld__sImmOffset',{'e':'E','sz':'S8','Pnz@not':0},fp)
    assert {v for c in cases for n,v in c.expected.items() if n.startswith('R')}=={0,0xffffffef}
    for c in cases:
        assert '__native' not in c.expected
        assert c.expected['__max_ops']==dict(count=128,userops=['sass_memory_order'],branches=True)


def test_explicit_branch_permission_keeps_effect_guard():
    expected={'__max_ops':dict(count=128,userops=['sass_memory_order'],branches=True)}
    result=dict(registers={},pcode_ops={'CALLOTHER':1,'CBRANCH':3},pcode_userops=['sass_memory_order'])
    assert not problems(expected,result)
    assert problems({'__max_ops':dict(count=128,userops=['sass_memory_order'])},result)
    assert problems(expected,dict(result,pcode_userops=['sass_prim_LD']))
    assert problems({'__native':True},result)


def test_mufu_error_contract_does_not_claim_values():
    cases=generated('SM89','mufu__RRR_RR',{'mufuop':'RCP'},'exact')
    assert cases
    assert all(c.expected=={'error':'MUFU approximation'} for c in cases)


@pytest.mark.parametrize('op', ['SIN', 'COS'])
def test_mufu_sincos_profiles_have_values(op):
    from tests.refs import mufu
    cases=generated('SM89','mufu__RRR_RR',{'mufuop':op},'exact')
    assert cases
    for case in cases:
        destination=next(iter(case.expected))
        assert case.expected[destination] == mufu.sincos(case.request['registers']['R2'],op)
        assert not case.hardware or not case.hardware.get('skip')


def test_mufu_error_checks_never_enter_value_coverage(monkeypatch):
    from tests import autocases
    monkeypatch.setattr(autocases,'emulator_cases',lambda *a,**kw: {})
    row=dict(cls='mufu__RRR_RR',opcode='MUFU',combinations=dict(
        labels={'mufuop':['RCP']},decides=[],table=[['primitive']],free=1))
    plan,report=autocases.plan('SM89',[row])
    assert not plan['forms']
    assert report['counts']['blocked']==1
    assert any('numerical approximation oracle unavailable' in reason for reason in report['gaps'][0]['attempts'])


def test_ordering_event_payload_is_checked_and_gpu_eligible():
    from tests.harness import lifted, device_cases
    case = generated('SM89', 'ld__sImmOffset', {'e': 'E', 'sz': '32', 'sem': 'STRONG', 'sco': 'GPU'}, 'exact')[0]
    event = case.expected['__event_values']
    assert event == [dict(op='LD', controls=[2, 4, 1, 0])]
    assert not lifted.run('SM89', 'exact', case)[1]
    assert device_cases.classify(case, arch='SM89')[0] == 'memory'
    for wrong in ([], [dict(op='ST', controls=event[0]['controls'])],
                  [dict(op='LD', controls=list(reversed(event[0]['controls'])))],
                  [dict(op='LD', controls=[0] * len(event[0]['controls']))]):
        assert lifted.run('SM89', 'exact', case._replace(expected={**case.expected, '__event_values': wrong}))[1]


@pytest.mark.parametrize('invert', [0, 1])
def test_neutral_load_anchor_issues_nonzero_access(invert):
    from tests.harness import lifted
    word = encoding.encode('SM89', 'ld__sImmOffset', e='E', sz='32', Ra_offset=0, **{'Pnz@not': invert})
    form = settings('SM89', pydecode.decode('SM89', bytes.fromhex(word)))
    cases = autocases.generate('SM89', 'ld__sImmOffset', form, profile_mode='minimal', sample=100)
    assert cases and all(any(v for n, v in c.expected.items() if n.startswith('R')) for c in cases)
    assert all(not lifted.run('SM89', 'exact', c)[1] for c in cases)


@pytest.mark.parametrize('mode',['ANY','ALL','EQ'])
@pytest.mark.parametrize('invert',[0,1])
def test_vote_profiles_witness_both_collective_results(mode,invert):
    cases=generated('SM89','vote_',{'voteop':mode,'Pp@not':invert},'exact')
    assert {c.expected['P0'] for c in cases}=={0,1}
