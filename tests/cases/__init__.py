"""Per-family emulator cases and GPU fixtures."""
import importlib
from tests.harness.case import Cases

FAMILIES = ("int_alu", "int_simd", "carry", "bmsk", "fp32", "fp64", "fp16", "f2fp", "conversions", "predicates", "memory", "control", "threads")


def module(family):
    return importlib.import_module(f"tests.cases.{family}")


def emulator_cases(arch, fp, families=FAMILIES, exhaustive=True):
    """{family: Cases} for one architecture and fp lowering."""
    out = {}
    for family in families:
        out[family] = Cases(arch, fp, exhaustive); module(family).emulator_cases(out[family])
    return out


def gpu_fixtures(families=FAMILIES):
    """[(family, Fixture)] for the SM89 GPU fixtures."""
    return [(family, f) for family in families for f in getattr(module(family), "gpu_fixtures", lambda: [])()]
