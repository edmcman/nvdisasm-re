"""Selector and value checks for F2FP E2M3/E3M2 downconversion."""
import json
import math
import random
import struct
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
from compare_ghidra import GHIDRA_PY, LDEFS, ghidra
from f2fp_8b_downconvert_semantics import put
from semantic_cases import encode
from sass import ir, pydecode
from sass.gen_sleigh import Gen, variant
from test_dataflow import actual, expected as expected_dataflow, expected_flow


FORMATS = {'E2M3': (2, 3, 1), 'E3M2': (3, 2, 3)}


def decode(code, fmt):
    ebits, mbits, bias = FORMATS[fmt]
    exp = (code >> mbits) & ((1 << ebits) - 1)
    mant = code & ((1 << mbits) - 1)
    quantum = Fraction(2) ** (1 - bias - mbits)
    if exp == 0:
        return mant * quantum
    return Fraction((1 << mbits) + mant, 1 << mbits) * Fraction(2) ** (exp - bias)


def convert(bits, fmt, rounding, relu):
    value = struct.unpack('<f', bits.to_bytes(4, 'little'))[0]
    sign = bits >> 31
    max_code = 0x1f
    if math.isnan(value):
        return max_code
    if math.isinf(value):
        return 0 if relu and sign else (sign << 7) | max_code
    magnitude = abs(Fraction.from_float(value))
    if relu and sign:
        return 0
    candidates = [(code, decode(code, fmt)) for code in range(max_code + 1)]
    if magnitude >= candidates[-1][1]:
        code = max_code
    elif rounding == 'RN':
        distances = [(abs(magnitude - target), code) for code, target in candidates]
        nearest = min(distance for distance, _ in distances)
        if nearest == 0:
            code = next(code for distance, code in distances if distance == 0)
        else:
            tied = [code for distance, code in distances if distance == nearest]
            code = next((code for code in tied if (code & 1) == 0), tied[0])
    elif rounding == 'RP' and not sign:
        code = next(code for code, target in candidates if target >= magnitude)
    else:
        code = next(code for code, target in reversed(candidates) if target <= magnitude)
    return (sign << 7) | code


def src_as_f32(raw, source_fmt):
    if source_fmt == 'F32':
        return raw
    if source_fmt == 'BF16':
        return raw << 16
    value = struct.unpack('<e', raw.to_bytes(2, 'little'))[0]
    return int.from_bytes(struct.pack('<f', value), 'little')


def sample_bits(source_fmt, fmt):
    rng = random.Random((0xf600 + list(FORMATS).index(fmt) * 19 +
                         {'F32': 0, 'F16': 1, 'BF16': 2}[source_fmt]))
    if source_fmt == 'F32':
        samples = [0, 0x80000000, 1, 0x007fffff, 0x00800000,
                   0x7f800000, 0xff800000, 0x7fc00000, 0xffc12345]
        samples.extend(rng.getrandbits(32) for _ in range(14))
        for code in range(32):
            v = float(decode(code, fmt))
            samples.append(struct.unpack('<I', struct.pack('<f', v))[0])
            if code < 31:
                midpoint = float((decode(code, fmt) + decode(code + 1, fmt)) / 2)
                samples.append(struct.unpack('<I', struct.pack('<f', midpoint))[0])
        return sorted(set(samples))
    samples = [0, 0x8000, 1, 0x7c00, 0xfc00, 0x7e00, 0xfe00]
    for code in range(32):
        v = float(decode(code, fmt))
        try:
            raw = (struct.pack('<e', v) if source_fmt == 'F16' else
                   struct.pack('<f', v)[2:4])
            raw = int.from_bytes(raw, 'little')
            samples.append(raw)
            if code < 31:
                midpoint = float((decode(code, fmt) + decode(code + 1, fmt)) / 2)
                raw = (struct.pack('<e', midpoint) if source_fmt == 'F16' else
                       struct.pack('<f', midpoint)[2:4])
                samples.append(int.from_bytes(raw, 'little'))
        except OverflowError:
            pass
    samples.extend(rng.getrandbits(16) for _ in range(12))
    return sorted(set(samples))


