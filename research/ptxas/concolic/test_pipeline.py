"""Normalization checks: uv run --with pytest python -m pytest research/ptxas/concolic"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[2]), str(HERE.parents[2] / 'tests')]
from corpus import instruction_region, name_key, token_key
import os, subprocess, pytest
from forms import decode_section, digest
from semantic_cases import encode
from pipeline import Coordinator, SCHEMA, afl_statistics


def test_forms_export_is_live_deduplicated_and_restored_on_resume(tmp_path):
    import io, json, sqlite3, time
    from types import SimpleNamespace
    db = sqlite3.connect(':memory:'); db.executescript(SCHEMA)
    (tmp_path / 'sources').mkdir()
    config = dict(architectures=['SM75'], compiler='test-compiler')
    coordinator = Coordinator(tmp_path, db, config)
    path = tmp_path / 'catalogue.jsonl'
    assert path.read_text() == ''
    coordinator.candidate(b'.entry k() { ret; }', 'seed')
    ck = db.execute('SELECT cache_key FROM compilations').fetchone()[0]
    form = dict(opcode='EXIT', operands=[])
    msg = dict(cache_key=ck, state='decoded', rc=0, diagnostics='', arch='SM75',
               forms={'form-hash': form}, sequences=[])
    publisher = SimpleNamespace(stdin=io.StringIO())
    with path.open() as previous_snapshot:
        coordinator.compiled(msg, publisher)
        coordinator.status({}, {}, time.monotonic(), 'running')
        assert previous_snapshot.read() == ''  # replacement preserves existing readers
    coordinator.compiled(msg, publisher)
    coordinator.status({}, {}, time.monotonic(), 'running')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['signature'] == form
    assert Path(rows[0]['ptx']).read_bytes() == b'.entry k() { ret; }'
    assert rows[0]['cubin'].endswith('/kernel.cubin')
    assert rows[0]['compiler'] == config['compiler']
    path.write_text('stale export\n')
    Coordinator(tmp_path, db, config)
    assert [json.loads(line) for line in path.read_text().splitlines()] == rows
    db.close()


def test_afl_corpus_statistics_survive_rotation_and_resume(tmp_path):
    import json, sqlite3
    contexts = {
        '0:sm_75': dict(worker='0', arch='sm_75', corpus_size=10, pending=3, favored=4, pending_favored=1, covered_edges=20),
        '0:sm_110': dict(worker='0', arch='sm_110', corpus_size=7, pending=2, favored=3, pending_favored=2, covered_edges=15),
        '1:sm_75': dict(worker='1', arch='sm_75', corpus_size=8, pending=4, favored=2, pending_favored=1, covered_edges=18),
    }
    stats = afl_statistics(contexts)
    assert stats['corpus_size'] == 25
    assert stats['corpus_by_arch'] == {'SM75': 18, 'SM101': 7}
    assert stats['pending'] == 9 and stats['pending_favored'] == 4
    assert 'covered_edges' not in stats  # overlapping coverage is not additive
    (tmp_path / 'status.json').write_text(json.dumps(dict(afl=stats)))
    db = sqlite3.connect(':memory:'); db.executescript(SCHEMA)
    coordinator = Coordinator(tmp_path, db, dict(architectures=['SM75', 'SM101']))
    # Revisiting a context replaces its snapshot; inactive contexts remain counted.
    coordinator.afl_contexts['0:sm_75'] = dict(contexts['0:sm_75'], corpus_size=12)
    resumed = afl_statistics(coordinator.afl_contexts)
    assert resumed['corpus_size'] == 27
    assert resumed['contexts']['0:sm_110']['covered_edges'] == 15
    db.close()

PTX = b'.entry k() { add.u32 %r1, %r2, 3; st.global.u32 [%rd0], "a b"; }'

def test_tokens():
    assert token_key(PTX) == token_key(PTX.replace(b', ', b' ,\n\t').replace(b'{', b'{ // note\n /* x */'))
    assert token_key(PTX) != token_key(PTX.replace(b'"a b"', b'"a  b"'))
    assert token_key(PTX) != token_key(PTX.replace(b'%r1, %r2', b'%r1, %r 2'))
    assert token_key(PTX) == token_key(PTX.replace(b'add.u32 ', b'\x1aadd.u32\x1a\x1a'))

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
    same(('lop3_lut__RuIR_RIR', dict(Rd=1, Ra=2, Sb=5, imm8=0x96)), ('lop3_lut__RuIR_RIR', dict(Rd=1, Ra=2, Sb=22222, imm8=0x96)))
    differ(('lop3_lut__RuIR_RIR', dict(Rd=1, Ra=2, Sb=5, imm8=0x96)), ('lop3_lut__RuIR_RIR', dict(Rd=1, Ra=2, Sb=5, imm8=0xfe)))
    differ(('shf__RRuI_RRI', dict(Rd=1, Ra=2, Rb=3, Sc=5)), ('shf__RRuI_RRI', dict(Rd=1, Ra=2, Rb=3, Sc=6)))  # shift count


APP = Path(os.environ.get('PTX_SASS_GEN', '/tmp/ptx-concolic/libafl-target/release/ptx-sass-gen'))
REGION_CASES = [
    b'a\n// BEGIN_INSTRUCTION\nadd.u32 %r2, %r0, %r1;\n// END_INSTRUCTION\nb\n',
    b'ld; /* BEGIN_INSTRUCTION */ add; /* END_INSTRUCTION */ st;',
    b'x // y /* BEGIN_INSTRUCTION */\nadd;\n/* END_INSTRUCTION */',
    b'/* BEGIN_INSTRUCTION */ add; // END_INSTRUCTION\n',
    b'/* END_INSTRUCTION */ add; /* BEGIN_INSTRUCTION */',
    b'/* BEGIN_INSTRUCTION add; /* END_INSTRUCTION */',
    b'BEGIN_INSTRUCTION add; END_INSTRUCTION',
    b'add.u32 %r2, %r0, %r1;',
]

@pytest.mark.skipif(not APP.exists(), reason='ptx-sass-gen not built')
def test_region_parsers_agree(tmp_path):
    """The concolic worker (Python) and the mutators (Rust) must use the same instruction region."""
    files = [*HERE.parent.glob('*.ptx'), *HERE.glob('*.ptx')]
    for i, case in enumerate(REGION_CASES):
        files.append(tmp_path / f'{i}.ptx'); files[-1].write_bytes(case)
    for f in files:
        rust = subprocess.run([APP, '--region', f], capture_output=True, text=True, check=True).stdout.split()
        python = instruction_region(f.read_bytes())
        assert rust == (['none'] if python is None else [str(python[0]), str(python[1])]), f
    assert all(instruction_region(f.read_bytes()) for f in [*HERE.parent.glob('*.ptx'), HERE / 'generic_sm75.ptx'])
