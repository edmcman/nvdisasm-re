"""Focused F32/F16/BF16-to-E4M3/E5M2 F2FP semantic checks."""
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
from semantic_cases import encode
from sass import ir, pydecode
from sass.gen_sleigh import Gen, variant
from test_dataflow import actual, expected, expected_flow


def decode_fp8(code, fmt):
    if fmt == 'E4M3':
        exp, mant = (code >> 3) & 15, code & 7
        if exp == 15 and mant == 7:
            return None
        if exp == 0:
            return Fraction(mant, 1) * Fraction(2) ** -9
        return Fraction(8 + mant, 8) * Fraction(2) ** (exp - 7)
    exp, mant = (code >> 2) & 31, code & 3
    if exp == 31:
        return None if mant else math.inf
    if exp == 0:
        return Fraction(mant, 1) * Fraction(2) ** -16
    return Fraction(4 + mant, 4) * Fraction(2) ** (exp - 15)


def convert(bits, fmt, relu, rnd='RN', satfinite=True):
    value = struct.unpack('<f', bits.to_bytes(4, 'little'))[0]
    sign = bits >> 31
    if math.isnan(value):
        return 0x7f
    max_code = 0x7e if fmt == 'E4M3' else 0x7b
    if math.isinf(value):
        result = max_code if satfinite else (0x7f if fmt == 'E4M3' else 0x7c)
        if not satfinite and fmt=='E4M3':sign=0
    else:
        magnitude = abs(Fraction.from_float(value))
        candidates = [(code, decode_fp8(code, fmt)) for code in range(max_code + 1)]
        maximum=candidates[-1][1]
        ulp=maximum-candidates[-2][1]
        overflow=(rnd=='RP' and not sign and magnitude>maximum) or (
            rnd=='RN' and magnitude>=maximum+ulp/2)
        if satfinite and magnitude >= maximum:
            result = max_code
        elif overflow:
            result=0x7f if fmt=='E4M3' else 0x7c
            sign=0 if fmt=='E4M3' else sign
        elif magnitude >= maximum:
            result=max_code
        else:
            if rnd == 'RP':
                if sign:
                    result = next((code for code, target in reversed(candidates)
                                   if target <= magnitude), 0)
                else:
                    result = next((code for code, target in candidates
                                   if target >= magnitude), max_code)
            else:
                distances = [(abs(magnitude - target), code) for code, target in candidates]
                closest = min(distance for distance, _ in distances)
                tied = [code for distance, code in distances if distance == closest]
                result = next((code for code in tied if code & 1 == 0), tied[0])
    # RELU clamps negative numbers, but NaNs retain their sign/payload class.
    if relu and sign and not math.isnan(value):
        return 0
    return (sign << 7) | result


def nosatfinite_cases(arch):
    model=ir.load(arch)
    f32_values=(0,0x80000000,0x3f800000,0xbf800000,0x477fe000,
                0x477ff000,0xc77ff000,0x7f800000,0xff800000,
                0x7fc00000,0xffc12345)
    half_values=(0,0x8000,0x3c00,0xbc00,0x7bff,0xfbff,
                 0x7c00,0xfc00,0x7e00,0xfe00)
    for klass in model.classes:
        if klass.alternate or not klass.name.startswith((
                'f2fp_f32_to_8b_downconvert__','uf2fp_f32_to_8b_downconvert__',
                'f2fp_f16_to_8b_downconvert__','uf2fp_f16_to_8b_downconvert__')):
            continue
        half='_f16_to_8b_' in klass.name
        src_enum=model.enums[klass.operand_types['srcfmt'].type]
        rnd_enum=model.enums[klass.operand_types['rndMode'].type]
        rnd_modes=tuple(x for x in ('RN','RP') if x in rnd_enum)
        src_formats=tuple(x for x in ('F16','BF16') if x in src_enum) if half else ('F32',)
        for srcfmt in src_formats:
            samples=half_values if half else f32_values
            for fmt in ('E4M3','E5M2'):
                for rnd in rnd_modes:
                    for relu in ('norelu','RELU'):
                        for extract in ('H0','H1'):
                            for i,x in enumerate(samples):
                                y=samples[(i*7+3)%len(samples)]
                                carry=(0xa55a1234 ^ (i*0x01010101)) & 0xffffffff
                                dest='URd' if klass.name.startswith('uf2fp_') else 'Rd'
                                fields=dict(dstfmt=fmt,srcfmt=srcfmt,
                                    merge='UNPACK_B_MERGE_C' if half else 'PACK_AB_MERGE_C',
                                    rndMode=rnd,satfinite='nosatfinite',relu=relu,
                                    extract=extract,**{dest:8})
                                regs={}
                                if half:
                                    put('Rb/URb/Sb',klass,fields,regs,2,x|(y<<16))
                                    ax=source_bits(x,srcfmt);by=source_bits(y,srcfmt)
                                else:
                                    put('Ra/URa',klass,fields,regs,2,x)
                                    put('Rb/URb/Sb',klass,fields,regs,4,y)
                                    ax,by=x,y
                                put('Rc/URc/Sc',klass,fields,regs,6,carry)
                                cvt_a=convert(ax,fmt,relu=='RELU',rnd,False)
                                cvt_b=convert(by,fmt,relu=='RELU',rnd,False)
                                half_c=carry>>16 if extract=='H1' else carry&0xffff
                                packed=((cvt_b<<8)|cvt_a) if half else ((cvt_a<<8)|cvt_b)
                                yield ({'word':encode(arch,klass.name,**fields),
                                        'registers':regs,
                                        'observe':[f'{"UR" if dest=="URd" else "R"}8'],
                                        'inspect_pcode':True},(half_c<<16)|packed,klass.name)


