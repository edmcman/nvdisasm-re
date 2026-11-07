"""Recovered MUFU models and raw GPU fixtures."""
import struct
from tests.harness.manual import retained
from tests.refs import mufu

OPS = ("COS", "SIN", "EX2", "LG2", "RCP", "RSQ", "RCP64H", "RSQ64H", "SQRT", "TANH")


@retained('Every coefficient segment, retained-square thresholds, guards and sinks supplement generated operand/modifier forms.')
def emulator_cases(c):
    field = 'mufuop' if 'mufuop' in c.classes['mufu__RRR_RR'].operand_types else 'op'
    for op in ('SIN', 'COS'):
        for segment in range(64):
            for local in (0, 1023, 1024, 1280, (1 << 17) - 1):
                value = ((segment << 17) + local) / (1 << 25)
                bits = struct.unpack('<I', struct.pack('<f', value))[0]
                c.add('mufu__RRR_RR', dict(Rd=2, Rb=2, **{field: op}), dict(R2=bits),
                      dict(R2=mufu.sincos(bits, op)))
        c.add('mufu__RRR_RR', dict(Rd=8, Rb=2, Pg=0, **{field: op}),
              dict(R2=0x3daaaaab, R8=0x12345678, P0=0), dict(R8=0x12345678))
        c.add('mufu__RRR_RR', dict(Rd='RZ', Rb=2, **{field: op}), dict(R2=0x3daaaaab), dict(RZ=0))
    for cls, op in (('mufu__RRR_RR', 'EX2'), ('mufu_fp16__RR', 'SIN')):
        c.add(cls, dict(Rd=8, Rb=2, **{field: op}), dict(R2=0x3e800000),
              dict(error='MUFU approximation'), hardware={'skip':'unresolved MUFU operation or precision'})


def gpu_fixtures(arch="SM89"):
    from tests.harness.gpu import encode
    from tests.harness.templates import variable_latency_fixture
    inputs, outputs = mufu.samples()
    pad = -len(inputs) % 32  # whole warps: padding threads repeat the first input
    full = [variable_latency_fixture(f"mufu_{op.lower()}", encode("mufu__RRR_RR", Rd=11, Rb=2, mufuop=op, arch=arch),
                                     [(x, 0, 0) for x in inputs + inputs[:1] * pad], outputs[op] + outputs[op][:1] * pad,
                                     modes=('exact', 'readable') if op in ('SIN', 'COS', 'RCP', 'RSQ', 'RCP64H', 'RSQ64H') else (),
                                     emulator_same=(lambda got, want: got == want) if op in ('RCP', 'RSQ', 'RCP64H', 'RSQ64H') else None, arch=arch)
            for op in OPS]
    from tests.harness.autocase_profiles import PROFILES
    from sass.decode import ir
    k = next(k for k in ir.load(arch).classes if k.name == 'mufu_fp16__RR')
    fields = {'width' if 'width' in k.operand_types else 'fmt': 'F16'}
    halves = list(PROFILES['mufu_reciprocal']['halves']) + list(range(0x713, 0x7800, 0x800))
    halves += halves[:1] * (-len(halves) % 32)
    half = [variable_latency_fixture(f'mufu_{op.lower()}_f16_{extract.lower()}',
            encode(k.name, Rd=11, Rb=2, mufuop=op, extract=extract, arch=arch, **fields),
            [(x | 0xfc01 << 16 if extract == 'H0' else x << 16 | 0x7e01, 0, 0) for x in halves],
            [mufu.reciprocal(x, op, 'F16') for x in halves],
            modes=('exact', 'readable'), arch=arch)
            for op in ('RCP', 'RSQ') for extract in ('H0', 'H1')]
    return full + half
