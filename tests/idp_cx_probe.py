"""Probe IDP.4A CX source B on SM89 and compare whole-kernel P-code."""
import argparse
import json
import random
import struct
import subprocess
from pathlib import Path

from bmsk_cx_semantics import patch as patch_template
from compare_ghidra import GHIDRA_PY, LDEFS, ROOT
from gpu_semantics import Driver, kernel_request
from sass import cubin, pydecode
from semantic_cases import encode

CLASS = 'idp_4a__CXb'
OUT = Path('/tmp/sass-idp-cx')


def signed_byte(value):
    value &= 0xff
    return value - 256 if value & 0x80 else value


def reference(a, b, acc, afmt, bfmt):
    convert_a = signed_byte if afmt == 'S8' else lambda x: x & 0xff
    convert_b = signed_byte if bfmt == 'S8' else lambda x: x & 0xff
    total = acc
    for lane in range(4):
        total += convert_a(a >> (lane * 8)) * convert_b(b >> (lane * 8))
    return total & 0xffffffff


def patched_image(base, afmt, bfmt, offset=4):
    image = bytearray(patch_template(base, 'C', 128, offset))
    code = cubin.kernel_image(base, 'sem_cx')['code']
    start = base.find(code)
    control = int.from_bytes(image[start+0x110:start+0x120], 'little') & ~((1 << 105)-1)
    fields = dict(Rd=7, Ra=2, URb=0, Sb_offset=offset, Rc=2, Pg=7,
                  mode='4A', SrcAFmt=afmt, SrcBFmt=bfmt)
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

    rng = random.Random(0x1D0)  # deterministic corpus
    vectors = [(0x01020304, 0x05060708, 0x10203040),
               (0x7f80ff01, 0x80ff017f, 0xffffffff),
               (0, 0xffffffff, 0), (0xffffffff, 0xffffffff, 0x80000000)]
    vectors += [(rng.getrandbits(32), rng.getrandbits(32), rng.getrandbits(32))
                for _ in range(124)]
    cases = []
    for afmt in ('S8', 'U8'):
        for bfmt in ('S8', 'U8'):
            cx = 0x80ff017f
            data = b''.join(struct.pack('<III', a, cx, 0) for a, _, _ in vectors)
            expected = [reference(a, cx, a, afmt, bfmt) for a, _, _ in vectors]
            cases.append((afmt, bfmt, cx, data, expected,
                          patched_image(base, afmt, bfmt)))
    print(f'Validated {len(cases)} IDP.4A CX selector forms', flush=True)
    if args.compile_only:
        return

    driver = Driver()
    requests, records = [], []
    try:
        for i, (afmt, bfmt, cx, data, expected, image) in enumerate(cases):
            path = args.output / f'idp_cx_{i}.cubin'
            path.write_bytes(image)
            actual = list(driver.run(path, 'cx', data))
            if actual != expected:
                mismatch = next(j for j, pair in enumerate(zip(actual, expected))
                                if pair[0] != pair[1])
                raise AssertionError(('GPU/reference mismatch', afmt, bfmt, mismatch,
                    hex(actual[mismatch]), hex(expected[mismatch])))
            requests.append(kernel_request(image, 'cx', data))
            records.append(dict(src_a=afmt, src_b=bfmt, cx=hex(cx),
                                gpu_reference=len(actual)))

        language = 'sm89' if args.fp == 'exact' else 'sm89_readable'
        process = subprocess.run([GHIDRA_PY, str(ROOT/'tests/ghidra_emulate.py'),
            str(LDEFS), 'SASS:LE:64:'+language],
            input=''.join(json.dumps(r)+'\n' for r in requests),
            capture_output=True, text=True)
        results = [json.loads(line) for line in process.stdout.splitlines()
                   if line.startswith('{')]
        if process.returncode or len(results) != len(records):
            raise RuntimeError(process.stderr[-2500:])
        for (afmt, bfmt, cx, _, expected, _), record, result in zip(cases, records, results):
            if 'error' in result:
                raise RuntimeError((afmt, bfmt, result['error']))
            if 0x110 not in result['visited']:
                raise AssertionError(('IDP CX was not executed', afmt, bfmt))
            actual = [int.from_bytes(bytes.fromhex(x), 'little') for x in result['outputs']]
            if actual != expected:
                raise AssertionError(('P-code/reference mismatch', afmt, bfmt,
                                      actual[:8], expected[:8]))
            record['pcode'] = len(actual)
        comparisons = sum(r['gpu_reference'] for r in records)
        print(f'PASS: {len(records)} forms, {comparisons} GPU/reference and '
              f'{comparisons} {args.fp} P-code comparisons', flush=True)
    finally:
        driver.close()
    (args.output/f'results_{args.fp}.json').write_text(json.dumps({
        'architecture':'SM89','class':CLASS,'forms':records,
        'vectors_per_form':128,'pcode_mode':args.fp,'result':'pass'},indent=2)+'\n')


if __name__ == '__main__':
    main()
