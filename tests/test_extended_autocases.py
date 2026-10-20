"""Integration checks for reference-backed floating and predicate forms."""
import itertools
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness import autocases, lifted
from tests.refs import integer


@pytest.mark.parametrize('arch,cls', [
    *[('SM89',cls) for cls in ('fadd__RRR_RR','fmul__RRR_RR','ffma__RRR_RRR',
        'dadd__RRR_RR','dmul__RRR_RR','dfma__RRR_RRR','fsel__RRR_RRR',
        'fmnmx__RRR_RRR','fset__RRR_RRR','fsetp__RRR_RRR','dsetp__RRR_RR',
        'fchk__RRR_RR','frnd__f32_R','plop3_lut_2out_','uplop3_lut_2out_',
        'p2r__RRR_RRR','r2p__RRR','up2ur__Imm')],
    ('SM100','fmnmx3__RRR_RRR'),('SM120','ufsetp__URURUR_UUU'),
    *[('SM120',cls) for cls in ('ufadd__URURUR_UUU','ufmul__UUU','uffma__UUU_UUU','ufsel__URURUR_UUU','ufrnd__f32_URb')],
])
@pytest.mark.parametrize('fp',['exact','readable'])
def test_extended_reference_adapters(arch,cls,fp):
    fields={'Sb':0} if cls=='up2ur__Imm' else {}
    if cls.startswith('dsetp'): fields['test']='LT'
    if 'plop3' in cls: fields.update(uimm8=0,vimm8=255)
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(encoding.encode(arch,cls,**fields))))
    cases=autocases.generate(arch,cls,form,fp)
    assert cases
    for case in cases:
        _,found=lifted.run(arch,fp,case)
        assert not found,(case,found)


def test_predicate_truth_tables_all_inputs():
    for table in range(256):
        outputs=[integer.predicate_lut(table,table^255,*xs) for xs in itertools.product((0,1),repeat=3)]
        assert sum(a<<i for i,(a,b) in enumerate(outputs))==table
        assert all(a^b==1 for a,b in outputs)


def test_double_immediate_uses_high_word():
    from tests.harness import autocase_extended
    from tests.harness.case import Cases
    cls='dadd__RRsI_RI'
    container=Cases('SM89')
    placed=autocases.Inputs(container,container.classes[cls],{})
    assert autocase_extended.operand(placed,'Sc',0x40000000deadbeef,6,64)==0x4000000000000000
    assert placed.fields['Sc']==0x40000000
    form=settings('SM89',pydecode.decode('SM89',bytes.fromhex(encoding.encode('SM89',cls,Sc=0))))
    cases=autocases.generate('SM89',cls,form)
    assert cases
    for case in cases:
        _,found=lifted.run('SM89','exact',case)
        assert not found


def test_unmigrated_source_modifier_is_explicit_gap():
    from tests.harness import autocase_extended
    assert autocase_extended.blocked_reason('FMNMX3',{'Ra@negate':'set'}) == 'source-sign modifier corpus remains manual'


def test_readable_minmax_ties_and_unordered():
    from tests.refs import float
    assert float.fmnmx_readable(0x80000000,0,True)==0
    assert float.fmnmx_readable(0x80000000,0,False)==0x80000000
    assert float.fmnmx_readable(0x3f800000,0x7fc12345,True)==0x7fc12345
    assert float.fmnmx_readable(0x3f800000,0x7fc12345,False)==0x3f800000


@pytest.mark.parametrize('fp',['exact','readable'])
def test_float_set_flush_differs_by_lowering(fp):
    cls='fset__RRR_RRR'
    form=settings('SM89',pydecode.decode('SM89',bytes.fromhex(encoding.encode('SM89',cls,fcomp='LT',ftz='FTZ'))))
    for case in autocases.generate('SM89',cls,form,fp):
        _,found=lifted.run('SM89',fp,case)
        assert not found,(case,found)


