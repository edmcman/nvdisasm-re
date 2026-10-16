"""Atomics: ATOMG (global), ATOMS (shared), generic ATOM, and RED/REDG (global, no result). Each reads
the cell's old value, stores f(old, Rb) (CAS: Rc when old equals Rb) and returns old in Rd, all in
one atomic region carrying the class's memory-ordering controls (sass_atomic_begin/end).
Integer ADD/MIN/MAX/AND/OR/XOR/EXCH at U32/S32/U64/S64, INC/DEC at U32 (PTX atom.inc/dec), and
ADD at F32.FTZ.RN, F64.RN and F16x2.RN are modelled; SAFEADD, float MIN/MAX, vector and BF16
adds, 128-bit CAS, CAST, the arrive classes and predicate results are not."""
from sass.semantics.bitvec import concat, fadd, ite, smax, smin, umax, umin
from sass.semantics.family import atomic, family, outputs, unmodelled
from sass.semantics.families.floating import flushed, readable
from sass.semantics.families.integer import boolean, lanes
from sass.semantics.families.memory import address, controls, descriptor, routed_load, routed_stores, size

SPACES = {"ATOMG": "ram", "RED": "ram", "REDG": "ram", "ATOMS": "shared", "ATOM": "generic"}
FLOATS = {"F32.FTZ.RN": 32, "F64.RN": 64, "F16x2.RN": 32}
OPS = ("ADD", "MIN", "MAX", "INC", "DEC", "AND", "OR", "XOR", "EXCH")


def integer(op, old, b, signed):
    match op:
        case "ADD": return old + b
        case "MIN": return (smin if signed else umin)(old, b)
        case "MAX": return (smax if signed else umax)(old, b)
        case "INC": return ite(old < b, old + 1, 0)
        case "DEC": return ite((old == 0) | (b < old), b, old - 1)
        case "EXCH": return b
    return boolean(op, old, b)


def floating(i, sz, old, b):
    """old + b rounded to nearest; F32 flushes denormal inputs and the result (the readable lowering
    doesn't), F16x2 adds each lane."""
    if sz == "F16x2.RN": return concat(*reversed([fadd(x, y) for x, y in zip(lanes(old, 16), lanes(b, 16))]))
    if sz == "F32.FTZ.RN" and not readable(i): return flushed(fadd(flushed(old), flushed(b)))
    return fadd(old, b)


def operation(i):
    """(width, f(old, b)) of a non-CAS atomic."""
    op, sz = i.select_fork("op"), i.select_fork("sz")
    if op not in OPS: unmodelled(f"op {op}")
    w = next((w for name, w in FLOATS.items() if sz == name), None)
    if w is not None:
        if op != "ADD": unmodelled(f"float {op}")
        return w, lambda old, b: floating(i, sz, old, b)
    w, signed = size(i)
    if w not in (32, 64) or op in ("INC", "DEC") and (w, signed) != (32, False): unmodelled(f"{op} {sz}")
    return w, lambda old, b: integer(op, old, b, signed)


@family("ATOMG", "ATOMS", "ATOM", "RED", "REDG")
def atomics(i):
    """Rd = old = the w bits at the address, and the cell becomes f(old, Rb) (CAS: Rc if old == Rb,
    else unchanged), indivisibly. A generic address is routed through the shared and local windows."""
    space = SPACES[i.opcode]
    if i.has_operand("Pu") and not i.is_zero("Pu"): unmodelled("Pu result")
    if space == "generic" and i.has_operand("e") and i.select_fork("e") == "noe": unmodelled("32-bit generic address")
    cas = i.has_operand("cas")
    if cas:
        if i.select_fork("cas") != "CAS": unmodelled("CAST")
        w, _ = size(i)
        if w not in (32, 64): unmodelled(f"{w}-bit CAS")
    else:
        w, f = operation(i)
    effects = descriptor(i)
    addr = address(i)
    if i.width("Rb") != w or cas and i.width("Rc") != w: unmodelled("operand width")
    old, b = routed_load(i, space, addr, w), i.value("Rb", modifiers=False)
    if cas: stores = routed_stores(i, space, addr, i.value("Rc", modifiers=False), cond=old == b)
    else: stores = routed_stores(i, space, addr, f(old, b))
    returned = {}
    if i.has_operand("Rd"):
        if i.width("Rd") != w: unmodelled("Rd width")
        returned = {"Rd": old}
    return outputs(effects=effects, stores=stores, atomic=atomic(*controls(i)), **returned)
