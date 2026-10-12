"""Probe SM89 HMNMX2 CX selectors against GPU and whole-kernel P-code."""
import argparse, json, random, struct, subprocess
from pathlib import Path

from bmsk_cx_semantics import patch as patch_template
from compare_ghidra import GHIDRA_PY, LDEFS, ROOT
from gpu_semantics import Driver, kernel_request
from sass import cubin, pydecode
from semantic_cases import encode

CLASS = 'hmnmx2__RCxR'
FORMATS = ('F16_V2', 'BF16_V2', 'E6M9_V2')
OUT = Path('/tmp/sass-hmnmx2-cx-probe')


def is_nan(fmt, x):
    masks = {'F16_V2': (0x7c00, 0x03ff), 'BF16_V2': (0x7f80, 0x007f),
             'E6M9_V2': (0x7e00, 0x01ff)}
    exp, frac = masks[fmt]
    return (x & exp) == exp and (x & frac) != 0


def as_float(fmt, x):
    if fmt == 'F16_V2':
        return struct.unpack('<e', struct.pack('<H', x))[0]
    if fmt == 'BF16_V2':
        return struct.unpack('<f', (x << 16).to_bytes(4, 'little'))[0]
    raise ValueError(fmt)


def less(fmt, x, y):
    if fmt == 'E6M9_V2':
        sx, sy = bool(x & 0x8000), bool(y & 0x8000)
        if sx != sy:
            return sx
        return (x & 0x7fff) > (y & 0x7fff) if sx else (x & 0x7fff) < (y & 0x7fff)
    return as_float(fmt, x) < as_float(fmt, y)


def half_result(fmt, x, y, is_min, ftz, nan_mode, xorsign):
    sx, sy = x & 0x8000, y & 0x8000
    xn, yn = is_nan(fmt, x), is_nan(fmt, y)
    if nan_mode and (xn or yn):
        return 0x7fff
    if xn and yn:
        return 0x7fff
    # The encoded absolute source modifiers used by XORSIGN clear both signs
    # before comparison, but NaN classification uses the original bit patterns.
    a, b = (x & 0x7fff, y & 0x7fff) if xorsign else (x, y)
    if xn:
        result = b
    elif yn:
        result = a
    else:
        if ftz:
            if a & 0x7c00 == 0: a &= 0x8000
            if b & 0x7c00 == 0: b &= 0x8000
        if a & 0x7fff == 0 and b & 0x7fff == 0:
            result = (a | b) if is_min else (a & b)
        else:
            result = a if less(fmt, a, b) == is_min else b
        if xorsign:
            result = (result & 0x7fff) | (sx ^ sy)
    if xorsign and (xn or yn):
        result = (result & 0x7fff) | (sx ^ sy)
    if ftz and result & 0x7c00 == 0:
        result &= 0x8000
    return result


def reference(fmt, a, b, is_min, ftz, nan_mode, xorsign):
    out = 0
    for shift in (0, 16):
        x, y = (a >> shift) & 0xffff, (b >> shift) & 0xffff
        out |= half_result(fmt, x, y, is_min, ftz, nan_mode, xorsign) << shift
    return out


def formats_values(fmt):
    edges = {
        'F16_V2': [0, 0x8000, 1, 0x8001, 0x03ff, 0x83ff, 0x0400, 0x8400,
                   0x3c00, 0xbc00, 0x7c00, 0xfc00, 0x7e01, 0x7c01],
        'BF16_V2': [0, 0x8000, 1, 0x8001, 0x007f, 0x807f, 0x0080, 0x8080,
                    0x3f80, 0xbf80, 0x7f80, 0xff80, 0x7fc1, 0x7f81],
        'E6M9_V2': [0, 0x8000, 1, 0x8001, 0x01ff, 0x81ff, 0x0200, 0x8200,
                    0x3c00, 0xbc00, 0x7e00, 0xfe00, 0x7e01, 0x7e02],
    }[fmt]
    return edges


def input_vectors(fmt, seed):
    rng = random.Random(seed)
    edges = formats_values(fmt)
    values = [(edges[i % len(edges)] | (edges[(i * 5 + 3) % len(edges)] << 16))
              for i in range(16)]
    exp_mask, frac_mask = {'F16_V2': (0x7c00, 0x03ff), 'BF16_V2': (0x7f80, 0x007f),
                           'E6M9_V2': (0x7e00, 0x01ff)}[fmt]
    while len(values) < 128:
        pair = []
        for _ in range(2):
            x = rng.getrandbits(16)
            while (x & exp_mask) == exp_mask:
                x = rng.getrandbits(16)
            pair.append(x)
        values.append(pair[0] | (pair[1] << 16))
    return values


def cx_words(fmt):
    finite = {'F16_V2': 0x3c00, 'BF16_V2': 0x3f80, 'E6M9_V2': 0x3c00}[fmt]
    nan = {'F16_V2': 0x7e01, 'BF16_V2': 0x7fc1, 'E6M9_V2': 0x7e01}[fmt]
    return (finite | (finite << 16), nan | (nan << 16))


