"""Name ORI opcodes by compiling one PTX instruction per kernel (CUDA 13.0.88 ptxas).

Each kernel loads typed parameters, applies one instruction and stores the result.
Its initial ORI (before OriCheckInitialProgram) minus the ORI of a baseline kernel
with the same signature (same loads and store, d left unset) leaves the opcodes of
that instruction. Complex instructions (div, rcp, sqrt, ...) are already expanded.

  python3 research/ptxas/ori_opcodes.py OUT_DIR    -> OUT_DIR/ori_opcodes.json, .md
"""
import collections, json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PTXAS = '/usr/local/cuda-13.0/bin/ptxas'
PRINTER = Path(__file__).with_name('ori_print.py')
ARCH = 'sm_100'

I, U, S, L, SL = 'u32', 'u32', 's32', 'u64', 's64'
F, D, H, H2, B, P = 'f32', 'f64', 'b16', 'b32', 'b32', 'pred'
# (instruction, destination type, source types); '%d' etc. are appended automatically
SPECS = [
    ('add.u32', U, [U, U]), ('add.s64', SL, [SL, SL]), ('sub.u32', U, [U, U]),
    ('add.sat.s32', S, [S, S]), ('sub.sat.s32', S, [S, S]),
    ('mul.lo.u32', U, [U, U]), ('mul.hi.u32', U, [U, U]), ('mul.hi.s32', S, [S, S]),
    ('mul.wide.u32', L, [U, U]), ('mul.wide.s32', SL, [S, S]), ('mul.lo.u64', L, [L, L]),
    ('mul.hi.u64', L, [L, L]), ('mad.lo.u32', U, [U, U, U]), ('mad.hi.u32', U, [U, U, U]),
    ('mad.wide.u32', L, [U, U, L]), ('mul24.lo.u32', U, [U, U]), ('mad24.lo.u32', U, [U, U, U]),
    ('sad.u32', U, [U, U, U]), ('div.u32', U, [U, U]), ('div.s32', S, [S, S]),
    ('rem.u32', U, [U, U]), ('div.u64', L, [L, L]), ('abs.s32', S, [S]), ('neg.s32', S, [S]),
    ('min.u32', U, [U, U]), ('max.s32', S, [S, S]), ('min.s64', SL, [SL, SL]),
    ('popc.b32', U, [B]), ('popc.b64', U, ['b64']), ('clz.b32', U, [B]), ('bfind.u32', U, [U]),
    ('bfind.shiftamt.s32', U, [S]), ('brev.b32', B, [B]), ('bfe.u32', U, [U, U, U]),
    ('bfe.s32', S, [S, U, U]), ('bfi.b32', B, [B, B, U, U]), ('fns.b32', B, [B, U, S]),
    ('szext.clamp.s32', S, [S, U]), ('bmsk.clamp.b32', B, [U, U]),
    ('dp4a.u32.u32', U, [U, U, U]), ('dp2a.lo.u32.u32', U, [U, U, U]),
    ('and.b32', B, [B, B]), ('or.b32', B, [B, B]), ('xor.b32', B, [B, B]), ('not.b32', B, [B]),
    ('cnot.b32', B, [B]), ('and.b64', 'b64', ['b64', 'b64']),
    ('shl.b32', B, [B, U]), ('shr.u32', U, [U, U]), ('shr.s32', S, [S, U]), ('shl.b64', 'b64', ['b64', U]),
    ('shf.l.wrap.b32', B, [B, B, U]), ('shf.r.clamp.b32', B, [B, B, U]),
    ('prmt.b32', B, [B, B, B]), ('selp.b32', B, [B, B, P]), ('slct.u32.s32', U, [U, U, S]),
    ('setp.lt.u32', P, [U, U]), ('setp.eq.s64', P, [SL, SL]), ('setp.lt.f32', P, [F, F]),
    ('setp.ltu.f64', P, [D, D]), ('set.lt.u32.u32', U, [U, U]),
    ('add.rn.f32', F, [F, F]), ('add.rz.f32', F, [F, F]), ('sub.f32', F, [F, F]),
    ('mul.rn.f32', F, [F, F]), ('fma.rn.f32', F, [F, F, F]), ('mad.rn.f32', F, [F, F, F]),
    ('add.ftz.sat.f32', F, [F, F]), ('div.rn.f32', F, [F, F]), ('div.approx.f32', F, [F, F]),
    ('div.full.f32', F, [F, F]), ('rcp.rn.f32', F, [F]), ('rcp.approx.f32', F, [F]),
    ('sqrt.rn.f32', F, [F]), ('sqrt.approx.f32', F, [F]), ('rsqrt.approx.f32', F, [F]),
    ('sin.approx.f32', F, [F]), ('cos.approx.f32', F, [F]), ('lg2.approx.f32', F, [F]),
    ('ex2.approx.f32', F, [F]), ('tanh.approx.f32', F, [F]), ('abs.f32', F, [F]), ('neg.f32', F, [F]),
    ('min.f32', F, [F, F]), ('max.NaN.f32', F, [F, F]), ('copysign.f32', F, [F, F]),
    ('testp.finite.f32', P, [F]),
    ('add.rn.f64', D, [D, D]), ('mul.rn.f64', D, [D, D]), ('fma.rn.f64', D, [D, D, D]),
    ('div.rn.f64', D, [D, D]), ('rcp.rn.f64', D, [D]), ('sqrt.rn.f64', D, [D]),
    ('rsqrt.approx.f64', D, [D]), ('min.f64', D, [D, D]), ('abs.f64', D, [D]),
    ('add.rn.f16x2', H2, [H2, H2]), ('fma.rn.f16x2', H2, [H2, H2, H2]), ('add.rn.f16', H, [H, H]),
    ('add.rn.bf16x2', H2, [H2, H2]), ('fma.rn.relu.f16x2', H2, [H2, H2, H2]),
    ('cvt.rn.f32.s32', F, [S]), ('cvt.rn.f32.u64', F, [L]), ('cvt.rzi.s32.f32', S, [F]),
    ('cvt.rni.u64.f64', L, [D]), ('cvt.f64.f32', D, [F]), ('cvt.rn.f32.f64', F, [D]),
    ('cvt.u64.u32', L, [U]), ('cvt.s64.s32', SL, [S]), ('cvt.u32.u64', U, [L]),
    ('cvt.s32.s8', S, [S]), ('cvt.u16.u32', 'u16', [U]), ('cvt.rn.f16.f32', H, [F]),
    ('cvt.f32.f16', F, [H]), ('cvt.rn.f16x2.f32', H2, [F, F]), ('cvt.rn.bf16x2.f32', H2, [F, F]),
    ('cvt.rni.f32.f32', F, [F]), ('cvt.sat.f32.f32', F, [F]),
    ('mov.b64', 'b64', [L]), ('mov.b32', B, [B]),
]
# Whole-body specs: name -> (destination type, source types, body using %d %a %b %c %po)
BODIES = {
    'ld.global.u32': (U, [L], 'ld.global.u32 %d, [%a];'),
    'ld.shared.u32': (U, [U], 'ld.shared.u32 %d, [%a];'),
    'ld.local.u32': (U, [U], 'ld.local.u32 %d, [%a];'),
    'ld.global.nc.u32': (U, [L], 'ld.global.nc.u32 %d, [%a];'),
    'ld.const.u32': (U, [U], 'ld.const.u32 %d, [%a];'),
    'st.shared.u32': (U, [U, U], 'st.shared.u32 [%a], %b; mov.u32 %d, %b;'),
    'atom.global.add.u32': (U, [L, U], 'atom.global.add.u32 %d, [%a], %b;'),
    'atom.global.cas.b32': (B, [L, B, B], 'atom.global.cas.b32 %d, [%a], %b, %c;'),
    'atom.shared.exch.b32': (B, [U, B], 'atom.shared.exch.b32 %d, [%a], %b;'),
    'red.global.add.f32': (F, [L, F], 'red.global.add.f32 [%a], %b; mov.f32 %d, %b;'),
    'shfl.sync.bfly.b32': (B, [B, U], 'shfl.sync.bfly.b32 %d, %a, %b, 31, -1;'),
    'vote.sync.ballot.b32': (B, [P], 'vote.sync.ballot.b32 %d, %a, -1;'),
    'redux.sync.add.u32': (U, [U], 'redux.sync.add.u32 %d, %a, -1;'),
    'activemask.b32': (B, [B], 'activemask.b32 %d;'),
    'bar.sync': (U, [U], 'bar.sync 0; mov.u32 %d, %a;'),
    'membar.gl': (U, [U], 'membar.gl; mov.u32 %d, %a;'),
    'fence.acq_rel.gpu': (U, [U], 'fence.acq_rel.gpu; mov.u32 %d, %a;'),
    'mov.u32 %tid.x': (U, [U], 'mov.u32 %d, %tid.x;'),
    'mov.u32 %ctaid.x': (U, [U], 'mov.u32 %d, %ctaid.x;'),
    'mov.u32 %laneid': (U, [U], 'mov.u32 %d, %laneid;'),
    'mov.u64 %clock64': (L, [L], 'mov.u64 %d, %clock64;'),
    'add.cc+addc.u32': (U, [U, U], '{ .reg .u32 t; add.cc.u32 t, %a, %b; addc.u32 %d, %a, %b; }'),
    'sub.cc+subc.u32': (U, [U, U], '{ .reg .u32 t; sub.cc.u32 t, %a, %b; subc.u32 %d, %a, %b; }'),
    'mad.cc+madc.lo.u32': (U, [U, U, U], '{ .reg .u32 t; mad.lo.cc.u32 t, %a, %b, %c; madc.hi.u32 %d, %a, %b, %c; }'),
    'lop3.b32': (B, [B, B, B], 'lop3.b32 %d, %a, %b, %c, 0x96;'),
    'bra': (U, [U], '{ .reg .pred q; setp.eq.u32 q, %a, 0; mov.u32 %d, 1; @q bra L1; mov.u32 %d, %a; L1: }'),
}

