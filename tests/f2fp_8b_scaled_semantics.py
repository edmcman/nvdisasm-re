"""Exhaustive 8-bit scaled F2FP-to-BF16 semantic checks."""
import json
import math
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
from compare_ghidra import GHIDRA_PY, LDEFS
from semantic_cases import encode
from sass import ir
from sass.gen_sleigh import variant

FORMATS = ('E5M2', 'E4M3', 'E2M3', 'E3M2', 'E3M4', 'S2_6')


def source_value(code, fmt):
    if fmt == 'S2_6':
        signed = code if code < 128 else code - 256
        return signed / 64
    if fmt == 'E3M4':
        sign = -1 if code & 0x80 else 1
        exp, mant = (code >> 4) & 7, code & 15
        if exp == 7 and mant == 15:
            return math.nan
        mag = mant / 64 if exp == 0 else (1 + mant / 16) * 2 ** (exp - 3)
        return sign * mag
    if fmt in ('E2M3', 'E3M2'):
        sign = -1 if code & 0x20 else 1
        ebits, mbits, bias = (2, 3, 1) if fmt == 'E2M3' else (3, 2, 3)
        exp, mant = (code >> mbits) & ((1 << ebits) - 1), code & ((1 << mbits) - 1)
        mag = mant * 2 ** (1 - bias - mbits) if exp == 0 else (1 + mant / (1 << mbits)) * 2 ** (exp - bias)
        return sign * mag
    sign = -1 if code & 0x80 else 1
    exp, mant = ((code >> 2) & 31, code & 3) if fmt == 'E5M2' else ((code >> 3) & 15, code & 7)
    ebits, mbits, bias = (5, 2, 15) if fmt == 'E5M2' else (4, 3, 7)
    if fmt == 'E5M2' and exp == 31:
        return math.inf * sign if mant == 0 else math.nan
    if fmt == 'E4M3' and exp == 15 and mant == 7:
        return math.nan
    mag = mant * 2 ** (1 - bias - mbits) if exp == 0 else (1 + mant / (1 << mbits)) * 2 ** (exp - bias)
    return sign * mag


def scaled_bf16(code, fmt, scale, relu, satfinite):
    value = source_value(code, fmt)
    factor = math.nan if scale == 255 else 2.0 ** (-127 if scale == 0 else scale - 127)
    if math.isnan(value) or math.isnan(factor):
        out = 0x7fff
    else:
        scaled = value * factor
        sign = 0x80000000 if math.copysign(1.0, scaled) < 0 else 0
        try:
            bits = int.from_bytes(struct.pack('<f', scaled), 'little')
        except OverflowError:
            bits = sign | 0x7f800000
        exp, frac = bits & 0x7f800000, bits & 0x007fffff
        if exp == 0x7f800000 and frac:
            out = 0x7fff
        else:
            hi, lo = bits >> 16, bits & 0xffff
            if lo > 0x8000 or (lo == 0x8000 and hi & 1):
                hi += 1
            out = hi & 0xffff
    if satfinite and (out & 0x7fff) == 0x7f80:
        out = (out & 0x8000) | 0x7f7f
    if relu and (out & 0x8000) and not ((out & 0x7f80) == 0x7f80 and (out & 0x7f)):
        out = 0
    return out


def cases(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if k.name.startswith('f2fp_8b_upconvert_scale__') and not k.alternate]
    for klass in classes:
        bsrc = next(n for n in ('Rb', 'URb', 'Sb') if n in klass.operand_types)
        csrc = next(n for n in ('Rc', 'URc', 'Sc') if n in klass.operand_types)
        btype, ctype = klass.operand_types[bsrc].type, klass.operand_types[csrc].type
        for fmt in FORMATS:
            if fmt not in model.enums[klass.operand_types['srcfmt'].type]:
                continue
            for extract in ('H0', 'H1'):
                for relu in ('norelu', 'RELU'):
                    for satfinite in ('nosatfinite', 'SATFINITE'):
                        for i in range(256):
                            # Sweep every byte encoding and every scale encoding in
                            # each lane; the second lane uses a different permutation.
                            code0, scale0 = i, (i * 73 + 19) & 0xff
                            code1, scale1 = (i * 29 + 7) & 0xff, 255 - scale0
                            low = code0 | (code1 << 8)
                            scales = scale0 | (scale1 << 8)
                            # Deliberately place different data in the unselected half.
                            other_bytes = ((i ^ 0xa5) & 0xff) | (((i ^ 0x5a) & 0xff) << 8)
                            other_scales = ((i * 11 + 3) & 0xff) | (((i * 17 + 9) & 0xff) << 8)
                            bword = low | (other_bytes << 16) if extract == 'H0' else (other_bytes | (low << 16))
                            cword = scales | (other_scales << 16) if extract == 'H0' else (other_scales | (scales << 16))
                            fields = dict(Rd=8, dstfmt='BF16', srcfmt=fmt, merge='UNPACK_B',
                                          rndMode='RN', relu=relu, satfinite=satfinite, extract_B=extract)
                            registers = {}
                            if btype == 'Register':
                                fields[bsrc] = 2
                                registers['R2'] = bword
                            elif btype == 'UniformRegister':
                                fields[bsrc] = 2
                                registers['UR2'] = bword
                            else:
                                fields[bsrc] = bword
                            if ctype == 'Register':
                                fields[csrc] = 4
                                registers['R4'] = cword
                            elif ctype == 'UniformRegister':
                                fields[csrc] = 4
                                registers['UR4'] = cword
                            else:
                                fields[csrc] = cword
                            word = encode(arch, klass.name, **fields)
                            expected = (scaled_bf16(code0, fmt, scale0, relu == 'RELU', satfinite == 'SATFINITE') |
                                        (scaled_bf16(code1, fmt, scale1, relu == 'RELU', satfinite == 'SATFINITE') << 16))
                            yield dict(word=word, registers=registers, observe=['R8'], inspect_pcode=True), expected, klass.name


def run(arch, fp):
    todo = list(cases(arch))
    payload = ''.join(json.dumps(request) + '\n' for request, _, _ in todo)
    proc = subprocess.run([GHIDRA_PY, str(ROOT / 'tests/ghidra_emulate.py'), str(LDEFS),
                           f'SASS:LE:64:{variant(arch, fp)}'], input=payload,
                          capture_output=True, text=True)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
    if len(results) != len(todo):
        raise RuntimeError(proc.stderr[-3000:])
    failures = []
    for (request, expected, klass), result in zip(todo, results):
        pcode = result.get('pcode_ops', [])
        if result.get('registers') != {'R8': expected} or not pcode or 'CALLOTHER' in pcode:
            failures.append((klass, request['word'], expected, result.get('registers'), result.get('error')))
            if len(failures) == 10:
                break
    print(f'{arch} {fp}: scaled 8-bit cases {len(todo) - len(failures)}/{len(todo)}')
    if failures:
        raise AssertionError(failures)


if __name__ == '__main__':
    arch = sys.argv[1] if len(sys.argv) > 1 else 'SM100'
    modes = sys.argv[2:] or ['exact', 'readable']
    for mode in modes:
        run(arch, mode)
