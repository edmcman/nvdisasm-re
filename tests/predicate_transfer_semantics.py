"""Patched SM89 P2R/R2P GPU, reference, and whole-kernel p-code oracle."""
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
from sass import cubin, pydecode
from semantic_cases import encode


def word(cls, **fields):
    return int.from_bytes(bytes.fromhex(encode('SM89', cls, **fields)), 'little')


def main():
    outdir = Path(os.environ.get('PREDICATE_TRANSFER_OUT', '/tmp/sass-predicate-transfer-oracle'))
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
    bars = [i * 16 for i, item in enumerate(decoded) if item.klass.mnemonic == 'BAR']
    if bars != list(range(0x100, 0x180, 16)):
        raise RuntimeError(f'compiler changed reserved slots: {bars}')
    placeholder = decoded[0x180 // 16]
    if placeholder.klass.mnemonic != 'IMAD' or any(placeholder.env[n] != v for n, v in
            dict(Ra=2, Rb=5, Rc=6, Rd=11).items()):
        raise RuntimeError('compiler changed placeholder register assignment')

    data = b''.join(struct.pack('<III', i, 0, 0) for i in range(128))
    # Four ISETPs establish P0..P6 = 0b1010101. Each writes a complementary pair.
    init = [word('isetp__RRR_RRR_noEX', Pu=lo, Pv=hi, Pp='PT', Ra='RZ', Rb='RZ',
                 icmp='EQ', fmt='U32', bop='AND')
            for lo, hi in ((0, 1), (2, 3), (4, 5), (6, 'PT'))]
    masks = (0, 1, 0x15, 0x2a, 0x55, 0x7f, 0xff, 0x80000055, 0xffffffff)
    control = int.from_bytes(code[0x180:0x190], 'little') & ~((1 << 105) - 1)
    controlfirst = (control & ~(0x3f << 116)) | (0x3f << 116)
    cases = []

    def add_case(label, filename, sequence, expected, target):
        image = bytearray(base)
        for i, value in enumerate(sequence):
            sched = controlfirst if i == 0 else control
            off = 0x100 + 16 * i
            image[start + off:start + off + 16] = ((value & ((1 << 105) - 1)) | sched).to_bytes(16, 'little')
        image[start + 0x180:start + 0x190] = ((word('nop_') & ((1 << 105) - 1)) | control).to_bytes(16, 'little')
        path = outdir / filename
        path.write_bytes(image)
        cases.append((label, path, bytes(image), expected, target))

    for mask in masks:
        p2r = word('p2r__RsIR_RIR', Rd=11, Pr='PR', Ra='RZ', Sb=mask, insert='B0')
        seq = init + [p2r, word('nop_'), word('nop_'), word('nop_')]
        expected = 0x55 & mask & 0x7f
        add_case(f'p2r mask={mask:#x}', f'p2r_mask_{mask:08x}.cubin', seq, expected, 0x140)

    # The selector chooses the destination byte that receives the packed bits.
    p2r_base = 0x11223344
    p2r_mask = 0x7f
    for byte in range(4):
        selector = f'B{byte}'
        p2r = word('p2r__RsIR_RIR', Rd=11, Pr='PR', Ra=4, Sb=p2r_mask, insert=selector)
        seq = [word('mov__RI', Rd=4, Sb=p2r_base, PixMaskU04=15)] + init + [p2r, word('nop_'), word('nop_')]
        shift = byte * 8
        expected = (p2r_base & ~(0xff << shift)) | ((0x55 & p2r_mask & 0x7f) << shift)
        add_case(f'p2r .{selector} base={p2r_base:#x}', f'p2r_{selector.lower()}.cubin', seq, expected, 0x150)

    # Clear selected P bits from the initialized pattern, then pack all seven back.
    r2p = word('r2p__RIR', Ra='RZ', Sb=0x15, a_bsel='B0')
    p2r = word('p2r__RsIR_RIR', Rd=11, Pr='PR', Ra='RZ', Sb=0x7f, insert='B0')
    seq = init + [r2p, word('nop_'), p2r, word('nop_')]
    add_case('r2p mask=0x15 source=RZ then p2r readback', 'r2p_mask_15.cubin', seq, 0x40, 0x140)

    # The selector chooses which source byte supplies the selected predicate bits.
    r2p_source = 0xa1b2c354
    r2p_mask = 0x7f
    for byte in range(4):
        selector = f'B{byte}'
        r2p = word('r2p__RIR', Ra=4, Sb=r2p_mask, a_bsel=selector)
        p2r_readback = word('p2r__RsIR_RIR', Rd=11, Pr='PR', Ra='RZ', Sb=0x7f, insert='B0')
        seq = [word('mov__RI', Rd=4, Sb=r2p_source, PixMaskU04=15)] + init + [
            r2p, word('nop_'), p2r_readback]
        expected = (r2p_source >> (8 * byte)) & r2p_mask & 0x7f
        add_case(f'r2p .{selector} source={r2p_source:#x}', f'r2p_{selector.lower()}.cubin', seq, expected, 0x150)

    requests = [kernel_request(image, 'probe', data) for _, _, image, _, _ in cases]
    driver = Driver()
    try:
        for label, path, _, expected, _ in cases:
            got = driver.run(path, 'probe', data)
            if got != (expected,) * 128:
                raise AssertionError(f'{label}: GPU={got[:8]}, expected={expected:#x}')
            print(f'{label}: GPU={expected:#x}, 128 lanes', flush=True)
    finally:
        driver.close()

    proc = subprocess.run([GHIDRA_PY, str(ROOT / 'tests/ghidra_emulate.py'), str(LDEFS),
        'SASS:LE:64:sm89'], input=''.join(json.dumps(req) + '\n' for req in requests),
        capture_output=True, text=True)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith('{')]
    if len(results) != len(cases):
        raise RuntimeError(proc.stderr[-3000:])
    for (label, _, _, expected, target), result in zip(cases, results):
        if 'error' in result:
            raise RuntimeError((label, result['error']))
        if result.get('exit') != 'EXIT' or target not in result.get('visited', []):
            raise AssertionError((label, 'target instruction was not executed through EXIT'))
        outputs = [int.from_bytes(bytes.fromhex(v), 'little') for v in result['outputs']]
        if outputs != [expected] * 128:
            raise AssertionError((label, outputs[:8], expected))

    total = len(cases) * 128
    result = dict(architecture='SM89', fixture='manually patched SASS in compiled whole-kernel wrapper',
        predicate_initial_bits=0x55, p2r_masks=list(masks), byte_selectors=['B0','B1','B2','B3'], r2p_mask=0x15,
        threads_per_case=128, cases=len(cases), comparisons=total, passed=True,
        p2r_rule='replace byte selected by insert with the selected P0-P6 bits; preserve other bytes; ignore mask bits above 6',
        r2p_rule='selected Pi receive Ra[8*byte+i] where byte is a_bsel selector; unselected predicates retain their prior values',
        note='Eight BAR slots and the placeholder arithmetic word replaced; compiled wrapper and EXIT preserved')
    (outdir / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f'SM89 P2R/R2P: {total} GPU/reference/p-code comparisons passed')


if __name__ == '__main__':
    main()
