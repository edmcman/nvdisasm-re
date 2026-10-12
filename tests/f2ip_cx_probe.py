"""Probe F2IP CX B conversion against SM89 GPU and whole-kernel P-code."""
import argparse
import json
import math
import random
import struct
import subprocess
from pathlib import Path

from bmsk_cx_semantics import patch as patch_template
from compare_ghidra import GHIDRA_PY, LDEFS, ROOT
from gpu_semantics import Driver, kernel_request
from sass import cubin, pydecode
from semantic_cases import encode

CLASS = 'f2ip__RCxR'
OUT = Path('/tmp/sass-f2ip-cx')
CX_VALUES = (0x3fc00000, 0xc0200000, 0x42ff8000, 0xcf000000,
             0x7fc00000, 0x7f800000, 0xbf000000, 0x3f000000)
A_VALUES = (0, 0x80000000, 0x3f000000, 0xbf000000, 0x3fc00000,
            0xbfc00000, 0x40200000, 0xc0200000, 0x42ff0000, 0xc3000000,
            0x43000000, 0xc2ff0000, 0x42ff8000, 0xc2ff8000, 0x437f8000,
            0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 0x4f000000,
            0xcf000000)


def convert(bits, signed):
    value = struct.unpack('<f', bits.to_bytes(4, 'little'))[0]
    if math.isnan(value):
        return 0
    lo, hi = (-128, 127) if signed else (0, 255)
    if math.isinf(value):
        result = hi if value > 0 else lo
    else:
        result = round(value)
    return max(lo, min(hi, result)) & 0xff


def patched_image(base, dstfmt, offset=4):
    image = bytearray(patch_template(base, 'C', 128, offset))
    code = cubin.kernel_image(base, 'sem_cx')['code']
    start = base.find(code)
    control = int.from_bytes(image[start+0x110:start+0x120], 'little') & ~((1 << 105)-1)
    fields = dict(Rd=7, Ra=2, URb=0, Sb_offset=offset, Rc=2, Pg=7,
                  dstfmt=dstfmt, srcfmt='F32', rnd='ROUND', ntz='NTZ', relu='norelu')
    raw = int.from_bytes(bytes.fromhex(encode('SM89', CLASS, **fields)), 'little')
    word = ((raw & ((1 << 105)-1)) | control).to_bytes(16, 'little')
    decoded = pydecode.decode('SM89', word)
    if decoded.klass.name != CLASS:
        raise RuntimeError(f'patched word decoded as {decoded.text}: {fields}')
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

    cases = []
    for dstfmt in ('S8', 'U8'):
        for cx in CX_VALUES:
            image = patched_image(base, dstfmt)
            values = [A_VALUES[i % len(A_VALUES)] for i in range(128)]
            data = b''.join(struct.pack('<III', a, cx, 0) for a in values)
            signed = dstfmt == 'S8'
            expected = [((a & 0xffff) << 16) | (convert(a, signed) << 8) |
                        convert(cx, signed) for a in values]
            cases.append((dstfmt, cx, values, data, image, expected))
    print(f'Validated {len(cases)} F2IP CX patched forms', flush=True)
    if args.compile_only:
        return

    driver = Driver()
    requests, records = [], []
    try:
        for i, (dstfmt, cx, values, data, image, expected) in enumerate(cases):
            path = args.output / f'f2ip_cx_{i}.cubin'
            path.write_bytes(image)
            actual = list(driver.run(path, 'cx', data))
            if actual != expected:
                mismatch = next(j for j, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1])
                raise AssertionError(('GPU/reference mismatch', dstfmt, hex(cx), mismatch,
                    hex(actual[mismatch]), hex(expected[mismatch])))
            requests.append(kernel_request(image, 'cx', data))
            records.append(dict(dstfmt=dstfmt, cx=hex(cx), gpu_reference=len(actual)))

        language = 'sm89' if args.fp == 'exact' else 'sm89_readable'
        process = subprocess.run([GHIDRA_PY, str(ROOT/'tests/ghidra_emulate.py'),
            str(LDEFS), 'SASS:LE:64:'+language],
            input=''.join(json.dumps(r)+'\n' for r in requests), capture_output=True, text=True)
        results = [json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
        if process.returncode or len(results) != len(records):
            raise RuntimeError(process.stderr[-2500:])
        for case, record, result in zip(cases, records, results):
            dstfmt, cx, values, _, _, expected = case
            if 'error' in result:
                raise RuntimeError((dstfmt, hex(cx), result['error']))
            if 0x110 not in result['visited']:
                raise AssertionError(('F2IP CX was not executed', dstfmt, hex(cx)))
            actual = [int.from_bytes(bytes.fromhex(x), 'little') for x in result['outputs']]
            if actual != expected:
                raise AssertionError(('P-code/reference mismatch', dstfmt, hex(cx),
                                      actual[:8], expected[:8]))
            record['pcode'] = len(actual)
        comparisons = sum(record['gpu_reference'] for record in records)
        print(f'PASS: {len(records)} forms, {comparisons} GPU/reference and '
              f'{comparisons} {args.fp} P-code comparisons', flush=True)
    finally:
        driver.close()
    (args.output/f'results_{args.fp}.json').write_text(json.dumps({
        'architecture':'SM89','class':CLASS,'forms':records,
        'vectors_per_form':128,'pcode_mode':args.fp,'result':'pass'},indent=2)+'\n')


if __name__ == '__main__':
    main()
