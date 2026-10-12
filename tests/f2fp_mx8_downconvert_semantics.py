"""Scaled F32/BF16-to-S2_6 F2FP checks for every SM100+ source shape."""
import json
import math
import struct
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
from compare_ghidra import GHIDRA_PY, LDEFS, ghidra
from semantic_cases import encode
from sass import ir, pydecode
from sass.gen_sleigh import Gen, variant
from test_dataflow import actual, expected as expected_dataflow, expected_flow


FLOATS = (0x00000000, 0x80000000, 0x3f000000, 0x3fc00000,
          0xbfc00000, 0x3f400000, 0xbf400000, 0x3f600000,
          0xbf600000, 0x3f800000, 0xbf800000, 0x3ffe0000,
          0xbffe0000, 0x40000000, 0xc0000000, 0x3c000000,
          0xbc000000, 0x3cc00000, 0xbcc00000, 0x3d200000,
          0xbd200000, 0x7e800000, 0xfe800000, 0x7f000000,
          0xff000000, 0x7f7fffff, 0xff7fffff, 0x7f800000,
          0xff800000, 0x7fc12345, 0xffc54321)
SCALES = (0, 1, 126, 127, 128, 253, 254, 255)
SCALE_PAIRS = tuple((scale, SCALES[(i + 3) % len(SCALES)])
                    for i, scale in enumerate(SCALES)) + tuple((s, s) for s in SCALES)


def as_fraction(bits):
    value = struct.unpack('<f', bits.to_bytes(4, 'little'))[0]
    return value, None if not math.isfinite(value) else Fraction.from_float(value)


def round_even(value):
    q, r = divmod(abs(value.numerator), value.denominator)
    if 2 * r > value.denominator or (2 * r == value.denominator and q & 1):
        q += 1
    return -q if value < 0 else q


def convert(bits, scale, relu):
    value, exact = as_fraction(bits)
    if math.isnan(value) or scale == 255:
        return 0x7f
    if math.isinf(value):
        integer = -127 if value < 0 else 127
    else:
        scaled = exact * (Fraction(2) ** (127 - scale))
        if scaled > Fraction(127, 64):
            integer = 127
        elif scaled < Fraction(-127, 64):
            integer = -127
        else:
            integer = round_even(scaled * 64)
    if relu and integer < 0:
        return 0
    return integer & 0xff


def put(klass, fields, registers, group, index, value):
    name = next(n for n in group.split('/') if n in klass.operand_types)
    typ = klass.operand_types[name].type
    if typ == 'Register':
        fields[name] = index
        registers[f'R{index}'] = value
    elif typ == 'UniformRegister':
        fields[name] = index
        registers[f'UR{index}'] = value
    else:
        fields[name] = value
    return name


def classes_for(model):
    return [k for k in model.classes if not k.alternate and k.name.startswith((
        'f2fp_f16_to_mx8_downconvert_scale__',
        'f2fp_f32_to_mx8_downconvert_scale__'))]


def make_cases(arch):
    model = ir.load(arch)
    for klass in classes_for(model):
        is_bf16 = klass.name.startswith('f2fp_f16_')
        is_f32 = not is_bf16
        dst = 'Rd'
        for i, a_bits in enumerate(FLOATS):
            b_bits = FLOATS[(i * 7 + 3) % len(FLOATS)]
            if is_bf16:
                a_bits &= 0xffff0000
                b_bits &= 0xffff0000
            for scale_a, scale_b in SCALE_PAIRS:
                for relu in ('norelu', 'RELU'):
                    # F32 operand A occupies the high output byte and uses
                    # the high scale byte; B occupies the low byte/scale.
                    scales = scale_b | (scale_a << 8)
                    fields = dict(dstfmt='S2_6', srcfmt='F32' if is_f32 else 'BF16',
                                  merge='PACK_AB_MERGE_C' if is_f32 else 'UNPACK_B_MERGE_C',
                                  rndMode='RN', satnarrow='SATFINITE', relu=relu, **{dst: 8})
                    registers = {}
                    if is_f32:
                        put(klass, fields, registers, 'Ra/URa', 2, a_bits)
                        put(klass, fields, registers, 'Rb/URb/Sb', 4, b_bits)
                    else:
                        pair = ((a_bits >> 16) << 16) | (b_bits >> 16)
                        put(klass, fields, registers, 'Rb/URb/Sb', 4, pair)
                    put(klass, fields, registers, 'Rc/URc/Sc', 6, scales)
                    word = encode(arch, klass.name, **fields)
                    expected = ((convert(a_bits, scale_a, relu == 'RELU') << 8) |
                                convert(b_bits, scale_b, relu == 'RELU'))
                    yield (dict(word=word, registers=registers, observe=['R8'],
                                inspect_pcode=True), expected, klass.name)


