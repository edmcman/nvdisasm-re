"""Per-family emulator cases and GPU fixtures."""
import importlib
from tests.harness.case import Cases

FAMILIES = ("int_alu", "int_simd", "carry", "bmsk", "fp32", "fp32x2", "fp64", "fp16", "f2fp", "conversions", "predicates", "memory", "atomic", "control", "threads")


def module(family):
    return importlib.import_module(f"tests.cases.{family}")


def emulator_cases(arch, fp, families=FAMILIES, exhaustive=True):
    """{family: Cases} for one architecture and fp lowering."""
    out = {}
    for family in families:
        out[family] = Cases(arch, fp, exhaustive); module(family).emulator_cases(out[family])
    return out


def ghidra_cases(arch, fp, families=FAMILIES, exhaustive=True):
    """emulator_cases as arch's language runs them: classes of the opcodes it leaves out
    (operations.GHIDRA_NOT_LOWERED) take the opaque fallback, and their md-illegal words decode."""
    from sass.semantics.operations import OPERATION_FAMILIES, ghidra_lowers
    out = emulator_cases(arch, fp, families, exhaustive)
    for cases in out.values():
        kept = []
        for c in cases:
            k = cases.classes.get(c.cls)   # None for a kernel case, whose cls is its name (Cases.kernel)
            if k is None or k.mnemonic not in OPERATION_FAMILIES or ghidra_lowers(arch, k.mnemonic): kept.append(c)
            elif "illegal" not in c.expected: kept.append(c._replace(expected=dict(error=f"sass_opaque_{k.mnemonic}")))
        cases[:] = kept
    return out


def gpu_fixtures(families=FAMILIES):
    """[(family, Fixture)] for the SM89 GPU fixtures."""
    return [(family, f) for family in families for f in getattr(module(family), "gpu_fixtures", lambda: [])()]
