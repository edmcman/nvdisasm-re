"""Conversion adapter boundaries and both independent lowering contracts."""
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness import autocases, lifted


@pytest.mark.parametrize('arch,cls,fields',[
    ('SM89','f2i__Rb_32b',{}),
    ('SM89','f2i__Rb_32b',{'rnd':'CEIL','ftz':'FTZ','Rb@absolute':1,'Rb@negate':1}),
    ('SM89','f2i__Ib_16b',{'Sb':0}),
    ('SM89','f2i__Ib_64b',{'Sb':0}),
    ('SM89','i2f__IS_64b',{'Sb':0}),
    ('SM89','i2fp__RRR',{}),
    ('SM89','f2f_f32_downconvert__RRR_RRR',{}),
    ('SM120','uf2i__IU_32b',{'Sb':0}),
    ('SM120','ui2f__IS_32b',{'Sb':0}),
    ('SM120','ui2fp__UUU',{'dstfmt':'F32','srcfmt':'U32'}),
    ('SM89','f2i__Rb_16b',{'hsel':'H1'}),
    ('SM89','i2f__Rb_8b',{'bsel':'B3'}),
])
@pytest.mark.parametrize('fp',['exact','readable'])
def test_conversion_reference_adapters(arch,cls,fields,fp):
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(encoding.encode(arch,cls,**fields))))
    cases=autocases.generate(arch,cls,form,fp)
    assert cases
    for case in cases:
        _,found=lifted.run(arch,fp,case)
        assert not found,(case,found)


def test_signed_64_immediate_is_high_word():
    form=settings('SM89',pydecode.decode('SM89',bytes.fromhex(encoding.encode('SM89','i2f__IS_64b',Sb=0,dstfmt='F32'))))
    cases=autocases.generate('SM89','i2f__IS_64b',form)
    assert any(0xcf800000 in c.expected.values() for c in cases)


def test_conversion_fallback_is_an_explicit_gap():
    cls='f2i_Rd64__CXb_64b'
    form=settings('SM75',pydecode.decode('SM75',bytes.fromhex(encoding.encode('SM75',cls,Sb_offset=0))))
    with pytest.raises(autocases.Unsupported,match='runtime fallback'):
        autocases.generate('SM75',cls,form,native=False)
