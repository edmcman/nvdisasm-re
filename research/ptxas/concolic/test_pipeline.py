"""Normalization checks: uv run --with pytest python -m pytest research/ptxas/concolic"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[2]), str(HERE.parents[2] / 'tests')]
from corpus import name_key, token_key
from forms import decode_section, digest
from semantic_cases import encode

PTX = b'.entry k() { add.u32 %r1, %r2, 3; st.global.u32 [%rd0], "a b"; }'

def test_tokens():
    assert token_key(PTX) == token_key(PTX.replace(b', ', b' ,\n\t').replace(b'{', b'{ // note\n /* x */'))
    assert token_key(PTX) != token_key(PTX.replace(b'"a b"', b'"a  b"'))
    assert token_key(PTX) != token_key(PTX.replace(b'%r1, %r2', b'%r1, %r 2'))

KERNEL = b""".version 8.7
.target sm_75
.visible .entry k(.param .u32 a, .param .u64 out) {
.reg .b32 %r<4>; .reg .b64 %rd<2>; .shared .b8 buf[16];
ld.param.u32 %r1, [a]; ld.param.u64 %rd1, [out];
L: add.u32 %r2, %r1, 3; st.global.u32 [%rd1], %r2; ret;
}"""

def renamed(*pairs):
    data = KERNEL
    for old, new in pairs: data = data.replace(old, new)
    return name_key(data)

def test_bound_names():
    key = name_key(KERNEL)
    assert key == renamed((b'entry k', b'entry kernel'), (b'buf', b'scratch'), (b'L:', b'top:'))
    assert key == renamed((b'%r', b'%v'), (b'[a]', b'[x]'), (b'u32 a', b'u32 x'))
    assert key != renamed((b'[a]', b'[out]'))                # different binding structure
    assert key != renamed((b'%r2, %r1, 3', b'%r2, %r2, 3'))  # register aliasing
    assert key != renamed((b'add.u32', b'sub.u32'))
    assert key != renamed((b'sm_75', b'sm_80'))
    # A name bound like a mnemonic stays literal, so add/sub cannot merge through renaming.
    assert (renamed((b'u32 a', b'u32 add'), (b'[a]', b'[add]')) !=
            renamed((b'u32 a', b'u32 sub'), (b'[a]', b'[sub]'), (b'add.u32', b'sub.u32')))

def sequence(*instructions, arch='SM89'):
    forms, seq, _, errors = decode_section(arch, b''.join(bytes.fromhex(encode(arch, k, **v)) for k, v in instructions))
    assert not errors
    return digest(seq), set(forms)

RRR = 'iadd3_noimm__RRR_RRR'
IMM = 'iadd3_imm__RsIR_RIR'
LOP = 'lop3_lut__RRR_RRR'
EXIT = ('exit_', {})

def same(a, b): assert sequence(a, EXIT) == sequence(b, EXIT)
def differ(a, b): assert sequence(a, EXIT)[0] != sequence(b, EXIT)[0]

def test_register_renaming():
    same((RRR, dict(Rd=5, Ra=4, Rb=5, Rc=255)), (RRR, dict(Rd=9, Ra=2, Rb=9, Rc=255)))

def test_aliasing_and_zero():
    differ((RRR, dict(Rd=5, Ra=4, Rb=5, Rc=255)), (RRR, dict(Rd=6, Ra=4, Rb=5, Rc=255)))
    differ((RRR, dict(Rd=5, Ra=4, Rb=6, Rc=255)), (RRR, dict(Rd=5, Ra=4, Rb=6, Rc=7)))

def test_predicates():
    same((RRR, dict(Pg=0, Rd=1, Ra=2, Rb=3)), (RRR, dict(Pg=3, Rd=1, Ra=2, Rb=3)))
    differ((RRR, dict(Pg=0, Rd=1, Ra=2, Rb=3)), (RRR, {'Pg': 0, 'Pg@not': 1, 'Rd': 1, 'Ra': 2, 'Rb': 3}))
    differ((RRR, dict(Pg=0, Rd=1, Ra=2, Rb=3)), (RRR, dict(Pg=7, Rd=1, Ra=2, Rb=3)))

def test_data_constants_and_selectors():
    same((IMM, dict(Rd=1, Ra=2, Sb=5)), (IMM, dict(Rd=1, Ra=2, Sb=0x1234)))
    differ((LOP, dict(Rd=1, Ra=2, Rb=3, imm8=0x96)), (LOP, dict(Rd=1, Ra=2, Rb=3, imm8=0xe8)))
    differ(('lop3_lut__RuIR_RIR', dict(Rd=1, Ra=2, Sb=5, imm8=0x96)), ('lop3_lut__RuIR_RIR', dict(Rd=1, Ra=2, Sb=6, imm8=0x96)))
