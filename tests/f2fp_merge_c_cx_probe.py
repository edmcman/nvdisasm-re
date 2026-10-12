"""Probe SM89 F2FP MERGE_C with a CX conversion source."""
import argparse, json, struct, subprocess
from pathlib import Path

from bmsk_cx_semantics import patch as patch_template
from compare_ghidra import GHIDRA_PY, LDEFS, ROOT
from f2fp_e6m9_probe import reference as e6m9_reference
from gpu_semantics import Driver, kernel_request
from sass import cubin, pydecode, softfloat
from semantic_cases import encode

CLASS = 'f2fp_merge_c__RCxR'
OUT = Path('/tmp/sass-f2fp-merge-c-cx')
SOURCES = (0x3f800000, 0xbf800000, 0x7f7fffff, 0xff7fffff,
           0x7f800000, 0xff800000, 0x7fc12345, 0xff812345)
BASES = (0xdeadbeef, 0xaabbccdd, 0x00017fff, 0x7bff8000,
         0x7fc17e01, 0x3f803f80, 0xffff0001, 0x80017fff)


def convert(bits, fmt, rnd, sat, relu):
    kind, sign, value = softfloat.unpack(bits, 32)
    if fmt == 'E6M9':
        return e6m9_reference(bits, rnd, sat, relu)
    if fmt == 'F16':
        if kind == 'nan': out = (sign << 15) | 0x7e00
        elif kind == 'inf': out = (sign << 15) | 0x7c00
        else: out = softfloat.pack(value, 16, rnd, sign)
        if sat and (out & 0x7fff) == 0x7c00:
            out = (out & 0x8000) | 0x7bff
        nan = (out & 0x7c00) == 0x7c00 and (out & 0x03ff) != 0
    elif fmt == 'BF16':
        if kind == 'nan': out = 0x7fff
        elif rnd == 'RN':
            hi, lo = bits >> 16, bits & 0xffff
            out = (hi + int(lo > 0x8000 or (lo == 0x8000 and hi & 1))) & 0xffff
        else: out = bits >> 16
        if sat and (out & 0x7fff) == 0x7f80:
            out = (out & 0x8000) | 0x7f7f
        nan = (out & 0x7f80) == 0x7f80 and (out & 0x007f) != 0
    else:
        raise ValueError(fmt)
    if relu and out & 0x8000 and not nan:
        out = 0
    return out


def patched_image(base, spec, offset=4):
    image = bytearray(patch_template(base, 'C', 128, offset))
    code = cubin.kernel_image(base, 'sem_cx')['code']
    start = base.find(code)
    control = int.from_bytes(image[start+0x110:start+0x120], 'little') & ~((1 << 105)-1)
    fields = dict(Rd=7, URb=0, Sb_offset=offset, Rc=2,
        dstfmt=spec['fmt'], srcfmt='F32', merge='MERGE_C', rndMode=spec['rnd'],
        relu='RELU' if spec['relu'] else 'norelu',
        satfinite='SATFINITE' if spec['sat'] else 'nosatfinite',
        extract=spec['extract'], Pg=7)
    raw = int.from_bytes(bytes.fromhex(encode('SM89', CLASS, **fields)), 'little')
    word = ((raw & ((1 << 105)-1)) | control).to_bytes(16, 'little')
    decoded = pydecode.decode('SM89', word)
    if decoded.klass.name != CLASS:
        raise RuntimeError(f'patched word decoded as {decoded.text}: {fields}')
    image[start+0x110:start+0x120] = word
    return bytes(image)


def specs():
    for fmt in ('F16', 'BF16', 'E6M9'):
        for rnd in ('RN', 'RZ'):
            for relu in (False, True):
                for sat in (False, True):
                    for extract in ('H0', 'H1'):
                        yield dict(fmt=fmt, rnd=rnd, relu=relu, sat=sat, extract=extract)