def output_matches(actual, expected, fmt):
    if (actual & 0xffff0000) != (expected & 0xffff0000):
        return False
    for shift in (0, 8):
        got = (actual >> shift) & 0xff
        want = (expected >> shift) & 0xff
        if got == want:
            continue
        if fmt == 'E4M3':
            got_nan = (got & 0x7f) == 0x7f
            want_nan = (want & 0x7f) == 0x7f
        else:
            got_nan = (got & 0x7c) == 0x7c and (got & 3) != 0
            want_nan = (want & 0x7c) == 0x7c and (want & 3) != 0
        if not (got_nan and want_nan):
            return False
    return True


def source_bits(raw, fmt):
    if fmt == 'F16':
        value = struct.unpack('<e', raw.to_bytes(2, 'little'))[0]
        try:
            return int.from_bytes(struct.pack('<f', value), 'little')
        except OverflowError:
            return (raw & 0x8000) << 16 | 0x7f800000
    return raw << 16


def values(fmt, source_fmt):
    special = [0, 0x8000, 1, 0x8001, 0x03ff, 0x83ff, 0x0400,
               0x8400, 0x7c00, 0xfc00, 0x7e00, 0xfe00]
    max_code = 0x7e if fmt == 'E4M3' else 0x7b
    finite = [decode_fp8(code, fmt) for code in range(max_code + 1)]
    for idx, (low, high) in enumerate(zip(finite, finite[1:])):
        if idx % 7:
            continue
        midpoint = float((low + high) / 2)
        try:
            raw = int.from_bytes(struct.pack('<e', midpoint), 'little') if source_fmt == 'F16' else \
                  int.from_bytes(struct.pack('<f', midpoint), 'little') >> 16
        except OverflowError:
            continue
        width_mask = 0xffff
        special.extend(x & width_mask for x in (raw - 1, raw, raw + 1))
        special.extend((x | 0x8000) for x in (raw - 1, raw, raw + 1))
    rng = random.Random(0x8b00d + (0 if fmt == 'E4M3' else 1) + (0 if source_fmt == 'F16' else 2))
    special.extend(rng.getrandbits(16) for _ in range(8))
    return sorted(set(x & 0xffff for x in special))


def put(group, klass, fields, registers, reg, value):
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


