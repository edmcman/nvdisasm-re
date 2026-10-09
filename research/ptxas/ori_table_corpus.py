"""Name ORI opcodes from ptxas's own PTX instruction registry.

instruction_table.tsv (GrigoryEvko/crucible-notes, decoded/ptxas-instr-defs) lists
every registered PTX instruction with its type signature and per-operand classes.
For each row this builds one kernel: every register operand is declared, loaded
from a parameter and stored afterwards, so destinations and sources need not be
distinguished. Missing modifiers (rounding, comparison, .sync, ...) are found by
trial compilation. Accepted kernels go through ori_opcodes.run_corpus.

  python3 research/ptxas/ori_table_corpus.py instruction_table.tsv OUT_DIR
"""
import csv, itertools, os, re, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from ori_opcodes import PTXAS, ARCH, run_corpus

# signature token -> representative PTX types (register class follows from the type)
TYPES = {'F16': ['f16'], 'F32': ['f32'], 'F64': ['f64'], 'H32': ['f16x2'], 'E16': ['bf16'], 'E32': ['bf16x2'],
         'T32': ['tf32'], 'P': ['pred'], 'I': ['s32', 'u64'], 'B': ['b32', 'b64'], 'F': ['f32', 'f64'],
         'I8': ['s8'], 'I16': ['s16'], 'I32': ['s32'], 'I64': ['s64'], 'B8': ['b8'], 'B16': ['b16'],
         'B32': ['b32'], 'B64': ['b64'], 'B128': ['b128'], 'U32': ['u32'], 'U64': ['u64']}
# fixed operand classes (datatype_sig letters other than 0/1/2)
FIXED = {'u': 'u32', 'd': 'b32', 'f': 'f32', 's': 's32', 'l': 'u64', 'P': 'pred'}
REG = {'f16': 'b16', 'bf16': 'b16', 'f16x2': 'b32', 'bf16x2': 'b32', 'tf32': 'b32', 's8': 's16', 'b8': 'b16'}
PARAM = {'pred': 'u32', 's8': 's16', 'b8': 'b16'}
MODIFIERS = ['', '.rn', '.approx', '.lt', '.sync', '.full', '.rn.ftz', '.approx.ftz', '.rzi', '.lo', '.wide',
             '.sync.bfly', '.sync.ballot', '.sync.all', '.sync.aligned', '.global', '.shared', '.add',
             '.lt.and', '.l', '.r', '.clamp', '.wrap', '.gpu', '.sc.gpu', '.relaxed.gpu', '.cta', '.all',
             '.shiftamt', '.f4e', '.lt.u32.u32']


def expand(sig):
    """'F[16|32|64]I[8|16|32|64]' -> type slots, each a list of candidates."""
    slots = []
    for tok in re.findall(r'[A-Z]\[[\d|]+\]|[A-Z]\d*', sig):
        if '[' in tok:
            letter, widths = tok[0], tok[2:-1].split('|')
            cands = [c for w in widths for c in TYPES.get(f'{letter}{w}', [])]
            slots.append([c for c in cands if c.endswith(('32', '64'))][:2] or cands[:1])
        elif tok in TYPES: slots.append(TYPES[tok])
        else: return None
    return slots


def operands(dsig, types):
    """Per-operand (kind, ptx type) from datatype_sig; None if a class is unsupported."""
    out = []
    for c in dsig:
        if c.isdigit():
            if int(c) >= len(types): return None
            out.append(('reg', types[int(c)]))
        elif c in FIXED: out.append(('reg', FIXED[c]))
        elif c == 'M': out.append(('mem', None))
        elif c == 'C': out.append(('imm', None))
        elif c == 'U': continue
        else: return None
    return out


def kernel(ops_, insn):
    regs = [(i, t) for i, (k, t) in enumerate(ops_) if k == 'reg']
    lines = ['.version 8.7', f'.target {ARCH}', '.address_size 64',
             '.visible .entry k(' + ', '.join([f'.param .{PARAM.get(t, REG.get(t, t))} p{i}' for i, t in regs]
                                              + ['.param .u64 pm', '.param .u64 po']) + ') {']
    lines += [f'.reg .{"pred" if t == "pred" else REG.get(t, t)} %r{i};' for i, t in regs]
    lines += ['.reg .u64 %pm, %po;', 'ld.param.u64 %pm, [pm];', 'ld.param.u64 %po, [po];']
    for i, t in regs:
        if t == 'pred': lines.append(f'{{ .reg .u32 t; ld.param.u32 t, [p{i}]; setp.ne.u32 %r{i}, t, 0; }}')
        else: lines.append(f'ld.param.{PARAM.get(t, REG.get(t, t))} %r{i}, [p{i}];')
    if insn: lines.append(insn)
    for n, (i, t) in enumerate(regs):
        if t == 'pred': lines.append(f'{{ .reg .u32 t; selp.u32 t, 1, 0, %r{i}; st.global.u32 [%po+{8 * n}], t; }}')
        else: lines.append(f'st.global.{PARAM.get(t, REG.get(t, t))} [%po+{8 * n}], %r{i};')
    return '\n'.join(lines + ['ret;', '}', ''])


def text(name, types, ops_, mod):
    args = ['[%pm]' if k == 'mem' else '1' if k == 'imm' else f'%r{i}' for i, (k, _) in enumerate(ops_)]
    suffix = ''.join('.' + t for t in types if t != 'pred')
    return f'{name}{mod}{suffix} {", ".join(args)};'


def compiles(ptx):
    with tempfile.NamedTemporaryFile('w', suffix='.ptx', delete=False) as f:
        f.write(ptx)
    try:
        return subprocess.run([PTXAS, f'-arch={ARCH}', '-o', os.devnull, f.name],
                              capture_output=True, timeout=60).returncode == 0
    finally:
        os.unlink(f.name)


def candidates(rows):
    seen = set()
    for r in rows:
        if r['name'].startswith(('#', '_')): continue
        slots = expand(r['operand_type_signature'])
        if slots is None: continue
        for types in itertools.islice(itertools.product(*slots), 2):
            ops_ = operands(r['datatype_sig'], types)
            if ops_ is None or (r['name'], types, r['datatype_sig']) in seen: continue
            seen.add((r['name'], types, r['datatype_sig']))
            yield r['name'], types, ops_


def accept(cand):
    name, types, ops_ = cand
    for mod in MODIFIERS:
        insn = text(name, types, ops_, mod)
        if compiles(kernel(ops_, insn)): return insn, ops_
    return None


def main(tsv, outdir):
    rows = list(csv.DictReader(open(tsv), delimiter='\t'))
    cands = list(candidates(rows))
    with ThreadPoolExecutor(int(os.environ.get('JOBS', '16'))) as pool:
        accepted = [a for a in pool.map(accept, cands) if a]
    print(f'{len(cands)} candidate forms, {len(accepted)} compile', flush=True)
    jobs, bases = {}, {}
    for insn, ops_ in accepted:
        key = tuple(ops_)
        bases[key] = kernel(ops_, '')
        jobs[insn] = (insn, kernel(ops_, insn), key)
    run_corpus(outdir, jobs, bases)


if __name__ == '__main__':
    main(*sys.argv[1:])
