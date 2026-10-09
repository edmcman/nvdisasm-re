"""Print ptxas's internal ORI IR at phase boundaries (CUDA 13.0.88 ptxas, non-PIE).

Release ptxas has no working IR dump (DUMPIR's consumers and the Report* phases
are compiled out), so this walks the IR from the phase dispatch loop instead.

  ORI_PHASES=ConvertUnsupportedOps ORI_WHEN=both ORI_OUT=/tmp/ori.txt \
    gdb -q -batch -x research/ptxas/ori_print.py \
        --args /usr/local/cuda-13.0/bin/ptxas -arch=sm_100 -o k.cubin k.ptx

ORI_PHASES: comma-separated phase names, 'all', or 'list' (print phase order only).
ORI_WHEN: before | after | both (default both).
ORI_RAW: print every opcode as op_<hex> and every type as tN (used by ori_opcodes.py).
"""
import json
import os
import gdb

EXEC_CALL = 0xC6508D    # dispatch loop: call phase->execute(rdi=phase, rsi=ctx), r13 = phase name
EXEC_RET = 0xC6508F

# Operand word: bit 31 = definition, bits 28-30 = kind, bits 0-23 = id/value.
# Modifier word: 0x80000000 negate, 0x40000000 absolute, 0x20000000 complement,
# 0x0100000N memory in space N.
KIND = {1: '%', 2: 'sym', 3: 'k3_', 4: 'lbl', 5: 'const', 6: 'imm', 7: 'none'}
# Virtual register class (descriptor+0x40) -> physical file, checked against final SASS.
REGFILE = {2: 'UP', 3: 'UR', 5: 'P', 6: 'R', 9: 'SV'}  # SV: fixed system-value vregs (id = own number)
# Type field (insn+0x4c) from the opcode corpora; 32-bit sign-agnostic ops (add, mul.lo) use s32,
# and constant-bank loads use f32 for any 32-bit value.
# 1 (control/void) prints nothing; unlisted codes print as tN.
TYPE = {} if os.environ.get('ORI_RAW') else {1: '', 6: 'f32', 7: 'f16', 9: 's64', 10: 'u64', 11: 's32', 12: 'u32', 19: 'f64', 20: 'pred',
        26: 'cc', 31: 'bf16'}
# Evidence-backed opcode names (ori_opnames.json, produced with ori_opcodes.py).
OPNAME = {} if os.environ.get('ORI_RAW') else {
    int(op, 16): v['name'] for op, v in json.load(open(os.path.join(os.path.dirname(__file__), 'ori_opnames.json'))).items()}

inf = gdb.selected_inferior()
u32 = lambda a: int.from_bytes(inf.read_memory(a, 4).tobytes(), 'little')
u64 = lambda a: int.from_bytes(inf.read_memory(a, 8).tobytes(), 'little')
cstr = lambda a: inf.read_memory(a, 96).tobytes().split(b'\0')[0].decode()

phases = os.environ.get('ORI_PHASES', 'ConvertUnsupportedOps')
want = None if phases in ('all', 'list') else set(phases.split(','))
when = os.environ.get('ORI_WHEN', 'both')
out = open(os.environ['ORI_OUT'], 'w') if 'ORI_OUT' in os.environ else None
emit = lambda s: print(s, file=out) if out else gdb.write(s + '\n')


def vreg(ctx, vid):
    v = u64(u64(ctx + 0x58) + 8 * vid)
    cls, phys = u32(v + 0x40), u32(v + 0x44)
    loc = '' if phys == 0xFFFFFFFF else f'={REGFILE.get(cls, f"c{cls}:")}{phys}'
    return f'%{vid:x}{loc}'


def operand(ctx, word, mod):
    kind, val = word >> 28 & 7, word & 0xFFFFFF
    if kind == 7: return '_' if word >> 31 else 'none'
    text = vreg(ctx, val) if kind == 1 else f'{KIND.get(kind, f"k{kind}_")}{val:x}'
    if mod & 0x40000000: text = f'|{text}|'
    if mod & 0x80000000: text = '-' + text
    if mod & 0x20000000: text = '~' + text
    if mod & 0x01000000: text = f'[{text}]s{mod & 0xFF}'
    rest = mod & ~0xE10000FF
    return text + (f'{{{rest:x}}}' if rest else '')


def dump(ctx, label):
    emit(f'==== {label}')
    insn = u64(ctx + 0x110)
    while insn:
        op, ty, n = u32(insn + 0x48), u32(insn + 0x4C), u32(insn + 0x50)
        words = [(u32(insn + 0x54 + 8 * j), u32(insn + 0x58 + 8 * j)) for j in range(n)]
        defs = [operand(ctx, w, m) for w, m in words if w >> 31]
        uses = [operand(ctx, w, m) for w, m in words if not w >> 31]
        name = OPNAME.get(op & 0xFFFFCFFF, f'op_{op & 0xFFFFCFFF:x}')
        flags = f'/{op & 0x3000:x}' if op & 0x3000 else ''
        tyname = TYPE.get(ty, f't{ty}')
        emit(f'  {", ".join(defs) + " = " if defs else ""}{name}{flags}{"." + tyname if tyname else ""} {", ".join(uses)}')
        insn = u64(insn + 8)


class PhaseExec(gdb.Breakpoint):
    pending = None

    def stop(self):
        name, ctx = cstr(int(gdb.parse_and_eval('$r13'))), int(gdb.parse_and_eval('$rsi'))
        if phases == 'list': emit(name)
        elif want is None or name in want:
            if when in ('before', 'both'): dump(ctx, f'Before {name}')
            if when in ('after', 'both'): PhaseExec.pending = (ctx, name)
        return False


class PhaseRet(gdb.Breakpoint):
    def stop(self):
        if PhaseExec.pending:
            ctx, name = PhaseExec.pending
            PhaseExec.pending = None
            dump(ctx, f'After {name}')
        return False


PhaseExec(f'*{EXEC_CALL:#x}')
PhaseRet(f'*{EXEC_RET:#x}')
gdb.execute('run')
if out: out.close()
