"""Packed adapter lane placement, predicate outputs and independent references."""
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness import autocases, autocase_packed_fp, lifted
from tests.harness.case import Cases


@pytest.mark.parametrize('arch',['SM89','SM120'])
@pytest.mark.parametrize('cls,fields',[
    ('f2ip__RRR',{'dstfmt':'S8','rnd':'ROUND','ntz':'NTZ','relu':'norelu'}),
    ('i2ip_24__RRR_RRR',{'dstfmt':'S4','satrelu':'SAT'}),
    ('hadd2__RR',{}),('hmul2__RR',{}),('hfma2__RRR',{}),('hmnmx2__RRR',{}),
    ('hset2__RR',{'cmp':'LT','bop':'XOR','bval':'BF'}),
    ('hsetp2__RR',{'cmp':'NE','bop':'OR','h_and':'H_AND'}),
])
@pytest.mark.parametrize('fp',['exact','readable'])
def test_packed_reference_adapters(arch,cls,fields,fp):
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(encoding.encode(arch,cls,**fields))))
    cases=autocases.generate(arch,cls,form,fp)
    assert cases
    for case in cases:
        _,found=lifted.run(arch,fp,case)
        assert not found,(case,found)


def test_split_half_immediate_is_high_then_low():
    c=Cases('SM89'); k=c.classes['hadd2__RI']; s=autocases.Inputs(c,k,{})
    assert autocase_packed_fp.operand(s,'Sc',0x3c004000,6)==0x3c004000
    assert s.fields['Sc']==0x3c00 and s.fields['Sc1']==0x4000


@pytest.mark.parametrize('word,swizzle,expected',[
    (0x3c004000,'H1_H0',(0x4000,0x3c00)),
    (0x3c004000,'H0_H0',(0x4000,0x4000)),
    (0x3c004000,'H1_H1',(0x3c00,0x3c00)),
])
def test_asymmetric_lane_broadcasts(word,swizzle,expected):
    assert autocase_packed_fp.lanes(word,swizzle)==expected


def test_half_mixed_precision_is_an_explicit_gap():
    form={'ofmt':'F16_V2','iswzA':'F32'}
    assert 'mixed-precision' in autocase_packed_fp.blocked_reason('HADD2',form)


@pytest.mark.parametrize('fp',['exact','readable'])
def test_old_i2ip_constant_saturation(fp):
    cls='i2ip_24__RCR_RCR';arch='SM75'
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(encoding.encode(arch,cls,Sb_bank=2,Sb_addr=0))))
    for case in autocases.generate(arch,cls,form,fp):
        status,found=lifted.run(arch,fp,case)
        assert status=='native' and not found,(case,found)
