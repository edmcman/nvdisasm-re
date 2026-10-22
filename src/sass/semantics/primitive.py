"""Runtime primitives: what only the execution environment computes. Families build them like any
other expression (`outputs(Rd=primitive.mufu("SIN", x))`); SLEIGH calls each as the pcodeop
`sass_prim_<name>`, and src/sass/semantics/runtime.py implements it by name. The name is the
operation; every input is an argument, so everything computable from values stays native."""
from sass.semantics.bitvec import PRIMITIVE_OP, Expr, const, primitive
from sass.semantics.family import effect

MUFU = ("COS", "SIN", "EX2", "LG2", "RCP", "RSQ", "RCP64H", "RSQ64H", "SQRT", "TANH")
HALVES = {"F16": 0, "BF16": 1}   # the format argument of a 16-bit MUFU


def width(x, *ws):
    if not isinstance(x, Expr) or x.w not in ws: raise TypeError(f"{x!r}: expected {ws} bits")
    return x


def mufu(fn, x, half=None):
    """MUFU.fn: a hardware approximation of x (F32; the 64H forms on a double's high word), or of
    a 16-bit half in format `half` (F16, BF16)."""
    if fn not in MUFU: raise ValueError(f"MUFU.{fn}")
    if half is None: return primitive(f"mufu_{fn.lower()}", 32, width(x, 32))
    return primitive(f"mufu_{fn.lower()}", 16, width(x, 16), const(HALVES[half], 1))


# The warp: this lane, its executing and its waiting lanes (alive, not at this instruction). A
# collective's lane argument is this lane's operand as the register holds it: the environment
# supplies the other lanes' the same way, so a transform of it (a predicate's !) comes after.
def lane(): return primitive("lane", 32)
def active(): return primitive("active", 32)
def waiting(): return primitive("waiting", 32)


def read_lane(x, src):
    """x as lane `src` (a 32-bit lane id) holds it; this lane's is x itself."""
    return primitive("read_lane", x.w, x, width(src, 32))


def ballot(p):
    """The executing lanes whose predicate p is true, as a lane mask."""
    return primitive("ballot", 32, width(p, 1))


def match(x):
    """The executing lanes whose x equals this lane's, as a lane mask."""
    return primitive("match", 32, width(x, 32, 64))


REDUCTIONS = ("and", "or", "xor", "add", "umin", "umax", "smin", "smax")


def redux(op, x):
    """x combined over the executing lanes by `op` (32-bit, wrapping add, unsigned or signed min/max)."""
    if op not in REDUCTIONS: raise ValueError(f"reduction {op}")
    return primitive(f"redux_{op}", 32, width(x, 32))


def address_space(address):
    """The one-hot space of a 64-bit generic address: global 1, shared 2, local 4, shared::cluster 8."""
    return primitive("address_space", 32, width(address, 64))


SYNCHRONIZATION = ("bar", "bssy", "bsync", "break", "warpsync", "bmov")


def synchronize(op, *args):
    """The synchronization event `op` (an effect the environment acknowledges), with its inputs."""
    if op not in SYNCHRONIZATION: raise ValueError(f"synchronization {op}")
    return effect(PRIMITIVE_OP + op, *args)


def bmov(*args):
    """BMOV's 32-bit read of convergence-barrier state, with its inputs (the state selector first)."""
    return primitive("bmov", 32, *args)

