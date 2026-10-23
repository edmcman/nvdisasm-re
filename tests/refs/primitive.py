"""The runtime primitives (sass.semantics.primitive) for one lane of a partially active warp:
`values` holds every lane's operand, `active` the executing lanes. The warp collectives reduce over
the active lanes as the PTX definitions in refs/warp.py do over a whole warp."""
from tests.refs import warp


def lanes(active): return [i for i in range(32) if active >> i & 1]


def read_lane(values, src): return values[src]


def ballot(predicates, active): return sum(1 << i for i in lanes(active) if predicates[i])


def match(values, active, lane): return sum(1 << i for i in lanes(active) if values[i] == values[lane])


def redux(op, values, active):
    """op: and, or, xor, add, umin, umax, smin, smax over the active lanes' low 32 bits."""
    ordered = op in ("umin", "umax", "smin", "smax")
    return warp.redux(op[1:] if ordered else op, [values[i] & 0xffffffff for i in lanes(active)], op in ("smin", "smax"))


SPACES = {"global": 1, "shared": 2, "local": 4, "shared_cluster": 8}


def address_space(address, ranges):
    """The one-hot space of a generic address among [(space, start, end)], or None when it maps to
    no single space (CTA shared inside the cluster window counts as one)."""
    found = 0
    for space, start, end in ranges:
        if start <= address < end: found |= SPACES[space]
    return found if bin(found).count("1") == 1 or found == SPACES["shared"] | SPACES["shared_cluster"] else None
