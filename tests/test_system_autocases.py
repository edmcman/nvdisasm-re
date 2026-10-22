"""System adapters preserve register/PC state, sign extension and address provenance."""
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness import autocases, autocase_system, lifted
from tests.harness.case import Cases
from tests.refs import control, memory


def generated(arch,cls,fields,fp):
    k=Cases(arch).classes[cls]
    operands={n:0 for n,a in k.operand_types.items() if a.type in ('SImm','UImm','RSImm','F32Imm')}
    word=encoding.encode(arch,cls,**(operands|fields))
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(word)))
    return autocases.generate(arch,cls,form,fp)


@pytest.mark.parametrize('fp',['exact','readable'])
@pytest.mark.parametrize('arch,cls,fields',[
    ('SM89','bra_',{}),('SM89','bra_',{'cond':'U','Pp@not':1}),
    ('SM89','call_abs__RIR',{'depth':'NOINC'}),
    ('SM89','call_rel__RIR',{'depth':'NOINC'}),
    ('SM89','call_abs__RCR',{'depth':'NOINC'}),
    ('SM89','call_abs__URIR',{'depth':'NOINC'}),
    ('SM89','ret__REL',{'depth':'NODEC'}),
    ('SM89','ret__ABS_UR',{'depth':'NODEC'}),
    ('SM89','exit_',{'no_atexit':'NO_ATEXIT'}),('SM89','nop_',{}),
    ('SM89','s2r_',{}),('SM89','cs2r_',{'sz':'64'}),('SM89','s2ur_',{}),
    ('SM89','ldc__RaNonRZ',{'sz':'S8'}),('SM89','ldc__RaRZ',{'sz':'U16'}),
    ('SM89','uldc_const__RCR',{'sz':'S16'}),
    ('SM120','ldcu_const_RCR_',{'sz':'32'}),
])
def test_system_reference_adapters(arch,cls,fields,fp):
    cases=generated(arch,cls,fields,fp)
    assert cases
    for case in cases:
        status,problems=lifted.run(arch,fp,case)
        assert status=='native' and not problems,(case,status,problems)


def test_dotted_special_registers_are_checked_by_python():
    case=next(c for c in generated('SM89','s2r_',{},'exact') if c.request['registers'].get('SR_CTAID.X')==0x89abcdef)
    assert lifted.applies(case)
    assert lifted.run('SM89','exact',case)==('native',[])
    broken=case._replace(expected={**case.expected,'R8':0})
    assert lifted.run('SM89','exact',broken)[1]


def test_constant_reference_sign_and_byte_order():
    assert memory.constant(bytes.fromhex('80'),True)==0xffffff80
    assert memory.constant(bytes.fromhex('0080'),True)==0xffff8000
    assert memory.constant(bytes.fromhex('0080'))==0x8000
    assert memory.constant(bytes.fromhex('0100000002000000'))==0x200000001


def test_pc_reference_uses_next_instruction_and_wraps():
    assert control.counter(0x1000,32,True)==0x1030
    assert control.counter(0x1000,-0x1010,True)==0
    assert control.counter(0x1000,0x4560,taken=False)==0x1010
    assert control.counter(0x1000,0)==0


def test_query_reference_distinguishes_half_open_boundaries():
    ranges={'global':[[0x100,0x200]],'shared':[[0x200,0x300]],'local':[[0x300,0x400]]}
    assert memory.query(0x1ff,ranges,'global')==(1,1)
    assert memory.query(0x200,ranges,'global')==(0,2)
    assert memory.query(0x300,ranges,'local')==(1,4)


@pytest.mark.parametrize('cls',['qspc__RaNonRZ','qspc__RaRZ','qspc_urb__Ra64'])
def test_query_cases_require_runtime_without_claiming_native(cls):
    word=encoding.encode('SM89',cls,space='G',e='E',Ra_offset=0)
    form=settings('SM89',pydecode.decode('SM89',bytes.fromhex(word)))
    cases=autocases.generate('SM89',cls,form,native=False)
    assert len(cases)==9
    assert {c.expected['P1'] for c in cases}=={0,1}
    assert {v for c in cases for k,v in c.expected.items() if k.startswith('R')}=={1,2,4}
    assert all('__native' not in c.expected for c in cases)
    assert all(lifted.run('SM89','exact',c)==('primitive',[]) for c in cases)


def test_native_memory_effect_contract_rejects_value_primitives():
    from tests.harness.checks import problems
    expected={'R8':0x1234,'__max_ops':{'count':32,'userops':['sass_memory_order']},'__events':1}
    result={'registers':{'R8':0x1234},'events':[{'op':'LDC'}],
            'pcode_ops':{'LOAD':1,'CALLOTHER':1},'pcode_userops':['sass_memory_order']}
    assert not problems(expected,result)
    assert problems({'R8':0x1234,'__native':True},result)  # pure-native contract stays strict
    assert problems(expected,dict(result,pcode_userops=['sass_prim_LDC']))
    assert problems(expected,dict(result,pcode_ops={'CALLOTHER':2},pcode_userops=['sass_memory_order','sass_prim_LDC']))
    assert problems(expected,dict(result,pcode_userops=[]))


def test_nop_requires_inspected_empty_pcode():
    from tests.harness.checks import problems
    expected={'__max_ops':{'count':0,'userops':[]},'__events':0}
    result={'registers':{},'pcode_ops':{},'pcode_userops':[],'events':[]}
    assert not problems(expected,result)
    assert problems(expected,{'registers':{},'events':[]})
    assert problems(expected,dict(result,pcode_ops={'COPY':1}))
    assert problems(expected,dict(result,pcode_ops={'CALLOTHER':1},pcode_userops=['sass_opaque_NOP']))


def test_integer_pcode_limits_keep_their_existing_guards():
    from tests.harness.checks import problems
    one={'registers':{},'pcode_ops':{'COPY':1}}
    assert not problems({'__max_ops':1},one)
    assert problems({'__max_ops':0},one)
    for ops in ({},{'CALLOTHER':1},{'CBRANCH':1}):
        assert problems({'__max_ops':1},dict(one,pcode_ops=ops))


def test_zero_special_pair_keeps_its_physical_overwrite_anchor():
    from tests.cases import control
    from tests.harness import device_cases
    cases=Cases('SM89');control.emulator_cases(cases);cases.complete()
    case=next(c for c in cases if c.cls=='cs2r_')
    assert case.request['registers']=={'R8':1,'R9':2}
    assert case.expected=={'R8':0,'R9':0}
    assert device_cases.classify(case)==('scalar','')
    assert case.id in cases.manual_reasons
    assert lifted.run('SM89','exact',case)==('native',[])


def test_shared_cluster_query_provenance_overlaps():
    ranges = {'global':[[0x100,0x200]], 'shared':[[0x200,0x300]],
              'local':[[0x300,0x400]], 'shared_cluster':[[0x200,0x300]]}
    assert memory.query(0x220, ranges, 'shared') == (1, 10)
    assert memory.query(0x220, ranges, 'shared_cluster') == (1, 10)
    assert memory.query(0x220, ranges, 'global') == (0, 10)
    c = Cases('SM90')
    c.add('qspc__RaNonRZ', dict(Pg='PT', Pu=1, Rd=8, e='E', Ra=2, Ra_offset=0, space='S'),
          dict(R2=0x220, R3=0), dict(P1=1, R8=10), context={'address_spaces':ranges})
    assert lifted.run('SM90', 'exact', c.complete()[0]) == ('primitive', [])