def put_source(group, klass, fields, registers, reg, value, source_fmt):
    if source_fmt == 'F32':
        put(group, klass, fields, registers, reg, value)
    else:
        put(group, klass, fields, registers, reg, value)


def cases(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if 'to_8b_downconvert__' in k.name
               and not k.alternate and all(x in model.enums[k.operand_types['dstfmt'].type]
                                            for x in FORMATS)]
    for klass in classes:
        half = '_f16_to_8b_' in klass.name
        dest = 'URd' if klass.name.startswith('uf2fp_') else 'Rd'
        src_fmts = ('F16', 'BF16') if half else ('F32',)
        for source_fmt in src_fmts:
            for fmt in FORMATS:
                vals = sample_bits(source_fmt, fmt)
                rounds = ('RN', 'RZ', 'RP')
                for rounding in rounds:
                    for relu in ('norelu', 'RELU'):
                        for i in range(0, len(vals) - 1, 2):
                            x, y = vals[i], vals[i + 1]
                            carry = 0x9bd753e1 ^ (i * 0x01010101)
                            fields = dict(dstfmt=fmt, srcfmt=source_fmt,
                                          merge='UNPACK_B_MERGE_C' if half else 'PACK_AB_MERGE_C',
                                          rndMode=rounding, satfinite='SATFINITE', relu=relu,
                                          extract='H1', **{dest: 8})
                            regs = {}
                            if half:
                                pair = x | (y << 16)
                                put_source('Rb/URb/Sb', klass, fields, regs, 2, pair, source_fmt)
                            else:
                                put_source('Ra/URa', klass, fields, regs, 2, x, source_fmt)
                                put_source('Rb/URb/Sb', klass, fields, regs, 4, y, source_fmt)
                            put('Rc/URc/Sc', klass, fields, regs, 6, carry)
                            fx = src_as_f32(x, source_fmt)
                            fy = src_as_f32(y, source_fmt)
                            outx = convert(fx, fmt, rounding, relu == 'RELU')
                            outy = convert(fy, fmt, rounding, relu == 'RELU')
                            packed = (outy << 8) | outx if half else (outx << 8) | outy
                            expected = ((carry >> 16) & 0xffff) << 16 | packed
                            yield ({'word': encode(arch, klass.name, **fields),
                                    'registers': regs,
                                    'observe': [f'{"UR" if dest == "URd" else "R"}8'],
                                    'inspect_pcode': True}, expected, klass.name)


def run(arch, fp):
    todo = list(cases(arch))
    payload = ''.join(json.dumps(req) + '\n' for req, _, _ in todo)
    proc = subprocess.run([GHIDRA_PY, str(ROOT / 'tests/ghidra_emulate.py'), str(LDEFS),
                           f'SASS:LE:64:{variant(arch, fp)}'], input=payload,
                          capture_output=True, text=True)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
    if len(results) != len(todo):
        raise RuntimeError(proc.stderr[-3000:])
    failures = []
    for (req, want, klass), result in zip(todo, results):
        got = result.get('registers', {}).get(req['observe'][0])
        ops = result.get('pcode_ops', [])
        if got != want or not ops or 'CALLOTHER' in ops:
            failures.append((klass, req['word'], want, result))
            if len(failures) == 10:
                break
    print(f'{arch} {fp}: FP6 SATFINITE downconvert {len(todo)-len(failures)}/{len(todo)}')
    if failures:
        raise AssertionError(failures)