def specs():
    for fmt in FORMATS:
        for ftz in ((False, True) if fmt == 'F16_V2' else (False,)):
            for nan_mode in (False, True):
                for xorsign in (False, True):
                    for is_min in (True, False):
                        for b in cx_words(fmt):
                            yield dict(fmt=fmt, ftz=ftz, nan=nan_mode,
                                       xorsign=xorsign, is_min=is_min, cx=b)


def patched_image(base, spec, offset=4):
    image = bytearray(patch_template(base, 'C', 128, offset))
    code = cubin.kernel_image(base, 'sem_cx')['code']
    start = base.find(code)
    control = int.from_bytes(image[start+0x110:start+0x120], 'little') & ~((1 << 105)-1)
    fields = dict(Rd=7, Ra=2, URb=0, Sb_offset=offset,
        ofmt=spec['fmt'], ftz='FTZ' if spec['ftz'] else 'noftz',
        nan='NAN' if spec['nan'] else 'nonan',
        xorsign='XORSIGN' if spec['xorsign'] else 'noxorsign',
        iswzA='H1_H0', iswzB='H1_H0', Pg=7, Pp=7,
        **{'Pp@not': 0 if spec['is_min'] else 1})
    if spec['xorsign']:
        fields.update({'Ra@absolute': 1, 'Sb@absolute': 1})
    raw = int.from_bytes(bytes.fromhex(encode('SM89', CLASS, **fields)), 'little')
    word = ((raw & ((1 << 105)-1)) | control).to_bytes(16, 'little')
    decoded = pydecode.decode('SM89', word)
    if decoded.klass.name != CLASS:
        raise RuntimeError(f"patched word decoded as {decoded.text}: {fields}")
    image[start+0x110:start+0x120] = word
    return bytes(image)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compile-only', action='store_true')
    parser.add_argument('--require-gpu', action='store_true')
    parser.add_argument('--output', type=Path, default=OUT)
    parser.add_argument('--fp', choices=('exact', 'readable'), default='exact')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    template = args.output / 'template.cubin'
    subprocess.run(['/usr/local/cuda/bin/nvcc', '--allow-unsupported-compiler',
        '-cubin', '-arch=sm_89', str(ROOT/'tests/kernels/bmsk_cx_semantics.cu'),
        '-o', str(template)], check=True)
    base = template.read_bytes()
    meta = cubin.kernel_image(base, 'sem_cx')
    if meta['parameter_base'] != 0x160 or [p['size'] for p in meta['parameters']] != [8, 8]:
        raise RuntimeError('unexpected CX wrapper parameter ABI')
    cases = [(spec, input_vectors(spec['fmt'], 0x2c0 + i), patched_image(base, spec))
             for i, spec in enumerate(specs())]
    print(f'Validated {len(cases)} HMNMX2 CX selector words', flush=True)
    if args.compile_only:
        return

    driver = Driver()
    requests, records = [], []
    try:
        for i, (spec, values, image) in enumerate(cases):
            data = b''.join(struct.pack('<III', a, spec['cx'], 0) for a in values)
            expected = [reference(spec['fmt'], a, spec['cx'], spec['is_min'], spec['ftz'],
                                  spec['nan'], spec['xorsign']) for a in values]
            path = args.output / f'cx_{i}.cubin'
            path.write_bytes(image)
            actual = list(driver.run(path, 'cx', data))
            if actual != expected:
                mismatch = next(j for j, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1])
                raise AssertionError(('GPU/reference mismatch', spec, mismatch,
                                      hex(actual[mismatch]), hex(expected[mismatch])))
            requests.append(kernel_request(image, 'cx', data))
            records.append(dict(spec=spec, gpu_reference=len(actual)))
        language = 'sm89' if args.fp == 'exact' else 'sm89_readable'
        proc = subprocess.run([GHIDRA_PY, str(ROOT/'tests/ghidra_emulate.py'),
            str(LDEFS), 'SASS:LE:64:'+language],
            input=''.join(json.dumps(r)+'\n' for r in requests), capture_output=True, text=True)
        results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
        if proc.returncode or len(results) != len(records):
            raise RuntimeError(proc.stderr[-2500:])
        for i, (record, result, (spec, values, _)) in enumerate(zip(records, results, cases)):
            if 'error' in result:
                raise RuntimeError((spec, result['error']))
            if 0x110 not in result['visited']:
                raise AssertionError(('HMNMX2 CX not executed', spec))
            actual = [int.from_bytes(bytes.fromhex(x), 'little') for x in result['outputs']]
            expected = [reference(spec['fmt'], a, spec['cx'], spec['is_min'], spec['ftz'],
                                  spec['nan'], spec['xorsign']) for a in values]
            if actual != expected:
                raise AssertionError(('P-code/reference mismatch', spec, actual[:8], expected[:8]))
            record.update(pcode=len(actual), steps=result['steps'])
        vectors = sum(r['gpu_reference'] for r in records)
        print(f'PASS: {len(records)} selector forms, {vectors} GPU/reference and '
              f'{vectors} {args.fp} P-code comparisons', flush=True)
    finally:
        driver.close()
    (args.output/f'results_{args.fp}.json').write_text(json.dumps({
        'architecture': 'SM89', 'class': CLASS, 'vectors_per_form': 128,
        'forms': records, 'pcode_mode': args.fp, 'result': 'pass'}, indent=2)+'\n')


if __name__ == '__main__':
    main()
