"""MUFU against the SM89 GPU. The emulator has no model of MUFU (a runtime primitive, issue #21), so there are no
emulator cases; the fixtures compare each raw MUFU.<op> with the outputs recorded by research/mufu/survey.py."""
from tests.refs import mufu

OPS = ("COS", "SIN", "EX2", "LG2", "RCP", "RSQ", "RCP64H", "RSQ64H", "SQRT", "TANH")


def emulator_cases(c): pass


def gpu_fixtures():
    from tests.harness.gpu import sm89
    from tests.harness.templates import variable_latency_fixture
    inputs, outputs = mufu.samples()
    pad = -len(inputs) % 32  # whole warps: padding threads repeat the first input
    return [variable_latency_fixture(f"mufu_{op.lower()}", sm89("mufu__RRR_RR", Rd=11, Rb=2, mufuop=op),
                                     [(x, 0, 0) for x in inputs + inputs[:1] * pad], outputs[op] + outputs[op][:1] * pad, modes=())
            for op in OPS]
