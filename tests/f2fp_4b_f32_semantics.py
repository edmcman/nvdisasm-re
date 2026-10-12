"""F32-to-E2M1/E0M3 packed F2FP semantic and dataflow checks."""
import json
import math
import random
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
from compare_ghidra import GHIDRA_PY, LDEFS, ghidra
from semantic_cases import encode
from sass import ir, pydecode
from sass.gen_sleigh import Gen, variant
from test_dataflow import actual, expected, expected_flow

THRESHOLDS = {
    'E2M1': (0x3e800000, 0x3f400000, 0x3fa00000, 0x3fe00000,
            0x40200000, 0x40600000, 0x40a00000),
    'E0M3': (0x3f000000, 0x3fc00000, 0x40200000, 0x40600000,
            0x40900000, 0x40b00000, 0x40d00000),
}


def ref(bits, fmt, relu):
    sign = bits >> 31
    mag = bits & 0x7fffffff
    exp, frac = bits & 0x7f800000, bits & 0x007fffff
    if exp == 0x7f800000:
        code = 7 if frac or exp else 7
        is_nan = bool(frac)
    else:
        is_nan = False
        if fmt == 'E0M3':
            value = struct.unpack('<f', bits.to_bytes(4, 'little'))[0]
            code = min(7, round(abs(value)))
        else:
            code = 7
            for idx, threshold in enumerate(THRESHOLDS[fmt], start=1):
                # The midpoint belongs to the even encoded value.
                if mag < threshold or (mag == threshold and (idx - 1) % 2 == 0):
                    code = idx - 1
                    break
    if relu and sign:
        return 0
    return code | ((sign << 3) if not is_nan else 0)


def inputs(klass, fields, registers, a, b, c):
    for group, reg, value in (('Ra/URa', 2, a), ('Rb/URb/Sb', 4, b), ('Rc/URc/Sc', 6, c)):
        name = next(n for n in group.split('/') if n in klass.operand_types)
        typ = klass.operand_types[name].type
        if typ == 'Register':
            fields[name] = reg
            registers[f'R{reg}'] = value
        elif typ == 'UniformRegister':
            fields[name] = reg
            registers[f'UR{reg}'] = value
        else:
            fields[name] = value


def cases(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if k.name.startswith('f2fp_f32_to_4b_downconvert__') and not k.alternate]
    rng = random.Random(0xf2f4)
    for klass in classes:
        for fmt in ('E2M1', 'E0M3'):
            edges = [0, 0x80000000, 1, 0x80000001, 0x7f800000, 0xff800000,
                     0x7fc00001, 0xffc12345, 0x00000001, 0x007fffff]
            for threshold in THRESHOLDS[fmt]:
                edges.extend((threshold - 1, threshold, threshold + 1,
                              threshold | 0x80000000,
                              (threshold - 1) | 0x80000000,
                              (threshold + 1) | 0x80000000))
            edges.extend(rng.getrandbits(32) for _ in range(20))
            for relu in ('norelu', 'RELU'):
                for i, a in enumerate(edges):
                    b = edges[(i * 7 + 5) % len(edges)]
                    c = (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff
                    fields = dict(Rd=8, dstfmt=fmt, srcfmt='F32',
                                  merge='PACK_AB_MERGE_C', rndMode='RN',
                                  satfinite='SATFINITE', relu=relu)
                    registers = {}
                    inputs(klass, fields, registers, a, b, c)
                    word = encode(arch, klass.name, **fields)
                    packed = ((ref(a, fmt, relu == 'RELU') << 4) |
                              ref(b, fmt, relu == 'RELU'))
                    # PACK_AB writes the low byte; C supplies the preserved upper bits.
                    want = (c & 0xffffff00) | packed
                    yield dict(word=word, registers=registers, observe=['R8'],
                               inspect_pcode=True), want, klass.name


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
    for (request, want, klass), result in zip(todo, results):
        pcode = result.get('pcode_ops', [])
        if result.get('registers') != {'R8': want} or not pcode or 'CALLOTHER' in pcode:
            failures.append((klass, request['word'], want, result))
            if len(failures) == 10:
                break
    print(f'{arch} {fp}: F32 FP4 cases {len(todo)-len(failures)}/{len(todo)}')
    if failures:
        raise AssertionError(failures)


def check_dataflow(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if k.name.startswith('f2fp_f32_to_4b_downconvert__') and not k.alternate]
    words = []
    for i, klass in enumerate(classes):
        fields = dict(Rd=8, dstfmt='E2M1', srcfmt='F32', merge='PACK_AB_MERGE_C',
                      rndMode='RN', satfinite='SATFINITE', relu='RELU')
        registers = {}
        inputs(klass, fields, registers, 0xc0800000, 0x40200000, 0xdeadbeef)
        raw = bytes.fromhex(encode(arch, klass.name, **fields))
        words.append((raw, pydecode.decode(arch, raw)))
    for fp in ('exact', 'readable'):
        got = ghidra(arch, [(raw, 0) for raw, _ in words], '--dataflow', fp=fp)
        gen = Gen(arch, fp)
        failures = []
        for (_, decoded), result in zip(words, got):
            optimized = not any((op.get('userop') or '').startswith(('sass_opaque_', 'sass_prim_'))
                                for op in result.get('ops', []))
            if ('error' in result or result['flow'] != expected_flow(gen, decoded) or
                    actual(result.get('ops', [])) != expected(gen, decoded, optimized=optimized)):
                failures.append((decoded.klass.name, result))
        print(f'{arch} {fp}: F32 FP4 dataflow {len(words)-len(failures)}/{len(words)}')
        if failures:
            raise AssertionError(failures[:5])


if __name__ == '__main__':
    arch = sys.argv[1] if len(sys.argv) > 1 else 'SM100'
    modes = sys.argv[2:] or ['exact', 'readable']
    for mode in modes:
        run(arch, mode)
    check_dataflow(arch)
