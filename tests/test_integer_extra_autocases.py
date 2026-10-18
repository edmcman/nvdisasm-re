"""Additional integer forms exercise references, both words, and aliased destinations."""
import json
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.paths import LANGUAGES
from sass.sleigh.modifiers import settings
from tests.autocases import forms
from tests.harness import autocases, autocase_integer_extra, lifted


@pytest.mark.parametrize('arch', ['SM90','SM100','SM103','SM107','SM120'])
@pytest.mark.parametrize('fp', ['exact','readable'])
def test_additional_integer_native_forms(arch,fp):
    count=0
    rows=json.loads((LANGUAGES/('sass_'+arch.lower()+'_coverage.json')).read_text())
    for row in rows:
        if row['opcode'] not in autocase_integer_extra.OPS:continue
        for form,status in forms(row):
            if status!='native':continue
            cases=autocases.generate(arch,row['cls'],form,fp)
            assert cases
            for case in cases:
                actual,problems=lifted.run(arch,fp,case)
                assert actual=='native' and not problems,(case,problems)
            count+=len(cases)
    assert count


@pytest.mark.parametrize('cls',['mov64iur_imm_','mov64iur_ur_'])
def test_pair_move_preserves_distinct_high_word(cls):
    cases=autocases.generate('SM120',cls,{})
    assert any(c.expected.get('R3')==0x01234567 and c.expected.get('R2')==0x89abcdef for c in cases)


def test_uniform_pack_preserves_bits_from_third_operand():
    cls='ui2ip__URURUR_UUU'
    form=settings('SM120',pydecode.decode('SM120',bytes.fromhex(encoding.encode(
        'SM120',cls,dstfmt='S8',satrelu='SAT',extract_limited='H1'))))
    cases=autocases.generate('SM120',cls,form)
    for case in cases:
        d=pydecode.decode('SM120',bytes.fromhex(case.request['word'])).env['URd']
        preserved=case.request['registers']['UR8']
        assert case.expected['UR'+str(d)]>>16==preserved>>16


def test_packed_viadd_is_an_explicit_gap():
    cls='viadd__RRR_RRR'
    form=settings('SM120',pydecode.decode('SM120',bytes.fromhex(encoding.encode(
        'SM120',cls,fmt='16x2'))))
    with pytest.raises(autocases.Unsupported,match='packed and saturating'):
        autocases.generate('SM120',cls,form)


def test_reserved_uniform_pack_mode_is_rejected():
    with pytest.raises(autocases.Unsupported,match='reserved'):
        autocases.generate('SM120','ui2ip_24__URURUR_UUU',
                           {'dstfmt':'S4','satrelu':'INVALID3','extract_limited':'24'})


def test_uniform_relu_alias_saturates_positive_overflow():
    cases=autocases.generate('SM120','ui2ip_24__URURUR_UUU',
                            {'dstfmt':'S4','satrelu':'RELU','extract_limited':'24'})
    case=cases[0]
    assert case.request['registers']['UR6']==0x7fffffff
    assert case.expected['UR4']&0xff==7
