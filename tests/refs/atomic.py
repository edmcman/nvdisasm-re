"""Atomic read-modify-write references. Integer operations follow PTX `atom`/`red` (inc/dec
wrap at the operand, min/max by the size's signedness); CAS operand roles come from ptxas's
lowering of `atom.cas` (`tests/ptx/atom/`). Float adds round to nearest; F32 flushes denormal
inputs and results (PTX `atom.add.f32`). F32 and F16x2 NaN results are FADD's and HADD2's
canonical ones; F64 passes a NaN operand through (SM89 GPU)."""
from sass.semantics.fp import softfloat
from tests.refs.float import rounded

SIZES = {"U32": (32, False), "S32": (32, True), "U64": (64, False), "S64": (64, True)}


def signed(x, w): return x - (1 << w) if x >> (w - 1) & 1 else x


def integer(op, sz, old, b):
    """The value an integer atomic stores; it returns `old`."""
    w, s = SIZES[sz]
    key = (lambda x: signed(x, w)) if s else (lambda x: x)
    match op:
        case "ADD": return (old + b) % (1 << w)
        case "MIN": return min(old, b, key=key)
        case "MAX": return max(old, b, key=key)
        case "INC": return 0 if old >= b else old + 1
        case "DEC": return b if old == 0 or old > b else old - 1
        case "AND": return old & b
        case "OR": return old | b
        case "XOR": return old ^ b
        case "EXCH": return b
    raise ValueError(op)


def f64_add(old, b):
    """F64.RN: a NaN operand is stored unchanged, not quieted, b's if both are NaN; inf + -inf gives
    the canonical NaN (SM89 GPU, RED.E.ADD.F64.RN)."""
    for x in (b, old):
        if softfloat.unpack(x, 64)[0] == "nan": return x
    return rounded("add", [old, b], 64)


def floating(sz, old, b):
    """The value a float atomic ADD stores; it returns `old`."""
    match sz:
        case "F32.FTZ.RN": return rounded("add", [old, b], 32, ftz=True)
        case "F64.RN": return f64_add(old, b)
        case "F16x2.RN": return sum(rounded("add", [old >> s & 0xffff, b >> s & 0xffff], 16) << s for s in (0, 16))
    raise ValueError(sz)


def rmw(op, sz, old, b):
    """(stored, returned) of `op` on the cell holding `old`."""
    return (floating(sz, old, b) if sz.startswith("F") else integer(op, sz, old, b)), old


def cas(old, compare, new):
    """(stored, returned): `new` replaces `old` if it equals `compare`."""
    return (new if old == compare else old), old
