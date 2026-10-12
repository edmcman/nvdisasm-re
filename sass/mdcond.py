"""md CONDITIONS as decode constraints: which encodings of a class are legal.

nvdisasm prints condition-violating words; pydecode rejects them by default. The generated SLEIGH
doesn't enforce them yet (the subtables cost too much .sla size; see the plan).
"""
from sass import mdexpr


def errors(k):
    """The class's conditions that make an encoding illegal (`*_ERROR`; warnings don't)."""
    return [(kind, expr, msg) for kind, expr, msg in k.conditions if kind.endswith("_ERROR")]


def violation(arch, k, env):
    """The first violated error condition as (kind, message), or None."""
    return next(((kind, msg) for kind, expr, msg in errors(k) if not mdexpr.holds(arch, expr, env)), None)
