"""Exhaustive emulator checks for the F2FP E3M4-to-F16 UNPACK_B form."""
import json
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
from compare_ghidra import GHIDRA_PY, LDEFS
from semantic_cases import encode
from sass import ir
from sass.gen_sleigh import variant


def to_f16(code):
    sign = (code & 0x80) << 8
    exponent, mantissa = (code >> 4) & 7, code & 15
    if exponent == 7 and mantissa == 15:
        return 0x7fff
    value = mantissa / 64 if exponent == 0 else (1 + mantissa / 16) * 2 ** (exponent - 3)
    if code & 0x80:
        value = -value
    return sign | int.from_bytes(struct.pack('<e', value), 'little')


def cases(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes if k.name.startswith('f2fp_8b_upconvert__') and not k.alternate]
    for klass in classes:
        source = next((n for n in ('Rb', 'URb', 'Sb') if n in klass.operand_types), None)
        if source is None:
            continue
        typ = klass.operand_types[source].type
        for extract in ('H0', 'H1'):
            for relu in ('norelu', 'RELU'):
                for code in range(256):
                    other = code ^ 0xff
                    packed = code | (other << 8)
                    packed = packed if extract == 'H0' else packed << 16
                    fields = dict(Rd=8, dstfmt='F16', srcfmt='E3M4', merge='UNPACK_B',
                                  rndMode='RN', relu=relu, extract_B=extract)
                    registers = {}
                    if typ == 'Register':
                        fields[source] = 2
                        registers['R2'] = packed
                    elif typ == 'UniformRegister':
                        fields[source] = 2
                        registers['UR2'] = packed
                    else:
                        fields[source] = packed
                    word = encode(arch, klass.name, **fields)
                    lo, hi = to_f16(code), to_f16(other)
                    if relu != 'norelu':
                        if lo & 0x8000: lo = 0
                        if hi & 0x8000: hi = 0
                    # Selected half is the byte pair whose low byte is code.
                    expected = lo | (hi << 16)
                    yield dict(word=word, registers=registers, observe=['R8'], inspect_pcode=True), expected, klass.name


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
    for (request, expected, klass), result in zip(todo, results):
        pcode = result.get('pcode_ops', [])
        if result.get('registers') != {'R8': expected} or not pcode or 'CALLOTHER' in pcode:
            failures.append((klass, request['word'], expected, result))
            if len(failures) == 10:
                break
    print(f'{arch} {fp}: E3M4 cases {len(todo) - len(failures)}/{len(todo)}')
    if failures:
        raise AssertionError(failures)


if __name__ == '__main__':
    arch = sys.argv[1] if len(sys.argv) > 1 else 'SM100'
    modes = sys.argv[2:] or ['exact', 'readable']
    for mode in modes:
        run(arch, mode)