SRC = 'abce'  # source register names; %d is the destination
PARAM = {P: 'u32', 'u16': 'u16'}
REG = {H2: 'b32', P: 'pred'}


def kernel(dt, srcs, body):
    names = SRC[:len(srcs)]
    params = ', '.join(f'.param .{PARAM.get(t, t)} p{n}' for n, t in zip(names, srcs))
    regs = [f'.reg .{REG.get(t, t)} %{n};' for n, t in zip(names, srcs)] + [f'.reg .{REG.get(dt, dt)} %d;']
    loads = []
    for n, t in zip(names, srcs):
        if t == P:
            loads += [f'{{ .reg .u32 t; ld.param.u32 t, [p{n}]; setp.ne.u32 %{n}, t, 0; }}']
        else:
            loads += [f'ld.param.{PARAM.get(t, t)} %{n}, [p{n}];']
    store = ('{ .reg .u32 t; selp.u32 t, 1, 0, %d; st.global.u32 [%po], t; }' if dt == P
             else f'st.global.{PARAM.get(dt, dt)} [%po], %d;')
    return '\n'.join(['.version 8.7', f'.target {ARCH}', '.address_size 64',
                      f'.visible .entry k({params}, .param .u64 po) {{',
                      *regs, '.reg .u64 %po;', *loads, 'ld.param.u64 %po, [po];',
                      body, store, 'ret;', '}', ''])


