"""Probe SM89 VABSDIFF4's CX B operand against GPU and Ghidra P-code."""
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

CLASS = 'vabsdiff4__RCxR_RCxR'
OUT = Path('/tmp/sass-vabsdiff4-cx')
CX_WORDS = (0x807f01ff, 0xff010080, 0x7f80ff01, 0x01020304)
EDGE_WORDS = (0, 0xffffffff, 0x807f01ff, 0xff010080, 0x7f80ff01,
              0x01020304, 0x8000007f, 0x7f000080)


def reference(a, b, c, signed, reduction):
    diffs = []
    for lane in range(4):
        x, y = (a >> (lane * 8)) & 255, (b >> (lane * 8)) & 255
        if signed:
            if x & 0x80: x -= 256
            if y & 0x80: y -= 256
        diffs.append(abs(x-y))
    if reduction == 'SIMD_MRG':
        return sum((v & 255) << (8*i) for i, v in enumerate(diffs))
    return (sum(diffs) + c) & 0xffffffff


def patched_image(base, fmt, reduction, offset=4):
    image = bytearray(patch_template(base, 'C', 128, offset))
    code = cubin.kernel_image(base, 'sem_cx')['code']
    start = base.find(code)
    control = int.from_bytes(image[start+0x110:start+0x120], 'little') & ~((1 << 105)-1)
    fields = dict(Rd=7, Pu='PT', Ra=2, URb=0, Sb_offset=offset, Rc=2,
                  fmt=fmt, red=reduction, Pg=7)
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
    for fmt in ('S8', 'U8'):
        for reduction in ('ACC', 'SIMD_MRG'):
            for cx in CX_WORDS:
                image = patched_image(base, fmt, reduction)
                values = [EDGE_WORDS[i % len(EDGE_WORDS)] ^ ((i * 0x01010101) & 0xffffffff)
                          for i in range(128)]
                data = b''.join(struct.pack('<III', a, cx, 0) for a in values)
                expected = [reference(a, cx, a, fmt == 'S8', reduction) for a in values]
                cases.append((fmt, reduction, cx, values, image, data, expected))
    print(f'Validated {len(cases)} VABSDIFF4 CX patched forms', flush=True)
    if args.compile_only:
        return

    driver = Driver()
    requests, records = [], []
    try:
        for index, (fmt, reduction, cx, values, image, data, expected) in enumerate(cases):
            path = args.output / f'vabsdiff4_cx_{index}.cubin'
            path.write_bytes(image)
            actual = list(driver.run(path, 'cx', data))
            if actual != expected:
                mismatch = next(i for i, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1])
                raise AssertionError(('GPU/reference mismatch', fmt, reduction, hex(cx),
                    mismatch, hex(actual[mismatch]), hex(expected[mismatch])))
            requests.append(kernel_request(image, 'cx', data))
            records.append(dict(fmt=fmt, reduction=reduction, cx=hex(cx),
                                gpu_reference=len(actual)))

        language = 'sm89' if args.fp == 'exact' else 'sm89_readable'
        process = subprocess.run([GHIDRA_PY, str(ROOT/'tests/ghidra_emulate.py'),
            str(LDEFS), 'SASS:LE:64:'+language],
            input=''.join(json.dumps(r)+'\n' for r in requests), capture_output=True, text=True)
        results = [json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
        if process.returncode or len(results) != len(records):
            raise RuntimeError(process.stderr[-2500:])
        for case, record, result in zip(cases, records, results):
            fmt, reduction, cx, values, _, _, expected = case
            if 'error' in result:
                raise RuntimeError((fmt, reduction, hex(cx), result['error']))
            if 0x110 not in result['visited']:
                raise AssertionError(('VABSDIFF4 CX was not executed', fmt, reduction, hex(cx)))
            actual = [int.from_bytes(bytes.fromhex(x), 'little') for x in result['outputs']]
            if actual != expected:
                raise AssertionError(('P-code/reference mismatch', fmt, reduction, hex(cx),
                                      actual[:8], expected[:8]))
            record['pcode'] = len(actual)
        comparisons = sum(record['gpu_reference'] for record in records)
        print(f'PASS: {len(records)} forms, {comparisons} GPU/reference and '
              f'{comparisons} {args.fp} P-code comparisons', flush=True)
    finally:
        driver.close()
    (args.output/f'results_{args.fp}.json').write_text(json.dumps({
        'architecture': 'SM89', 'class': CLASS, 'forms': records,
        'vectors_per_form': 128, 'pcode_mode': args.fp, 'result': 'pass'}, indent=2)+'\n')


if __name__ == '__main__':
    main()
