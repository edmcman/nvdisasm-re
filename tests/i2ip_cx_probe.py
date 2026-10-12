"""Probe I2IP CX B selectors on SM89 and compare the whole-kernel P-code."""
import argparse
import json
import struct
import subprocess
from pathlib import Path

from bmsk_cx_semantics import patch as patch_template
from compare_ghidra import GHIDRA_PY, LDEFS, ROOT
from gpu_semantics import Driver, kernel_request
from sass import cubin, pydecode
from semantic_cases import encode

CLASS = 'i2ip__RCxR_RCxR'
OUT = Path('/tmp/sass-i2ip-cx')
VALUES = (0x12345678, 0x87654321, 0x0000ffff, 0xffff0000,
          0, 1, -1, -3, 3, -9, 7, 8, 15, 16, 127, -128, 255, -256,
          32767, -32768, 0x12345678, -0x1234567)
CX_VALUES = (0x12345678, 0xa5c391e7)


def reference(a, b, c, fmt, mode, extract):
    width = int(fmt[1:])
    signed = fmt.startswith('S')
    lo = -(1 << (width-1)) if signed else 0
    hi = (1 << (width-1))-1 if signed else (1 << width)-1

    def convert(value):
        if mode in ('SAT', 'SATRELU'):
            value = max(lo, min(hi, value))
        if mode == 'SATRELU':
            value = max(0, value)
        return value & ((1 << width)-1)

    tail_bits = 32 - 2*width
    tail_source = (c >> 16) if extract == 'H1' else c
    tail = ((tail_source & ((1 << tail_bits)-1)) << (2*width)) if tail_bits else 0
    return (tail | (convert(a) << width) | convert(b)) & 0xffffffff


def selectors():
    for fmt in ('S8', 'U8', 'S16', 'U16'):
        modes = ('nosatrelu', 'SAT', 'SATRELU') if fmt.startswith('S') else ('nosatrelu', 'SAT')
        for mode in modes:
            for extract in ('H0', 'H1'):
                for cx in CX_VALUES:
                    yield fmt, mode, extract, cx


def patched_image(base, fmt, mode, extract, offset=4):
    image = bytearray(patch_template(base, 'C', 128, offset))
    code = cubin.kernel_image(base, 'sem_cx')['code']
    start = base.find(code)
    control = int.from_bytes(image[start+0x110:start+0x120], 'little') & ~((1 << 105)-1)
    fields = dict(Rd=7, Ra=2, URb=0, Sb_offset=offset, Rc=2, Pg=7,
                  dstfmt=fmt, srcfmt='S32', satrelu=mode, extract_limited=extract)
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
    parser.add_argument('--observe', action='store_true',
                        help='print GPU outputs before applying the candidate reference')
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
    for fmt, mode, extract, cx in selectors():
        values = [VALUES[i % len(VALUES)] & 0xffffffff for i in range(128)]
        data = b''.join(struct.pack('<III', a, cx, 0) for a in values)
        expected = [reference(a if a < 0x80000000 else a-0x100000000,
                              cx if cx < 0x80000000 else cx-0x100000000,
                              a, fmt, mode, extract) for a in values]
        cases.append((fmt, mode, extract, cx, values, data, expected,
                      patched_image(base, fmt, mode, extract)))
    print(f'Validated {len(cases)} I2IP CX selector forms', flush=True)
    if args.compile_only:
        return

    driver = Driver()
    requests, records = [], []
    try:
        for i, (fmt, mode, extract, cx, values, data, expected, image) in enumerate(cases):
            path = args.output / f'i2ip_cx_{i}.cubin'
            path.write_bytes(image)
            actual = list(driver.run(path, 'cx', data))
            record = dict(fmt=fmt, satrelu=mode, extract=extract, cx=hex(cx),
                          gpu_outputs=[hex(x) for x in actual])
            if not args.observe and actual != expected:
                mismatch = next(j for j, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1])
                raise AssertionError(('GPU/candidate-reference mismatch', fmt, mode, extract,
                    hex(cx), mismatch, hex(actual[mismatch]), hex(expected[mismatch])))
            requests.append(kernel_request(image, 'cx', data))
            records.append(record)
        (args.output/'gpu_observations.json').write_text(json.dumps(records, indent=2)+'\n')
        if args.observe:
            print(f'GPU observation pass: {len(records)} forms; raw outputs saved.', flush=True)
            return

        language = 'sm89' if args.fp == 'exact' else 'sm89_readable'
        process = subprocess.run([GHIDRA_PY, str(ROOT/'tests/ghidra_emulate.py'),
            str(LDEFS), 'SASS:LE:64:'+language],
            input=''.join(json.dumps(r)+'\n' for r in requests), capture_output=True, text=True)
        results = [json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
        if process.returncode or len(results) != len(records):
            raise RuntimeError(process.stderr[-2500:])
        for case, record, result in zip(cases, records, results):
            fmt, mode, extract, cx, values, _, expected, _ = case
            if 'error' in result:
                raise RuntimeError((fmt, mode, extract, hex(cx), result['error']))
            if 0x110 not in result['visited']:
                raise AssertionError(('I2IP CX was not executed', fmt, mode, extract, hex(cx)))
            actual = [int.from_bytes(bytes.fromhex(x), 'little') for x in result['outputs']]
            if actual != expected:
                raise AssertionError(('P-code/reference mismatch', fmt, mode, extract,
                                      hex(cx), actual[:8], expected[:8]))
            record['pcode'] = len(actual)
        comparisons = sum(128 for _ in records)
        print(f'PASS: {len(records)} forms, {comparisons} GPU/reference and '
              f'{comparisons} {args.fp} P-code comparisons', flush=True)
        (args.output/f'results_{args.fp}.json').write_text(json.dumps({
            'architecture':'SM89','class':CLASS,'forms':records,
            'vectors_per_form':128,'pcode_mode':args.fp,'result':'pass'},indent=2)+'\n')
    finally:
        driver.close()


if __name__ == '__main__':
    main()