def spec_body(instr, dt, srcs):
    return f'{instr} %d, {", ".join("%" + n for n in SRC[:len(srcs)])};'


OP_RE = re.compile(r'^\s*(?:(?P<defs>.*?) = )?op_(?P<op>[0-9a-f]+)(?P<flags>/[0-9a-f]+)?\.t(?P<ty>\d+)')


def ori(ptx, work):
    src = work / 'k.ptx'
    src.write_text(ptx)
    env = dict(os.environ, ORI_PHASES='OriCheckInitialProgram', ORI_WHEN='before', ORI_RAW='1',
               ORI_OUT=str(work / 'k.ori'))
    run = subprocess.run(['gdb', '-q', '-batch', '-x', str(PRINTER), '--args', PTXAS, f'-arch={ARCH}',
                          '-o', str(work / 'k.cubin'), str(src)], env=env, capture_output=True, text=True)
    err = re.findall(r'ptxas (?:fatal|error)\s*: .*', run.stdout + run.stderr)
    text = (work / 'k.ori').read_text() if (work / 'k.ori').exists() else ''
    return text, err


def ops(text):
    out = []
    for line in text.splitlines():
        m = OP_RE.match(line)
        if m: out.append((int(m['op'], 16), int(m['ty']), m['flags'] or '',
                          re.sub(r'%[0-9a-f]+(=\w+)?', '%', line.strip()), line.strip()))
    return out


def main(outdir):
    outdir = Path(outdir)
    jobs = {name: (dt, srcs, spec_body(name, dt, srcs)) for name, dt, srcs in SPECS}
    jobs.update(BODIES)
    sigs = {(dt, tuple(srcs)) for dt, srcs, _ in jobs.values()}
    tasks = {('base', s): kernel(s[0], list(s[1]), '') for s in sigs}
    tasks.update({('insn', n): kernel(dt, srcs, body) for n, (dt, srcs, body) in jobs.items()})

    def go(item):
        key, ptx = item
        work = outdir / 'work' / re.sub(r'[^\w.+-]', '_', '_'.join(map(str, key)))
        work.mkdir(parents=True, exist_ok=True)
        return key, ori(ptx, work)

    with ThreadPoolExecutor(int(os.environ.get('JOBS', '16'))) as pool:
        results = dict(pool.map(go, tasks.items()))
    table = {}
    for name, (dt, srcs, body) in jobs.items():
        text, err = results[('insn', name)]
        ref = collections.Counter(o[:4] for o in ops(results[('base', (dt, tuple(srcs)))][0]))
        extra = []
        for o in ops(text):
            if ref[o[:4]]: ref[o[:4]] -= 1
            else: extra.append(o)
        table[name] = dict(body=body, errors=err, ops=[dict(op=hex(o), type=t, flags=f, line=l) for o, t, f, _, l in extra])
    (outdir / 'ori_opcodes.json').write_text(json.dumps(table, indent=1) + '\n')
    byop = collections.defaultdict(list)
    for name, r in table.items():
        for o in r['ops']: byop[o['op']].append(f'{name} (t{o["type"]}{o["flags"]})')
    lines = ['| ORI op | PTX instructions (type) |', '|---|---|']
    lines += [f'| {op} | {"; ".join(v)} |' for op, v in sorted(byop.items(), key=lambda x: int(x[0], 16))]
    (outdir / 'ori_opcodes.md').write_text('\n'.join(lines) + '\n')
    print(f'{len(table)} instructions, {len(byop)} distinct ORI opcodes; '
          f'{sum(bool(r["errors"]) for r in table.values())} with compile errors')


if __name__ == '__main__':
    main(sys.argv[1])
