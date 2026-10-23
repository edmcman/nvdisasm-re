"""Per-family emulator cases and GPU fixtures."""
import importlib
from collections import Counter, defaultdict
from functools import cache
from tests.harness.case import Cases
from tests.harness import sampling

FAMILIES = ("int_alu", "int_simd", "carry", "bmsk", "fp32", "fp32x2", "fp64", "fp16", "f2fp", "conversions", "predicates", "memory", "control", "threads", "mufu")


def module(family):
    return importlib.import_module(f"tests.cases.{family}")


def emulator_cases(arch, fp, families=FAMILIES, sample=sampling.DEFAULT, generated=True, complete=True):
    """{family: Cases} for one architecture and fp lowering; complete=False leaves the selected
    manual cases unencoded and their deferred expected values uncomputed (for callers that read
    only classes and ids)."""
    sample = sampling.percentage(sample)
    out = {}
    for family in families:
        out[family] = Cases(arch, fp); module(family).emulator_cases(out[family])
        if any((case.hardware or {}).get('probe') for case in out[family]):
            raise ValueError(f'{family}: draft probes need reference expectations before entering regression suites')
        out[family][:] = sampling.select(out[family], arch, sample, getattr(out[family], "manual_reasons", ()))
        if complete: out[family].complete()
        if generated:
            from tests.harness.autocases import append
            append(out[family], family, sample)
        if any((case.hardware or {}).get('probe') for case in out[family]):
            raise ValueError(f'{family}: draft probes need reference expectations before entering regression suites')
    return out


def stream(arch, fp, family, sample, shard):
    """One shard of a language's family, case by case: None, the manual cases; (i, n), every n-th
    generated form from the i-th."""
    if shard is None: yield from emulator_cases(arch, fp, (family,), sample, generated=False)[family]
    else:
        from tests.harness.autocases import stream as generated
        yield from generated(arch, fp, family, *shard, sample=sample)


@cache
def planned(arch):
    """{family: {class: planned generated forms}} of the language's saved plan."""
    from tests.harness.autocases import saved_plan
    out = defaultdict(Counter)
    for e in saved_plan(arch)['forms']: out[e['family']][e['cls']] += 1
    return out


def shards(arch, family, forms=1000):
    """The streamed work units of a language's family: None (the manual cases), then slices of about
    `forms` planned forms each."""
    n = -(-planned(arch)[family].total() // forms)
    return [None, *((i, n) for i in range(n))]


def gpu_fixtures(families=FAMILIES, arch="SM89"):
    """[(family, Fixture)] for the target GPU fixtures."""
    return [(family, f) for family in families for f in getattr(module(family), "gpu_fixtures", lambda **kw: [])(arch=arch)]
