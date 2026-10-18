"""Packed binary32 and ordinary float-pack reference adapter integration."""
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness import autocases, lifted


@pytest.mark.parametrize('arch,cls,fields',[
    ('SM100','fadd2_rc__RRR',{}),
    ('SM100','fmul2_rb__RRR',{}),
    ('SM100','ffma2_rb_rc__RRR',{}),
    ('SM107','fadd2_imm__RRI',{'Sc':0,'negA':'NP'}),
    ('SM89','f2fp__RRR',{'dstfmt':'F16','srcfmt':'F32','merge':'PACK_AB','rndMode':'RN'}),
    ('SM89','f2fp_merge_c__RRR',{'dstfmt':'BF16','srcfmt':'F32','merge':'MERGE_C','rndMode':'RZ','extract':'H1'}),
    ('SM89','uf2fp__URURUR',{'dfmt.sfmt':'F16.F32','merge':'PACK_AB','rndMode':'RN'}),
    ('SM120','uf2fp__URURUR',{'dstfmt':'BF16','srcfmt':'F32','merge':'PACK_AB','rndMode':'RZ'}),
])
@pytest.mark.parametrize('fp',['exact','readable'])
def test_wide_float_reference_adapters(arch,cls,fields,fp):
    form=settings(arch,pydecode.decode(arch,bytes.fromhex(encoding.encode(arch,cls,**fields))))
    cases=autocases.generate(arch,cls,form,fp)
    assert cases
    for case in cases:
        _,found=lifted.run(arch,fp,case)
        assert not found,(case,found)
