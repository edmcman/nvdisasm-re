"""Per-family emulator cases and GPU fixtures."""
import importlib
from concurrent.futures import ProcessPoolExecutor
from functools import cache
from tests.harness.case import Cases

FAMILIES = ("int_alu", "int_simd", "carry", "bmsk", "fp32", "fp32x2", "fp64", "fp16", "f2fp", "conversions", "predicates", "memory", "control", "threads")


def module(family):
    return importlib.import_module(f"tests.cases.{family}")


def emulator_cases(arch, fp, families=FAMILIES, exhaustive=True):
    """{family: Cases} for one architecture and fp lowering."""
    out = {}
    for family in families:
        out[family] = Cases(arch, fp, exhaustive); module(family).emulator_cases(out[family])
        if any((case.hardware or {}).get('probe') for case in out[family]):
            raise ValueError(f'{family}: draft probes need reference expectations before entering regression suites')
    return out


def build(job):
    arch, fp, family, exhaustive = job
    return list(emulator_cases(arch, fp, (family,), exhaustive)[family])


@cache
def built_cases(variants, families, exhaustive):
    """{(arch, fp): {family: [case]}}, built once per session across all cores (one task per
    language and family: the F2FP sweeps dominate)."""
    jobs = [(a, f, family, exhaustive) for a, f in variants for family in families]
    with ProcessPoolExecutor() as pool:
        built = dict(zip(jobs, pool.map(build, jobs)))
    return {(a, f): {family: built[a, f, family, exhaustive] for family in families} for a, f in variants}


def gpu_fixtures(families=FAMILIES):
    """[(family, Fixture)] for the SM89 GPU fixtures."""
    return [(family, f) for family in families for f in getattr(module(family), "gpu_fixtures", lambda: [])()]
