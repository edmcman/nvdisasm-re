"""Probe HADD2.F32 with GPU, independent reference, and whole-kernel P-code."""
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]
from carry_semantics import Driver
from compare_ghidra import GHIDRA_PY, LDEFS, ROOT
from gpu_semantics import kernel_request
from semantic_cases import encode
from sass import cubin, pydecode, softfloat


def word(cls, **fields):
    return int.from_bytes(bytes.fromhex(encode('SM89', cls, **fields)), 'little')


def reference(packed, fields):
    # CuLifter's HADD2.F32 formula is half2 addition followed by extracting the
    # low half and widening that value to an F32 bit pattern. The first source
    # is -RZ.F32, i.e. a pair of negative half zeros.
    source = packed
    if fields.get('Rc@absolute'): source &= 0x7fff7fff
    if fields.get('Rc@negate'): source ^= 0x80008000
    selector = fields['iswzB_as_C']
    lane = source >> (16 if selector == 'H1_H1' else 0) & 0xffff
    half = softfloat.arithmetic('add', [0x8000, lane], 16,
        ftz=fields['ftz'] == 'FTZ', sat=fields['sat'] == 'SAT')
    kind, sign, value = softfloat.unpack(half, 16)
    if kind == 'nan':
        raise ValueError('NaN payload behavior is not part of this finite probe')
    result = -0.0 if kind == 'zero' and sign else 0.0 if kind == 'zero' else float(value)
    return struct.unpack('<I', struct.pack('<f', result))[0]


def main():
    outdir = Path(os.environ.get('HADD2_F32_OUT', '/tmp/sass-hadd2-f32-oracle'))
    outdir.mkdir(parents=True, exist_ok=True)
    template = outdir / 'template.cubin'
    subprocess.run([os.environ.get('NVCC', '/usr/local/cuda-13.0/bin/nvcc'),
        '--allow-unsupported-compiler', '-cubin', '-arch=sm_89',
        '-I/usr/local/cuda-13.0/targets/x86_64-linux/include',
        str(ROOT / 'tests/kernels/carry_semantics.cu'), '-o', str(template)], check=True)
    base = template.read_bytes()
    code = cubin.kernel_image(base, 'sem_probe')['code']
    start = base.find(code)
    if start < 0 or base.find(code, start + 1) >= 0:
        raise RuntimeError('kernel text is not unique')
    decoded = [pydecode.decode('SM89', code[i:i + 16]) for i in range(0, len(code), 16)]
    placeholder = decoded[0x180 // 16]
    if placeholder.klass.mnemonic != 'IMAD' or any(placeholder.env[n] != v for n, v in
            dict(Ra=2, Rb=5, Rc=6, Rd=11).items()):
        raise RuntimeError('compiler changed placeholder register assignment')

    # Keep the upper half varied so an accidental use of lane 1 is visible.
    halves = [0x0000, 0x8000, 0x0001, 0x8001, 0x03ff, 0x0400, 0x1400,
              0x3555, 0x3800, 0x3c00, 0x3e00, 0x4000, 0xbc00, 0xc000,
              0x7bff]
    packed = [halves[i % len(halves)] | (halves[(i * 7 + 3) % len(halves)] << 16)
              for i in range(128)]
    data = b''.join(struct.pack('<III', i, ~i & 0xffffffff, x) for i, x in enumerate(packed))

    variants = []
    for ftz in ('noftz', 'FTZ'):
        for sat in ('nosat', 'SAT'):
            for selector in ('H1_H0', 'H0_H0', 'H1_H1'):
                for absolute in (0, 1):
                    for negate in (0, 1):
                        fields = dict(Rd=11, Ra='RZ', Rc=6, ofmt='F32', ftz=ftz, sat=sat,
                            iswzA='H1_H0', iswzB_as_C=selector,
                            **{'Ra@negate': 1, 'Rc@absolute': absolute, 'Rc@negate': negate})
                        variants.append((ftz, sat, selector, absolute, negate, fields))
    driver = Driver()
    requests = []
    try:
        for ftz, sat, selector, absolute, negate, fields in variants:
            target_word = word('hadd2_F32__RR', **fields)
            target = pydecode.decode('SM89', target_word.to_bytes(16, 'little'))
            if target.klass.name != 'hadd2_F32__RR':
                raise RuntimeError(f'unexpected target class: {target.klass.name}')
            if (ftz, sat, selector, absolute, negate) == ('noftz', 'nosat', 'H1_H0', 0, 0) and target.text != 'HADD2.F32 R11, -RZ, R6':
                raise RuntimeError(f'unexpected target decode: {target.text}')
            image = bytearray(base)
            control = int.from_bytes(code[0x180:0x190], 'little') & ~((1 << 105) - 1)
            image[start + 0x180:start + 0x190] = ((target_word & ((1 << 105) - 1)) | control).to_bytes(16, 'little')
            path = outdir / f'hadd2_f32_{ftz}_{sat}_{selector}_{absolute}_{negate}.cubin'
            path.write_bytes(image)
            expected = tuple(reference(x, fields) for x in packed)
            got = driver.run(path, 'probe', data)
            for i, (actual, want) in enumerate(zip(got, expected)):
                if actual != want:
                    raise AssertionError((ftz, sat, selector, absolute, negate,
                        f'lane {i}: GPU={actual:#010x}, reference={want:#010x}, input={packed[i]:#010x}'))
            print(f'HADD2.F32 FTZ={ftz} SAT={sat} {selector} abs={absolute} neg={negate}: 128 matched', flush=True)
            # The GPU wrapper uses eight BAR words as reserved patch slots.
            # The emulator runs independent threads, so remove those barriers
            # only from its copy while preserving each instruction's controls.
            emulator_image = bytearray(image)
            nop = word('nop_') & ((1 << 105) - 1)
            for offset in range(0x100, 0x180, 16):
                controls = int.from_bytes(code[offset:offset + 16], 'little') & ~((1 << 105) - 1)
                emulator_image[start + offset:start + offset + 16] = (nop | controls).to_bytes(16, 'little')
            requests.append(kernel_request(emulator_image, 'probe', data))
    finally:
        driver.close()
    proc = subprocess.run([GHIDRA_PY, str(ROOT / 'tests/ghidra_emulate.py'), str(LDEFS),
        'SASS:LE:64:sm89'], input=''.join(json.dumps(req) + '\n' for req in requests),
        capture_output=True, text=True)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
    if len(results) != len(variants):
        raise RuntimeError(proc.stderr[-3000:])
    for (ftz, sat, selector, absolute, negate, fields), result in zip(variants, results):
        if 'error' in result:
            raise RuntimeError(((ftz, sat, selector, absolute, negate), result['error']))
        if result.get('exit') != 'EXIT' or 0x180 not in result.get('visited', []):
            raise AssertionError(((ftz, sat, selector, absolute, negate), 'target not executed through EXIT'))
        expected = [reference(x, fields) for x in packed]
        outputs = [int.from_bytes(bytes.fromhex(v), 'little') for v in result['outputs']]
        if outputs != expected:
            mismatch = next(i for i, (a, b) in enumerate(zip(outputs, expected)) if a != b)
            raise AssertionError(((ftz, sat, selector, absolute, negate), mismatch,
                f'p-code={outputs[mismatch]:#010x}, reference={expected[mismatch]:#010x}'))
    print(f'SM89 HADD2.F32 GPU/reference/P-code: {len(variants) * len(packed)} comparisons matched')


if __name__ == '__main__':
    main()
