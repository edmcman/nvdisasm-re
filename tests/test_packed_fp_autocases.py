"""Packed adapter lane placement, predicate outputs and independent references."""
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.semantics import lifter
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


@pytest.mark.parametrize('cls,selector', [
    ('hmul2__RR', 'iswzA'), ('hmul2__RR', 'iswzB'),
    ('hfma2__RRR', 'iswzA'), ('hfma2__RRR', 'iswzB'), ('hfma2__RRR', 'iswzC'),
])
@pytest.mark.parametrize('fp', ['exact', 'readable'])
def test_sm75_mixed_float_input_uses_reference_cases(cls, selector, fp):
    arch = 'SM75'
    word = encoding.encode(arch, cls, **{selector: 'F32'})
    form = settings(arch, pydecode.decode(arch, bytes.fromhex(word)))
    cases = autocases.generate(arch, cls, form, fp)
    assert cases
    assert any(0x3f801800 in case.request['registers'].values()
               and 0x3c003c00 in case.expected.values() for case in cases)
    if fp == 'exact':
        for nan in (0x7fc12345, 0xffc54321, 0x7f812345, 0xff812345):
            assert any(nan in case.request['registers'].values() and 0x7fff7fff in case.expected.values()
                       for case in cases)
    for case in cases:
        status, found = lifted.run(arch, fp, case)
        assert status == 'native' and not found, (case, found)


@pytest.mark.parametrize('cls,fields', [
    ('hmul2__RR', {'iswzA': 'F32', 'iswzB': 'F32'}),
    ('hfma2__RRR', {'iswzA': 'F32', 'iswzB': 'F32', 'iswzC': 'F32'}),
])
def test_sm75_multiple_float_inputs_use_reference_cases(cls, fields):
    arch = 'SM75'
    word = encoding.encode(arch, cls, **fields)
    form = settings(arch, pydecode.decode(arch, bytes.fromhex(word)))
    cases = autocases.generate(arch, cls, form)
    assert cases
    for case in cases:
        status, found = lifted.run(arch, 'exact', case)
        assert status == 'native' and not found, (case, found)


@pytest.mark.parametrize('cls,fields', [
    ('hmul2__RR', {'iswzA': 'F32', 'Ra@negate': 1, 'Rb@absolute': 1, 'sat': 'SAT'}),
    ('hfma2__RRR', {'iswzC': 'F32', 'Rc@absolute': 1, 'Rb@negate': 1, 'sat': 'SAT'}),
])
def test_sm75_mixed_float_modifiers_use_reference_cases(cls, fields):
    arch = 'SM75'
    word = encoding.encode(arch, cls, **fields)
    form = settings(arch, pydecode.decode(arch, bytes.fromhex(word)))
    cases = autocases.generate(arch, cls, form)
    assert cases
    for case in cases:
        status, found = lifted.run(arch, 'exact', case)
        assert status == 'native' and not found, (case, found)


@pytest.mark.parametrize('cls,fields', [
    ('hmul2__RR', {'iswzA': 'F32', 'fmz': 'FTZ'}),
    ('hmul2__RR', {'iswzB': 'F32', 'fmz': 'FMZ'}),
    ('hfma2__RRR', {'iswzC': 'F32', 'fmz': 'FTZ'}),
    ('hfma2__RRR', {'iswzA': 'F32', 'fmz': 'FMZ'}),
    ('hadd2__RR', {'iswzA': 'F32', 'ofmt': 'F32', 'ftz': 'FTZ'}),
])
@pytest.mark.parametrize('fp', ['exact', 'readable'])
def test_sm75_mixed_float_flush_order_stays_opaque(cls, fields, fp):
    arch = 'SM75'
    regs = {'Rd': 8, 'Ra': 2, **({'Rb': 4, 'Rc': 6} if cls == 'hfma2__RRR' else
                                  {'Rb': 4} if cls == 'hmul2__RR' else {'Rc': 4})}
    word = encoding.encode(arch, cls, **regs, **fields)
    assert lifter.lift(arch, bytes.fromhex(word), fp=fp).status == 'opaque'


@pytest.mark.parametrize('fp', ['exact', 'readable'])
def test_sm75_nonzero_half_sum_widens_after_rounding(fp):
    arch, cls = 'SM75', 'hadd2__RR'
    word = encoding.encode(arch, cls, ofmt='F32')
    form = settings(arch, pydecode.decode(arch, bytes.fromhex(word)))
    cases = autocases.generate(arch, cls, form, fp)
    assert cases
    assert any(case.request['registers'].get('R4') == 0x3c003c00
               and case.request['registers'].get('R6') == 0x10001000
               and 0x3f800000 in case.expected.values() for case in cases)
    for case in cases:
        status, found = lifted.run(arch, fp, case)
        assert status == 'native' and not found, (case, found)


def test_sm80_nonzero_f32_sum_is_not_a_legal_encoding():
    with pytest.raises(ValueError, match='ofmt cannot be .F32 when Ra != RZ'):
        encoding.encode('SM80', 'hadd2__RR', Rd=8, Ra=2, Rc=4, ofmt='F32')


@pytest.mark.parametrize('absolute', [0, 1])
def test_sm80_widening_accepts_negative_zero_with_absolute(absolute):
    arch, cls = 'SM80', 'hadd2__RR'
    word = encoding.encode(arch, cls, Ra='RZ', **{'Ra@negate': 1, 'Ra@absolute': absolute, 'ofmt': 'F32'})
    form = settings(arch, pydecode.decode(arch, bytes.fromhex(word)))
    cases = autocases.generate(arch, cls, form)
    assert cases
    for case in cases:
        status, found = lifted.run(arch, 'exact', case)
        assert status == 'native' and not found, (case, found)


def test_hadd2_inferred_float_input_uses_shared_adapter():
    form={'ofmt':'F16_V2','iswzA':'F32'}
    assert autocase_packed_fp.blocked_reason('HADD2',form) is None


@pytest.mark.parametrize('fp',['exact','readable'])
def test_old_i2ip_constant_saturation(fp):
    cls='i2ip_24__RCR_RCR';arch='SM75'
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(encoding.encode(arch,cls,Sb_bank=2,Sb_addr=0))))
    for case in autocases.generate(arch,cls,form,fp):
        status,found=lifted.run(arch,fp,case)
        assert status=='native' and not found,(case,found)