def check_dataflow(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if 'to_8b_downconvert__' in k.name
               and not k.alternate and all(x in model.enums[k.operand_types['dstfmt'].type]
                                            for x in FORMATS)]
    words = []
    for klass in classes:
        half = '_f16_to_8b_' in klass.name
        dest = 'URd' if klass.name.startswith('uf2fp_') else 'Rd'
        fields = dict(dstfmt='E2M3', srcfmt='F16' if half else 'F32',
                      merge='UNPACK_B_MERGE_C' if half else 'PACK_AB_MERGE_C',
                      rndMode='RN', satfinite='SATFINITE', relu='RELU',
                      extract='H1', **{dest: 8})
        regs = {}
        if half:
            put('Rb/URb/Sb', klass, fields, regs, 2, 0x3c003c00)
        else:
            put('Ra/URa', klass, fields, regs, 2, 0x3f800000)
            put('Rb/URb/Sb', klass, fields, regs, 4, 0x40000000)
        put('Rc/URc/Sc', klass, fields, regs, 6, 0xdeadbeef)
        raw = bytes.fromhex(encode(arch, klass.name, **fields))
        words.append((raw, pydecode.decode(arch, raw)))
    for fp in ('exact', 'readable'):
        results = ghidra(arch, [(raw, 0) for raw, _ in words], '--dataflow', fp=fp)
        gen = Gen(arch, fp)
        failures = []
        for (_, decoded), result in zip(words, results):
            optimized = not any((op.get('userop') or '').startswith(('sass_opaque_', 'sass_prim_'))
                                for op in result.get('ops', []))
            if ('error' in result or result['flow'] != expected_flow(gen, decoded) or
                    actual(result.get('ops', [])) != expected_dataflow(gen, decoded,
                                                                         optimized=optimized)):
                failures.append((decoded.klass.name, result))
        print(f'{arch} {fp}: FP6 downconvert dataflow {len(words)-len(failures)}/{len(words)}')
        if failures:
            raise AssertionError(failures[:5])


def check_nosatfinite_fallback(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if 'to_8b_downconvert__' in k.name
               and not k.alternate and all(x in model.enums[k.operand_types['dstfmt'].type]
                                            for x in FORMATS)]
    reqs = []
    for klass in classes:
        half = '_f16_to_8b_' in klass.name
        dest = 'URd' if klass.name.startswith('uf2fp_') else 'Rd'
        fields = dict(dstfmt='E2M3', srcfmt='F16' if half else 'F32',
                      merge='UNPACK_B_MERGE_C' if half else 'PACK_AB_MERGE_C',
                      rndMode='RN', satfinite='nosatfinite', relu='norelu',
                      extract='H0', **{dest: 8})
        regs = {}
        if half:
            put('Rb/URb/Sb', klass, fields, regs, 2, 0x3c003c00)
        else:
            put('Ra/URa', klass, fields, regs, 2, 0x3f800000)
            put('Rb/URb/Sb', klass, fields, regs, 4, 0x40000000)
        put('Rc/URc/Sc', klass, fields, regs, 6, 0xdeadbeef)
        reqs.append({'word': encode(arch, klass.name, **fields), 'registers': regs,
                     'observe': [f'{"UR" if dest == "URd" else "R"}8'],
                     'inspect_pcode': True})
    payload = ''.join(json.dumps(req) + '\n' for req in reqs)
    proc = subprocess.run([GHIDRA_PY, str(ROOT / 'tests/ghidra_emulate.py'), str(LDEFS),
                           f'SASS:LE:64:{variant(arch, "exact")}'], input=payload,
                          capture_output=True, text=True)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
    if len(results) != len(reqs):
        raise RuntimeError(proc.stderr[-3000:])
    failures = [(req['word'], result) for req, result in zip(reqs, results)
                if not (result.get('error') or '').startswith('sass_opaque_F2FP(')]
    print(f'{arch}: FP6 nosatfinite opaque fallback {len(reqs)-len(failures)}/{len(reqs)}')
    if failures:
        raise AssertionError(failures[:5])


if __name__ == '__main__':
    arch = sys.argv[1] if len(sys.argv) > 1 else 'SM100'
    modes = sys.argv[2:] or ('exact', 'readable')
    for mode in modes:
        run(arch, mode)
    check_dataflow(arch)
    check_nosatfinite_fallback(arch)
