"""Mixed lane adapters retain independent dense references and lowering-specific rounding."""
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness import autocases, lifted
from tests.harness.case import Cases
from tests.refs import mixed


@pytest.mark.parametrize('fp',['exact','readable'])
@pytest.mark.parametrize('cls,fields',[
    ('fhadd2_a16b_rc__RRR',{'Afmt':'F16x2','iswzA':'H0_H0','negA':'NP','Ra@absolute':1,'Rc@negate':1}),
    ('fhadd2_a16b_urc__RUR',{'Afmt':'BF16x2','rnd':'RM'}),
    ('fhadd2_d16b_rc__RRR',{'Dfmt':'F16x2','ftz':'FTZ','rnd':'RZ'}),
    ('fhadd2_d16b_imm__RIR',{'Dfmt':'BF16x2','rnd':'RZ'}),
    ('fhmul2_d16b_rb__RRR',{'Dfmt':'BF16x2','rnd':'RZ'}),
    ('fhfma2_a16b_rb_urc__RRU',{'Afmt':'BF16x2','rnd':'RP','negC':'NP','iswzC':'F32x2.LO_HI'}),
    ('fhfma2_a16b_x_rc__RIR',{'Afmt':'F16x2','iswzA':'H1_H1','Ra@negate':1}),
])
def test_mixed_reference_adapters(cls,fields,fp):
    k=Cases('SM107').classes[cls]
    operands={n:0 for n,a in k.operand_types.items() if a.type in ('SImm','UImm','RSImm','F32Imm')}
    word=encoding.encode('SM107',cls,**(operands|fields))
    form=settings('SM107',pydecode.decode('SM107',bytes.fromhex(word)))
    cases=autocases.generate('SM107',cls,form,fp)
    assert cases
    for case in cases:
        status,problems=lifted.run('SM107',fp,case)
        assert status=='native' and not problems,(case,status,problems)


def test_mixed_lane_reference_swizzles_and_signs():
    assert mixed.ref_lanes(0x40003c00,'H1_H0',False,False,False,'F16')==[0x3f800000,0x40000000]
    assert mixed.ref_lanes(0xc0003c00,'H1_H1',True,True,True,'F16')==[0xc0000000,0x40000000]
    assert mixed.ref_lanes(0x400000003f800000,'F32x2.LO_HI',False,False,True)==[0x40000000,0xbf800000]
