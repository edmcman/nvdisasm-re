"""Focused register/flow checks for scaled 8-bit F2FP upconvert forms."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
import compare_ghidra
from semantic_cases import encode
from test_dataflow import actual, expected, expected_flow
from sass import ir, pydecode
from sass.gen_sleigh import Gen

FORMATS = ('E5M2', 'E4M3', 'E2M3', 'E3M2', 'E3M4')


def check(arch):
    model = ir.load(arch)
    classes = [k for k in model.classes
               if k.name.startswith('f2fp_8b_upconvert_scale__') and not k.alternate]
    words = []
    for i, klass in enumerate(classes):
        bsrc = next(n for n in ('Rb', 'URb', 'Sb') if n in klass.operand_types)
        csrc = next(n for n in ('Rc', 'URc', 'Sc') if n in klass.operand_types)
        fields = dict(Rd=8, dstfmt='BF16', srcfmt=FORMATS[i % len(FORMATS)],
                      merge='UNPACK_B', rndMode='RN', relu='RELU',
                      satfinite='SATFINITE', extract_B='H1')
        registers = {}
        for name, reg, value in ((bsrc, 2, 0x5a3c9f81), (csrc, 4, 0x7f00fe01)):
            typ = klass.operand_types[name].type
            if typ == 'Register':
                fields[name] = reg
                registers[f'R{reg}'] = value
            elif typ == 'UniformRegister':
                fields[name] = reg
                registers[f'UR{reg}'] = value
            else:
                fields[name] = value
        word = bytes.fromhex(encode(arch, klass.name, **fields))
        words.append((word, pydecode.decode(arch, word)))

    for fp in ('exact', 'readable'):
        got = compare_ghidra.ghidra(arch, [(word, 0) for word, _ in words], '--dataflow', fp=fp)
        gen = Gen(arch, fp)
        failures = []
        for (_, decoded), result in zip(words, got):
            optimized = not any((op.get('userop') or '').startswith(('sass_opaque_', 'sass_prim_'))
                                for op in result.get('ops', []))
            if ('error' in result or result['flow'] != expected_flow(gen, decoded) or
                    actual(result.get('ops', [])) != expected(gen, decoded, optimized=optimized)):
                failures.append((decoded.klass.name, result))
        print(f'{arch} {fp}: scaled 8-bit dataflow {len(words)-len(failures)}/{len(words)}')
        if failures:
            raise AssertionError(failures[:5])


if __name__ == '__main__':
    for architecture in sys.argv[1:] or ('SM100', 'SM101', 'SM103', 'SM120'):
        check(architecture)