def test_extended_adapter_imports_before_dispatcher():
    import subprocess
    import sys
    result=subprocess.run([sys.executable,'-c','from tests.harness import autocase_extended, autocases; assert autocase_extended.OPS <= autocases.OWNED'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('cls',['fadd__RRR_RR','fmul__RRR_RR','ffma__RRR_RRR',
    'fset__RRR_RRR','fsetp__RRR_RRR','fmnmx__RRR_RRR',
    'fsel__RRR_RRR','fchk__RRR_RR','frnd__f32_R','frnd__f64_R',
    'dsetp__RRR_RR','dsetp__RRU_RU','dsetp__RRsI_RI','dsetp__RRC_RC','dsetp__RRCx_RCx'])
@pytest.mark.parametrize('fp',['exact','readable'])
def test_source_sign_modifiers(cls,fp):
    fields={'test':'LT'} if cls.startswith('dsetp') else {}
    if cls=='dsetp__RRsI_RI': fields['Sc']=0
    if cls=='dsetp__RRC_RC': fields.update(Sc_bank=2,Sc_addr=0)
    if cls=='dsetp__RRCx_RCx': fields['Sc_offset']=0
    form=settings('SM89',pydecode.decode('SM89',bytes.fromhex(encoding.encode('SM89',cls,**fields))))
    flags=[n for n in form if n.endswith(('@absolute','@negate'))]
    for values in itertools.product(('unset','set'),repeat=len(flags)):
        modified=form | dict(zip(flags,values))
        for case in autocases.generate('SM89',cls,modified,fp):
            _,found=lifted.run('SM89',fp,case)
            assert not found,(modified,case,found)


def test_floating_absolute_precedes_negation():
    from tests.refs import float
    assert float.source_modifiers(0xbf800000,32,True,True)==0xbf800000
    assert float.source_modifiers(0xfff8123456789abc,64,True,False)==0x7ff8123456789abc
    assert float.source_modifiers(0x80000000,32,True,False)==0


@pytest.mark.parametrize('cls',['ufadd__URURUR_UUU','ufmul__UUU','uffma__UUU_UUU','ufsetp__URURUR_UUU'])
@pytest.mark.parametrize('fp',['exact','readable'])
def test_uniform_source_sign_modifiers(cls,fp):
    arch='SM120'
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(encoding.encode(arch,cls))))
    flags=[n for n in form if n.endswith(('@absolute','@negate'))]
    assert flags
    for values in itertools.product(('unset','set'),repeat=len(flags)):
        modified=form | dict(zip(flags,values))
        cases=autocases.generate(arch,cls,modified,fp)
        assert cases
        for case in cases:
            _,found=lifted.run(arch,fp,case)
            assert not found,(modified,case,found)


@pytest.mark.parametrize('cls',['dsetp__RRR_RR','dsetp__RRU_RU','dsetp__RRsI_RI','dsetp__RRC_RC','dsetp__RRCx_RCx'])
@pytest.mark.parametrize('fp',['exact','readable'])
@pytest.mark.parametrize('comparison',['MIN','MAX'])
@pytest.mark.parametrize('bop',['AND','OR','XOR'])
def test_dsetp_comparison_endpoints(cls,fp,comparison,bop):
    fields=dict(test=comparison,bop=bop)
    if cls=='dsetp__RRsI_RI': fields['Sc']=0
    if cls=='dsetp__RRC_RC': fields.update(Sc_bank=2,Sc_addr=0)
    if cls=='dsetp__RRCx_RCx': fields['Sc_offset']=0
    form=settings('SM89',pydecode.decode('SM89',bytes.fromhex(encoding.encode('SM89',cls,**fields))))
    cases=autocases.generate('SM89',cls,form,fp)
    assert cases
    for case in cases:
        _,found=lifted.run('SM89',fp,case)
        assert not found,(case,found)


def test_uniform_round_routes_to_conversion_family():
    assert autocases.FAMILIES['UFRND']=='conversions'


@pytest.mark.parametrize('fp',['exact','readable'])
def test_mixed_source_no_flush_alias(fp):
    cls='fmul__RUR_RU'
    form=settings('SM90',pydecode.decode('SM90',bytes.fromhex(encoding.encode('SM90',cls,fmz='nofmz_hfma2',scale='M8'))))
    for case in autocases.generate('SM90',cls,form,fp):
        _,found=lifted.run('SM90',fp,case)
        assert not found,(case,found)