def fp32_cases(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if k.name.startswith(('f2fp_f32_to_8b_downconvert__',
                                                               'uf2fp_f32_to_8b_downconvert__')) and not k.alternate]
    sample = [0, 0x80000000, 1, 0x007fffff, 0x00800000, 0x3f800000,
              0xbf800000, 0x40600000, 0x43e10000, 0x477fe000,
              0x7f800000, 0xff800000, 0x7fc00000, 0xffc12345]
    rng = random.Random(0xf328b)
    sample += [rng.getrandbits(32) for _ in range(6)]
    for klass in classes:
        rnd_modes = ['RN'] + (['RP'] if 'RP' in model.enums[klass.operand_types['rndMode'].type] else [])
        for fmt in ('E4M3', 'E5M2'):
            for relu in ('norelu', 'RELU'):
                for extract in ('H0', 'H1'):
                    for rnd in rnd_modes:
                        for i, a in enumerate(sample):
                            b = sample[(i * 7 + 3) % len(sample)]
                            c = (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff
                            dest='URd' if klass.name.startswith('uf2fp_') else 'Rd'
                            fields = dict(dstfmt=fmt, srcfmt='F32', merge='PACK_AB_MERGE_C',
                                          rndMode=rnd, satfinite='SATFINITE', relu=relu, extract=extract,
                                          **{dest: 8})
                            regs = {}
                            put('Ra/URa', klass, fields, regs, 2, a)
                            put('Rb/URb/Sb', klass, fields, regs, 4, b)
                            put('Rc/URc/Sc', klass, fields, regs, 6, c)
                            half = c >> 16 if extract == 'H1' else c & 0xffff
                            want = (half << 16) | (convert(a, fmt, relu == 'RELU', rnd) << 8) | convert(b, fmt, relu == 'RELU', rnd)
                            yield dict(word=encode(arch, klass.name, **fields), registers=regs,
                                       observe=[f'{"UR" if dest == "URd" else "R"}8'], inspect_pcode=True), want, klass.name


def fp16_cases(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if k.name.startswith(('f2fp_f16_to_8b_downconvert__',
                                                               'uf2fp_f16_to_8b_downconvert__')) and not k.alternate]
    for klass in classes:
        src_enum = model.enums[klass.operand_types['srcfmt'].type]
        rnd_modes = ['RN'] + (['RP'] if 'RP' in model.enums[klass.operand_types['rndMode'].type] else [])
        for source_fmt in (x for x in ('F16', 'BF16') if x in src_enum):
            for fmt in ('E4M3', 'E5M2'):
                for relu in ('norelu', 'RELU'):
                    for extract in ('H0', 'H1'):
                      for rnd in rnd_modes:
                        vals = values(fmt, source_fmt)
                        for i, x in enumerate(vals):
                            y = vals[(i * 11 + 7) % len(vals)]
                            pair = x | (y << 16)
                            c = (0xd35a9c71 ^ (i * 0x01010101)) & 0xffffffff
                            dest='URd' if klass.name.startswith('uf2fp_') else 'Rd'
                            fields = dict(dstfmt=fmt, srcfmt=source_fmt,
                                          merge='UNPACK_B_MERGE_C', rndMode=rnd,
                                          satfinite='SATFINITE', relu=relu, extract=extract,
                                          **{dest: 8})
                            regs = {}
                            put('Rb/URb/Sb', klass, fields, regs, 2, pair)
                            put('Rc/URc/Sc', klass, fields, regs, 6, c)
                            cvt_lo = convert(source_bits(x, source_fmt), fmt, relu == 'RELU', rnd)
                            cvt_hi = convert(source_bits(y, source_fmt), fmt, relu == 'RELU', rnd)
                            half = c >> 16 if extract == 'H1' else c & 0xffff
                            want = (half << 16) | (cvt_hi << 8) | cvt_lo
                            yield dict(word=encode(arch, klass.name, **fields), registers=regs,
                                       observe=[f'{"UR" if dest == "URd" else "R"}8'], inspect_pcode=True), want, klass.name


def run(arch, fp):
    todo = (list(fp32_cases(arch)) + list(fp16_cases(arch)) +
            list(nosatfinite_cases(arch)))
    payload = ''.join(json.dumps(req) + '\n' for req, _, _ in todo)
    proc = subprocess.run([GHIDRA_PY, str(ROOT / 'tests/ghidra_emulate.py'), str(LDEFS),
                           f'SASS:LE:64:{variant(arch, fp)}'], input=payload,
                          capture_output=True, text=True)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
    if len(results) != len(todo):
        raise RuntimeError(proc.stderr[-3000:])
    failures = []
    for (req, want, klass), result in zip(todo, results):
        ops = result.get('pcode_ops', [])
        got = result.get('registers', {}).get(req['observe'][0])
        if got is None or not output_matches(got, want, 'E4M3' if '.E4M3.' in result.get('text', '') else 'E5M2') or not ops or 'CALLOTHER' in ops:
            failures.append((klass, req['word'], want, result))
            if len(failures) == 10:
                break
    print(f'{arch} {fp}: FP8 downconvert {len(todo)-len(failures)}/{len(todo)}')
    if failures:
        raise AssertionError(failures)


def check_dataflow(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if ('f2fp_f32_to_8b_downconvert__' in k.name or
                                              'uf2fp_f32_to_8b_downconvert__' in k.name or
                                              'f2fp_f16_to_8b_downconvert__' in k.name or
                                              'uf2fp_f16_to_8b_downconvert__' in k.name)
               and not k.alternate]
    words = []
    for klass in classes:
        half = '_f16_to_8b_' in klass.name
        dest='URd' if klass.name.startswith('uf2fp_') else 'Rd'
        fields = dict(dstfmt='E4M3', srcfmt='F16' if half else 'F32',
                      merge='UNPACK_B_MERGE_C' if half else 'PACK_AB_MERGE_C',
                      rndMode='RN', satfinite='SATFINITE', relu='RELU', extract='H1')
        fields[dest]=8
        regs = {}
        if half:
            put('Rb/URb/Sb', klass, fields, regs, 2, 0xc0803c00)
            put('Rc/URc/Sc', klass, fields, regs, 6, 0xdeadbeef)
        else:
            put('Ra/URa', klass, fields, regs, 2, 0xc0800000)
            put('Rb/URb/Sb', klass, fields, regs, 4, 0x3f800000)
            put('Rc/URc/Sc', klass, fields, regs, 6, 0xdeadbeef)
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
        print(f'{arch} {fp}: FP8 downconvert dataflow {len(words)-len(failures)}/{len(words)}')
        if failures:
            raise AssertionError(failures[:5])


if __name__ == '__main__':
    arch = sys.argv[1] if len(sys.argv) > 1 else 'SM100'
    modes = sys.argv[2:] or ('exact', 'readable')
    for mode in modes:
        run(arch, mode)
    check_dataflow(arch)