def same_result(actual, expected, fmt):
    if actual == expected:
        return True
    # F16 conversion may canonicalize NaN payload/sign differently on the GPU.
    # Compare the NaN class while retaining exact checks for the merged half.
    if fmt == 'F16':
        got, want = actual & 0xffff, expected & 0xffff
        got_nan = got & 0x7c00 == 0x7c00 and got & 0x03ff != 0
        want_nan = want & 0x7c00 == 0x7c00 and want & 0x03ff != 0
        return actual >> 16 == expected >> 16 and got_nan and want_nan
    return False


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
    forms = [(spec, patched_image(base, spec)) for spec in specs()]
    print(f'Validated {len(forms)} F2FP MERGE_C CX selector words', flush=True)
    if args.compile_only:
        return

    driver = Driver()
    requests, records = [], []
    try:
        for i, (spec, image) in enumerate(forms):
            data = b''.join(struct.pack('<III', BASES[j % len(BASES)],
                SOURCES[i % len(SOURCES)] if j == 0 else 0x3f800000, 0)
                for j in range(128))
            cx_value = SOURCES[i % len(SOURCES)]
            merge_shift = 0 if spec['extract'] == 'H0' else 16
            expected = [(((BASES[j % len(BASES)] >> merge_shift) & 0xffff) << 16) |
                convert(cx_value, spec['fmt'], spec['rnd'], spec['sat'], spec['relu'])
                for j in range(128)]
            path = args.output / f'merge_c_cx_{i}.cubin'
            path.write_bytes(image)
            actual = list(driver.run(path, 'cx', data))
            if not all(same_result(got, want, spec['fmt'])
                       for got, want in zip(actual, expected)):
                mismatch = next(j for j, pair in enumerate(zip(actual, expected))
                                if not same_result(pair[0], pair[1], spec['fmt']))
                raise AssertionError(('GPU/reference mismatch', spec, hex(cx_value), mismatch,
                    hex(actual[mismatch]), hex(expected[mismatch])))
            requests.append(kernel_request(image, 'cx', data))
            records.append(dict(spec=spec, cx_source=hex(cx_value), gpu_reference=len(actual)))
        language = 'sm89' if args.fp == 'exact' else 'sm89_readable'
        proc = subprocess.run([GHIDRA_PY, str(ROOT/'tests/ghidra_emulate.py'),
            str(LDEFS), 'SASS:LE:64:'+language],
            input=''.join(json.dumps(r)+'\n' for r in requests), capture_output=True, text=True)
        results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
        if proc.returncode or len(results) != len(records):
            raise RuntimeError(proc.stderr[-2500:])
        for spec, record, result in zip((x[0] for x in forms), records, results):
            if 'error' in result: raise RuntimeError((spec, result['error']))
            if 0x110 not in result['visited']:
                raise AssertionError(('MERGE_C CX instruction not executed', spec))
            actual = [int.from_bytes(bytes.fromhex(x), 'little') for x in result['outputs']]
            source = int(record['cx_source'], 16)
            shift = 0 if spec['extract'] == 'H0' else 16
            expected = [(((BASES[j % len(BASES)] >> shift) & 0xffff) << 16) |
                convert(source, spec['fmt'], spec['rnd'], spec['sat'], spec['relu'])
                for j in range(128)]
            if actual != expected:
                raise AssertionError(('P-code/reference mismatch', spec, actual[:8], expected[:8]))
            record['pcode'] = len(actual)
        comparisons = sum(r['gpu_reference'] for r in records)
        print(f'PASS: {len(records)} forms, {comparisons} GPU/reference and '
              f'{comparisons} {args.fp} P-code comparisons', flush=True)
    finally:
        driver.close()
    (args.output/f'results_{args.fp}.json').write_text(json.dumps({
        'architecture': 'SM89', 'class': CLASS, 'forms': records,
        'vectors_per_form': 128, 'pcode_mode': args.fp, 'result': 'pass'}, indent=2)+'\n')


if __name__ == '__main__':
    main()
