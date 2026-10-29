"""SM75 precision evidence must preserve the actual compiler lowering."""
from pathlib import Path
import subprocess
import pytest
from sass.paths import PTXAS, ROOT
from sass.decode import cubin, pydecode
from sass.sleigh.modifiers import settings


@pytest.fixture(scope='module')
def compiled_sm75_half(tmp_path_factory):
    root=tmp_path_factory.mktemp('sm75-half-compiler')
    outputs={}
    for source in sorted((ROOT/'tests/ptx/fp16').glob('sm75_*.ptx')):
        output=root/(source.stem+'.cubin')
        subprocess.run([PTXAS,'-O3','-arch=sm_75',str(source),'-o',str(output)],check=True,capture_output=True)
        outputs.update({name.removeprefix('.text.'):code for name,code in cubin.text_sections(output.read_bytes()).items()})
    return outputs


def decoded(code):
    return [(d,settings('SM75',d)) for word in cubin.words(code)
            for d in [pydecode.decode('SM75',word)]]


def test_compiler_broadcast_stores_both_hadd2_lanes(compiled_sm75_half):
    rows=decoded(compiled_sm75_half['broadcast_rz'])
    convert=[d for d,f in rows if d.klass.mnemonic=='HADD2' and f.get('iswzA')=='F32']
    assert len(convert)==1 and convert[0].env['Rc']==255
    # PTX explicitly packs both converted halves; no duplication instruction
    # may be needed between the conversion and full 32-bit output store.
    index=next(i for i,(d,_) in enumerate(rows) if d is convert[0])
    assert rows[index+1][0].klass.mnemonic=='STG'
    assert rows[index+1][0].env['Rb']==convert[0].env['Rd']


@pytest.mark.parametrize('name', [
    'set_packed_0_rz','set_packed_1_rz','setp_packed_0_rz','setp_packed_1_rz',
    'set_eq_both_float','setp_eq_both_float','set_nan_float_half','setp_nan_float_half',
    'set_num_both_float','setp_num_both_float','set_geu_float_half','setp_leu_both_float',
])
def test_compiler_folds_rz_conversion_into_half_comparison(name,compiled_sm75_half):
    rows=decoded(compiled_sm75_half[name])
    comparisons=[(d,f) for d,f in rows if d.klass.mnemonic in ('HSET2','HSETP2')]
    assert len(comparisons)==1
    form=comparisons[0][1]
    assert any(n.startswith('iswz') and v=='F32' for n,v in form.items())
    if 'both_float' in name:
        assert form['iswzA']==form['iswzB_as_C']=='F32'
    assert not any(d.klass.mnemonic in ('F2F','HADD2') for d,_ in rows)


@pytest.mark.parametrize('name,opcode', [('mul_packed_0_rz','HMUL2'),('fma_packed_0_rz','HFMA2')])
def test_compiler_keeps_half_conversion_before_mul_fma(name,opcode,compiled_sm75_half):
    rows=decoded(compiled_sm75_half[name])
    assert any(d.klass.mnemonic=='HADD2' and f.get('iswzA')=='F32' for d,f in rows)
    consumers=[f for d,f in rows if d.klass.mnemonic==opcode]
    assert len(consumers)==1
    assert not any(n.startswith('iswz') and v=='F32' for n,v in consumers[0].items())