def run(arch, fp):
    todo = list(make_cases(arch))
    payload = ''.join(json.dumps(request) + '\n' for request, _, _ in todo)
    proc = subprocess.run([GHIDRA_PY, str(ROOT / 'tests/ghidra_emulate.py'), str(LDEFS),
                           f'SASS:LE:64:{variant(arch, fp)}'], input=payload,
                          capture_output=True, text=True)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
    if len(results) != len(todo):
        raise RuntimeError(proc.stderr[-4000:])
    failures = []
    for (request, expected, klass), result in zip(todo, results):
        got = result.get('registers', {}).get('R8')
        ops = result.get('pcode_ops', [])
        if got != expected or not ops or 'CALLOTHER' in ops:
            failures.append((klass, request['word'], request['registers'], expected,
                             got, result.get('error')))
    print(f'{arch} {fp}: scaled S2_6 {len(todo) - len(failures)}/{len(todo)}')
    if failures:
        raise AssertionError(failures[:12])


def check_fallbacks(arch):
    requests_by_class = {}
    for request, _, klass in make_cases(arch):
        requests_by_class.setdefault(klass, request)

    opaque = []
    for request in requests_by_class.values():
        raw = int.from_bytes(bytes.fromhex(request['word']), 'little') ^ (1 << 77)
        opaque.append(dict(request, word=raw.to_bytes(16, 'little').hex()))
    payload = ''.join(json.dumps(request) + '\n' for request in opaque)
    proc = subprocess.run([GHIDRA_PY, str(ROOT / 'tests/ghidra_emulate.py'), str(LDEFS),
                           f'SASS:LE:64:{variant(arch, "exact")}'], input=payload,
                          capture_output=True, text=True)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
    if len(results) != len(opaque):
        raise RuntimeError(proc.stderr[-3000:])
    rejected = sum('CALLOTHER' in result.get('pcode_ops', []) or
                   (result.get('error') or '').startswith('sass_opaque_F2FP(')
                   for result in results)
    print(f'{arch}: SATNARROW opaque fallbacks {rejected}/{len(opaque)}')
    if rejected != len(opaque):
        raise AssertionError([(r.get('text'), r.get('pcode_ops'), r.get('error'))
                              for r in results if not ('CALLOTHER' in r.get('pcode_ops', []) or
                              (r.get('error') or '').startswith('sass_opaque_F2FP('))])


def check_dataflow(arch):
    by_class = {}
    for request, _, klass in make_cases(arch):
        by_class.setdefault(klass, bytes.fromhex(request['word']))
    words = [(raw, pydecode.decode(arch, raw)) for raw in by_class.values()]
    for fp in ('exact', 'readable'):
        got = ghidra(arch, [(raw, 0) for raw, _ in words], '--dataflow', fp=fp)
        gen = Gen(arch, fp)
        failures = []
        for (_, decoded), result in zip(words, got):
            optimized = not any((op.get('userop') or '').startswith(
                ('sass_opaque_', 'sass_prim_')) for op in result.get('ops', []))
            if ('error' in result or result['flow'] != expected_flow(gen, decoded) or
                    actual(result.get('ops', [])) !=
                    expected_dataflow(gen, decoded, optimized=optimized)):
                failures.append((decoded.klass.name, result))
        print(f'{arch} {fp}: scaled S2_6 dataflow {len(words)-len(failures)}/{len(words)}')
        if failures:
            raise AssertionError(failures[:5])
    check_fallbacks(arch)


if __name__ == '__main__':
    arch = sys.argv[1] if len(sys.argv) > 1 else 'SM100'
    modes = sys.argv[2:] or ('exact', 'readable')
    if modes == ['fallback']:
        check_fallbacks(arch)
        raise SystemExit(0)
    for mode in modes:
        run(arch, mode)
    check_dataflow(arch)
