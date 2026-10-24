"""Backend-independent SASS semantics: a small typed expression IR.

Every value has a width in bits and means an unsigned integer modulo 2^w, like an SMT-LIB
bitvector; booleans are 1 bit and signedness lives in operators. `extract(x, hi, lo)` and
`concat(hi, ..., lo)` take bitvectors apart and put them together. Nodes are interned (equal live nodes are the
same object; the table holds them weakly, so unused ones are freed), so sharing is identity and backends
can memoize by node. `OPS` gives each operator's concrete meaning; constant folding here
and `sass.semantics.evaluate` both use it, so the IR has exactly one definition.

Python operators build IR: + - * & | ^ ~ (unary -) << >> (logical) and the unsigned comparisons
< <= > >= == !=, which give 1-bit booleans. Plain ints take the other operand's width. An IR
value has no truth value: a Python `if` on one raises (use a ternary, which `@family` rewrites).
"""
from functools import reduce
from typing import NamedTuple
from weakref import WeakValueDictionary
from sass.semantics.fp import softfloat


def mask(w): return (1 << w) - 1
def signed(v, w): return v - (1 << w) if v >> (w - 1) & 1 else v


def identity(a):
    """A hashable key for nested tuples of expressions and plain values: an expression is its id (an
    int), anything else a tuple, so the two never compare equal (and `==` never reaches an Expr)."""
    if type(a) is Expr: return id(a)
    if type(a) is tuple: return ("t", *[identity(x) for x in a])
    return ("v", a)


class Expr:
    __slots__ = ("op", "args", "w", "__weakref__")
    _interned = WeakValueDictionary()

    def __new__(cls, op, args, w):
        # Keyed by child identity: children are interned, and `==` on them builds IR. A child's id
        # can't be reused while a key names it: the entry lives only as long as its node, which holds the child.
        args = tuple(args)
        key = (op, w, *[id(a) if type(a) is Expr else identity(a) for a in args])
        node = cls._interned.get(key)
        if node is None:
            node = object.__new__(cls)
            node.op, node.args, node.w = op, args, w
            cls._interned[key] = node
        return node

    __hash__ = object.__hash__

    def __bool__(self):
        raise TypeError(f"dynamic condition {self!r} in a Python `if`/`while`/`and`/`or`; "
                        "use a ternary (`a if c else b`) or ite()")

    def __repr__(self): return show([(None, self)])

    def __add__(self, o): return binary("add", self, o)
    def __radd__(self, o): return binary("add", o, self)
    def __sub__(self, o): return binary("sub", self, o)
    def __rsub__(self, o): return binary("sub", o, self)
    def __mul__(self, o): return binary("mul", self, o)
    def __rmul__(self, o): return binary("mul", o, self)
    def __and__(self, o): return binary("and", self, o)
    def __rand__(self, o): return binary("and", o, self)
    def __or__(self, o): return binary("or", self, o)
    def __ror__(self, o): return binary("or", o, self)
    def __xor__(self, o): return binary("xor", self, o)
    def __rxor__(self, o): return binary("xor", o, self)
    def __invert__(self): return op("not", self)
    def __neg__(self): return op("neg", self)
    def __lshift__(self, n): return shift("shl", self, n)
    def __rshift__(self, n): return shift("shr", self, n)
    def __lt__(self, o): return compare("ult", self, o)
    def __le__(self, o): return compare("ule", self, o)
    def __gt__(self, o): return compare("ult", o, self)
    def __ge__(self, o): return compare("ule", o, self)
    def __eq__(self, o): return compare("eq", self, o)
    def __ne__(self, o): return compare("ne", self, o)


def const(v, w): return Expr("const", (v & mask(w),), w)
def param(name, w): return Expr("param", (name, ), w)


PRIMITIVE_OP = "sass_prim_"   # a primitive's pcodeop: PRIMITIVE_OP + name


def primitive(name, w, *args):
    """The w-bit result of the runtime primitive `name` on args: state only the execution
    environment holds (other lanes, barriers, address spaces) or an unmodelled hardware
    approximation. Never folded; the runtime (src/sass/semantics/runtime.py) computes it."""
    return op("primitive", name, *args, w=w)
def reg(file, index, count, element=32): return Expr("reg", (file, index, count), count * element)
def load(space, address, w):
    """`w` bits (a multiple of 8) of memory space `space` at byte `address`, little-endian."""
    return Expr("load", (space, address), w)
def is_const(e, v=None): return isinstance(e, Expr) and e.op == "const" and (v is None or e.args[0] == v & mask(e.w))
def value(e): return e.args[0]


class Definition(NamedTuple):
    expand: object        # (*args) -> the operation in the other operators
    description: str      # what it computes, in English


DEFINED = {}   # defined operator -> Definition (sass.semantics.family.definition)


def lift(x, w):
    """An int as a constant of width w; an Expr unchanged."""
    if isinstance(x, Expr): return x
    if isinstance(x, bool) or isinstance(x, int): return const(int(x), w)
    raise TypeError(f"not an IR value: {x!r}")


def pair(a, b):
    if not isinstance(a, Expr) and not isinstance(b, Expr): raise TypeError("an IR operation needs an IR operand")
    a, b = lift(a, b.w if isinstance(b, Expr) else None), lift(b, a.w if isinstance(a, Expr) else None)
    if a.w != b.w: raise TypeError(f"width mismatch: {a!r} is {a.w} bits, {b!r} is {b.w}")
    return a, b


# ---- concrete meaning: op -> f(values, widths of args, result width) -------------------------
# Plain (non-expression) arguments, like extract's bit positions, are passed as they are.

OPS = {
    "add": lambda v, ws, w: (v[0] + v[1]) & mask(w),
    "sub": lambda v, ws, w: (v[0] - v[1]) & mask(w),
    "mul": lambda v, ws, w: (v[0] * v[1]) & mask(w),
    "and": lambda v, ws, w: v[0] & v[1],
    "or": lambda v, ws, w: v[0] | v[1],
    "xor": lambda v, ws, w: v[0] ^ v[1],
    "not": lambda v, ws, w: ~v[0] & mask(w),
    "neg": lambda v, ws, w: -v[0] & mask(w),
    # Shift counts are unsigned values of any width; shifting by >= the width gives 0 (or sign fill).
    "shl": lambda v, ws, w: (v[0] << v[1]) & mask(w) if v[1] < w else 0,
    "shr": lambda v, ws, w: v[0] >> v[1] if v[1] < w else 0,
    "sar": lambda v, ws, w: (signed(v[0], w) >> min(v[1], w - 1)) & mask(w),
    "eq": lambda v, ws, w: int(v[0] == v[1]),
    "ne": lambda v, ws, w: int(v[0] != v[1]),
    "ult": lambda v, ws, w: int(v[0] < v[1]),
    "ule": lambda v, ws, w: int(v[0] <= v[1]),
    "slt": lambda v, ws, w: int(signed(v[0], ws[0]) < signed(v[1], ws[1])),
    "sle": lambda v, ws, w: int(signed(v[0], ws[0]) <= signed(v[1], ws[1])),
    "zext": lambda v, ws, w: v[0],
    "sext": lambda v, ws, w: signed(v[0], ws[0]) & mask(w),
    "extract": lambda v, ws, w: v[0] >> v[2] & mask(w),
    "concat": lambda v, ws, w: reduce(lambda acc, part: (acc << part[1]) | part[0], zip(v, ws), 0),
    # Floating point on IEEE encodings (16, 32, 64 bits): exactly rounded per mode, NaN results
    # canonical (nanresult gives them the GPU's); comparisons are false when either side is NaN.
    "fadd": lambda v, ws, w: softfloat.arithmetic("add", v[:2], w, v[2]),
    "fmul": lambda v, ws, w: softfloat.arithmetic("mul", v[:2], w, v[2], scale=v[3]),
    "ffma": lambda v, ws, w: softfloat.arithmetic("fma", v[:3], w, v[3]),
    "nanresult": lambda v, ws, w: v[1] if softfloat.unpack(v[0], w)[0] == "nan" else v[0],
    "flt": lambda v, ws, w: int(fcompare(v[0], v[1], ws[0], lambda x, y: x < y)),
    "fle": lambda v, ws, w: int(fcompare(v[0], v[1], ws[0], lambda x, y: x <= y)),
    "feq": lambda v, ws, w: int(fcompare(v[0], v[1], ws[0], lambda x, y: x == y)),
    "fnan": lambda v, ws, w: int(softfloat.unpack(v[0], ws[0])[0] == "nan"),
    "fconv": lambda v, ws, w: convert(v[0], ws[0], w, v[1]),
}


def convert(bits, src, dst, rnd):
    """An IEEE encoding converted between widths (NaN: canonical, keeping the sign)."""
    kind, sign, v = softfloat.unpack(bits, src)
    e, m = softfloat.FORMATS[dst]
    if kind == "nan": return sign << (dst - 1) | softfloat.nan(dst)
    if kind == "inf": return sign << (dst - 1) | ((1 << e) - 1) << m
    return softfloat.pack(v, dst, rnd, sign)


def fvalue(bits, w):
    """The real value of an IEEE encoding (infinities as float infinities), or None for NaN."""
    kind, sign, v = softfloat.unpack(bits, w)
    if kind == "nan": return None
    if kind == "inf": return float("-inf") if sign else float("inf")
    return v


def fcompare(a, b, w, test):
    x, y = fvalue(a, w), fvalue(b, w)
    return x is not None and y is not None and test(x, y)


def fold(name, args, w):
    if name in OPS and all(is_const(a) or not isinstance(a, Expr) for a in args):
        return const(OPS[name]([value(a) if isinstance(a, Expr) else a for a in args],
                               [a.w if isinstance(a, Expr) else None for a in args], w), w)
    return None


def op(name, *args, w=None):
    """A node of operator `name`, constant-folded and simplified."""
    w = w or args[0].w
    if (folded := fold(name, args, w)) is not None: return folded
    s = simplify(name, args, w)
    return Expr(name, args, w) if s is None else s


def simplify(name, args, w):
    """Algebraic identities that keep generated code compact; None if none applies."""
    a = args[0]; b = args[1] if len(args) > 1 else None
    zero, ones = (lambda e: is_const(e, 0)), (lambda e: is_const(e, -1))
    match name:
        case "sub" if is_const(b): return op("add", a, const(-value(b), w))
        case "add" | "mul" | "and" | "or" | "xor" if is_const(a) and not is_const(b): return op(name, b, a)
        case "add" if is_const(b) and a.op == "add" and is_const(a.args[1]):
            return op("add", a.args[0], const(value(a.args[1]) + value(b), w))
        case "add" | "or" | "xor" if zero(b): return a
        case "add" | "or" | "xor" if zero(a): return b
        case "sub" | "shl" | "shr" | "sar" if zero(b): return a
        case "and" if zero(a) or zero(b): return const(0, w)
        case "and" if ones(b): return a
        case "and" if ones(a): return b
        case "or" if ones(a) or ones(b): return const(-1, w)
        case "xor" if ones(b): return op("not", a)
        case "xor" if ones(a): return op("not", b)
        case "add" if a.op == "not" and is_const(b, 1): return op("neg", a.args[0])
        case "and" | "or" if a is b: return a
        case "xor" | "sub" if a is b: return const(0, w)
        case "mul" if is_const(b, 1): return a
        case "mul" if is_const(a, 1): return b
        case "mul" if zero(a) or zero(b): return const(0, w)
        case "not" | "neg" if a.op == name: return a.args[0]
        case "eq" | "ne" if a.w == 1 and is_const(b):   # booleans: b == 1 is b, b == 0 is !b
            return a if (name == "eq") == bool(value(b)) else op("not", a)
        case "eq" if a is b: return const(1, 1)
        case "ne" | "ult" if a is b: return const(0, 1)
        case "ule" if a is b: return const(1, 1)
        # Unsigned comparisons with zero (umax(x, 0) of PRMT's edge clamp).
        case "ult" if zero(b): return const(0, 1)
        case "ule" if zero(a): return const(1, 1)
        case "ult" if zero(a): return op("ne", b, a, w=1)
        case "ule" if zero(b): return op("eq", a, b, w=1)
    return None


def binary(name, a, b):
    a, b = pair(a, b)
    return op(name, a, b)


def shift(name, a, n):
    if not isinstance(a, Expr): raise TypeError("shift of a non-IR value")
    return op(name, a, lift(n, a.w))


def compare(name, a, b):
    a, b = pair(a, b)
    return op(name, a, b, w=1)


def slt(a, b): return compare("slt", a, b)
def sle(a, b): return compare("sle", a, b)
def sgt(a, b): return compare("slt", b, a)
def sge(a, b): return compare("sle", b, a)
def sar(a, n): return shift("sar", a, n)


def zext(x, w):
    if x.w == w: return x
    if x.w > w: raise TypeError(f"zext of {x.w} bits to {w}")
    if x.op == "zext": x = x.args[0]
    return op("zext", x, w=w)


def sext(x, w):
    if x.w == w: return x
    if x.w > w: raise TypeError(f"sext of {x.w} bits to {w}")
    return op("sext", x, w=w)


def extract(x, hi, lo):
    """Bits hi..lo of x (inclusive), a (hi - lo + 1)-bit value."""
    if not 0 <= lo <= hi < x.w: raise TypeError(f"extract [{hi}:{lo}] of a {x.w}-bit value")
    w = hi - lo + 1
    if w == x.w: return x
    a = x.args
    match x.op:
        case "const": return const(a[0] >> lo, w)
        case "extract": return extract(a[0], a[2] + hi, a[2] + lo)
        case "zext" | "sext" if hi < a[0].w: return extract(a[0], hi, lo)
        case "zext" if lo >= a[0].w: return const(0, w)
        case "zext" if lo == 0: return zext(a[0], w)
        case "sext" if lo == 0: return sext(a[0], w)
        case "concat":
            offset = 0
            for part in reversed(a):
                if offset <= lo and hi < offset + part.w:
                    return extract(part, hi - offset, lo - offset)
                offset += part.w
    return op("extract", x, hi, lo, w=w)


def resize(x, w): return zext(x, w) if x.w <= w else trunc(x, w)


def trunc(x, w):
    """The low w bits of x."""
    if x.w < w: raise TypeError(f"trunc of {x.w} bits to {w}")
    return extract(x, w - 1, 0)


def narrowed(x, w, shared=frozenset()):
    """The low w bits of x computed at width w: operations whose low bits depend only on their
    operands' low bits are rebuilt narrow, so trunc32(zext64(a) + zext64(b)) is a + b. Nodes in
    `shared` are computed wide anyway, so their low bits are extracted, not recomputed."""
    if x.w == w: return x
    if x in shared: return trunc(x, w)
    a, n = x.args, lambda y: narrowed(y, w, shared)
    match x.op:
        case "add" | "sub" | "mul" | "and" | "or" | "xor": return op(x.op, n(a[0]), n(a[1]))
        case "not" | "neg": return op(x.op, n(a[0]))
        case "shl" if is_const(a[1]): return n(a[0]) << value(a[1]) if value(a[1]) < w else const(0, w)
        case "shl": return op("shl", n(a[0]), a[1])   # a shift of >= w leaves no low bits either way
        case "ite": return ite(a[0], n(a[1]), n(a[2]))
        case "sel": return Expr("sel", (a[0], tuple((vals, n(t)) for vals, t in a[1])), w)
    return trunc(x, w)


def low(e): return e.op == "extract" and e.args[2] == 0


def narrow(roots):
    """The roots with low-bits extracts computed narrow where the wide value has no other use (the
    readable form; a wide value needed anyway is extracted from, not recomputed)."""
    uses = {}
    def count(e):
        if not isinstance(e, Expr): return
        uses[e] = uses.get(e, 0) + 1
        if uses[e] == 1:
            for x in e.args: count(x)
    for r in roots: count(r)
    memo, shared = {}, frozenset(e for e, n in uses.items() if n > 1)
    def walk(e):
        if not isinstance(e, Expr) or e.op in ("const", "param", "reg"): return e
        if e in memo: return memo[e]
        args = [walk(x) for x in e.args]
        out = narrowed(args[0], e.w, frozenset(memo.get(x, x) for x in shared)) if low(e) and uses.get(e.args[0], 0) == 1 else rebuilt(e, args)
        memo[e] = out
        return out
    return [walk(r) for r in roots]


def concat(*parts):
    """The bitvector of `parts`, most significant first."""
    if not parts: raise TypeError("concat needs at least one operand")
    if len(parts) == 1: return parts[0]
    return op("concat", *parts, w=sum(part.w for part in parts))


def bits(x, lo, n, w=None):
    """Bits lo..lo+n-1 of x, zero-extended to w bits (default: n)."""
    v = extract(x, lo + n - 1, lo)
    return zext(v, w) if w and w > n else v


def ite(c, a, b):
    """`a if c else b` on a dynamic width-1 condition."""
    if not isinstance(c, Expr): return a if c else b
    if c.w != 1: raise TypeError(f"condition {c!r} is {c.w} bits, not a boolean")
    if not isinstance(a, Expr) and not isinstance(b, Expr): raise TypeError("ite of two non-IR values needs a width")
    a, b = pair(a, b)
    if is_const(c): return a if value(c) else b
    if a is b: return a
    # x if x != 0 else 0 is x (and 0 if x == 0 else x).
    if c.op in ("ne", "eq") and is_const(c.args[1], 0) and c.args[0] is (a if c.op == "ne" else b) and is_const(b if c.op == "ne" else a, 0):
        return c.args[0]
    return Expr("ite", (c, a, b), a.w)


def lnot(c): return c == 0 if isinstance(c, Expr) else not c


def chosen(code, f):
    """f(value) of a constant code, or of a decode-time choice of constants (a sel, from
    select_lookup_const) the same choice of f(value) per arm: one table read by a definition's
    expansion as the family would have built it with select_lookup."""
    if is_const(code): return f(value(code))
    if code.op != "sel" or not all(is_const(t) for _, t in code.args[1]): raise TypeError(f"{code!r} is not a choice of constants")
    arms = tuple((v, f(value(t))) for v, t in code.args[1])
    return arms[0][1] if all(t is arms[0][1] for _, t in arms) else Expr("sel", (code.args[0], arms), arms[0][1].w)


ROUNDING = ("RN", "RZ", "RP", "RM")


CANONICAL_NAN = {16: 0x7fff, 32: 0x7fffffff}
QUIET64, INVALID64 = 1 << 51, 0xfff8000000000000


def nan_sources(sources):
    """The order in which F64 arithmetic propagates NaN sources: Rb, Rc, then Ra (SM89 GPU),
    that is operand 1 onward, then operand 0."""
    return [*sources[1:], *sources[:1]]


def choose(c, x, y):
    """x if c else y, without a branch."""
    return y ^ ((x ^ y) * zext(c, y.w))


def sass_nan(operands, nans=()):
    """The NaN result of arithmetic on `operands` (SM89 GPU): +0x7fff for F16, +0x7fffffff for F32;
    for F64 the first NaN of `nans` (the source registers before -/|.|, default the operands) in
    nan_sources order, quieted, else 0xfff8000000000000."""
    w = operands[0].w
    if nans and (len(nans) != len(operands) or any(n.w != w for n in nans)): raise TypeError("NaN sources must match the operands")
    if w != 64: return const(CANONICAL_NAN[w], w)
    v = const(INVALID64, 64)   # quiet already: the chosen NaN is quieted once
    for s in reversed(nan_sources(nans or operands)): v = choose(fnan(s), s, v)
    return v | QUIET64


def nanresult(r, nan):
    """r, or `nan` where r is a NaN: the GPU's NaN result of an IEEE operation. Outside the
    operation, so a decode-time choice of its rounding or scale leaves this shared."""
    return op("nanresult", r, nan)


# The SASS arithmetic: exactly rounded, with the GPU's NaN results (`nans`: see sass_nan).

def fadd(a, b, rnd="RN", nans=()):
    a, b = pair(a, b)
    return nanresult(op("fadd", a, b, rnd, w=a.w), sass_nan((a, b), nans))


def fmul(a, b, rnd="RN", scale=0, nans=()):
    """a * b * 2^scale, rounded once."""
    a, b = pair(a, b)
    return nanresult(op("fmul", a, b, rnd, scale, w=a.w), sass_nan((a, b), nans))


def ffma(a, b, c, rnd="RN", nans=()):
    """a * b + c, rounded once."""
    a, b = pair(a, b); a, c = pair(a, c)
    return nanresult(op("ffma", a, b, c, rnd, w=a.w), sass_nan((a, b, c), nans))


def fconv(x, w, rnd="RN"):
    """x converted to the w-bit IEEE format (exact when widening)."""
    return op("fconv", x, rnd, w=w)


def flt(a, b): a, b = pair(a, b); return op("flt", a, b, w=1)
def fle(a, b): a, b = pair(a, b); return op("fle", a, b, w=1)
def feq(a, b): a, b = pair(a, b); return op("feq", a, b, w=1)
def fnan(a): return op("fnan", a, w=1)


def complement(c):
    """not c, as the complementary comparison when c is one (one p-code operation, not two)."""
    f = flipped(c)
    return lnot(c) if f is None else f


def flipped(c):
    """not c without a negation: the complementary comparison, the operand of a negation, or De
    Morgan's dual when a side flips (never more operations than negating); None otherwise."""
    a = c.args
    match c.op:
        case "not" if c.w == 1: return a[0]
        case "eq": return compare("ne", *a)
        case "ne": return compare("eq", *a)
        case "ult": return compare("ule", a[1], a[0])
        case "ule": return compare("ult", a[1], a[0])
        case "slt": return compare("sle", a[1], a[0])
        case "sle": return compare("slt", a[1], a[0])
        case "and" | "or" if c.w == 1 and ((x := flipped(a[0])) is not None) | ((y := flipped(a[1])) is not None):
            x, y = x if x is not None else lnot(a[0]), y if y is not None else lnot(a[1])
            return x | y if c.op == "and" else x & y
    return None


def umin(a, b): a, b = pair(a, b); return ite(a < b, a, b)
def umax(a, b): a, b = pair(a, b); return ite(b < a, a, b)
def smin(a, b): a, b = pair(a, b); return ite(slt(a, b), a, b)
def smax(a, b): a, b = pair(a, b); return ite(slt(b, a), a, b)
def iabs(a): return ite(slt(a, 0), -a, a)
def any_of(*cs): return reduce(lambda x, y: x | y, cs)
def all_of(*cs): return reduce(lambda x, y: x & y, cs)


def rebuilt(e, args):
    """e's operator applied to new args through its builder, whose peepholes (zext(zext(x)) is one
    zext, ite and extract's look-through) op() alone doesn't apply: a rebuilt tree is the tree a
    direct build gives."""
    match e.op:
        case "ite": return ite(*args)
        case "extract": return extract(*args)
        case "zext": return zext(args[0], e.w)
        case "sext": return sext(args[0], e.w)
        case "concat": return concat(*args)
        case "load" | "sel": return Expr(e.op, tuple(args), e.w)
    return op(e.op, *args, w=e.w)


def substitute(e, mapping, memo=None):
    """`e` with leaves replaced: mapping(node) -> Expr or None (keep), applied to params, regs and
    decode-time choices (whose arms are not expression arguments)."""
    memo = {} if memo is None else memo
    if not isinstance(e, Expr): return e
    if e in memo: return memo[e]
    if e.op in ("param", "reg", "const", "sel"):
        m = mapping(e)
        out = e if m is None else m
    else:
        args = [substitute(a, mapping, memo) for a in e.args]
        out = rebuilt(e, args)
    memo[e] = out
    return out


def lowered(e, memo=None, origin=None):
    """e with each defined operator replaced by its expansion, lowered too (decode-time choices'
    arms included); the rest rebuilt, and so simplified and folded as if the expansions had been
    built in place, only where that changes something. origin(expansion, node) hears each one."""
    memo = {} if memo is None else memo
    if not isinstance(e, Expr) or e.op in ("const", "param", "reg"): return e
    if e not in memo:
        args = [lowered(a, memo, origin) for a in (e.args if e.op != "sel" else ())]
        match e.op:
            case "sel":
                arms = tuple((v, lowered(t, memo, origin)) for v, t in e.args[1])
                out = e if all(x is y for (_, x), (_, y) in zip(arms, e.args[1])) else Expr("sel", (e.args[0], arms), e.w)
            case name if name in DEFINED:
                out = lowered(DEFINED[name].expand(*args), memo, origin)
                if origin: origin(out, e)
            case _: out = e if all(x is y for x, y in zip(args, e.args)) else rebuilt(e, args)
        memo[e] = out
    return memo[e]


def leaves(e, kind="param"):
    """The distinct leaves of kind `kind` in e, in first-visit order."""
    seen, out, stack = set(), [], [e]
    while stack:
        n = stack.pop()
        if not isinstance(n, Expr) or n in seen: continue
        seen.add(n)
        if n.op == kind: out.append(n)
        stack.extend(reversed(n.args))
    return out


# ---- readable printing ------------------------------------------------------------------------

INFIX = {"add": "+", "sub": "-", "mul": "*", "and": "&", "or": "|", "xor": "^", "shl": "<<", "shr": ">>",
         "sar": "s>>", "eq": "==", "ne": "!=", "ult": "<", "ule": "<=", "slt": "s<", "sle": "s<="}


FLIPPED = {"ult": ">", "ule": ">=", "slt": "s>", "sle": "s>="}


class Text(NamedTuple):
    """Printed text and the labelled subtrees in it: (start, end, label), innermost first."""
    s: str
    spans: tuple = ()


def cat(*parts):
    s, spans = "", []
    for p in parts:
        p = p if isinstance(p, Text) else Text(p)
        spans += [(a + len(s), b + len(s), label) for a, b, label in p.spans]
        s += p.s
    return Text(s, tuple(spans))


def joined(parts, sep=", "): return cat(*[x for k, p in enumerate(parts) for x in ((sep, p) if k else (p,))])


def pointed(t):
    """t's line with each label pointing at its subtree: an underline row per nesting level, then
    the labels, rightmost first. Of labels on one span the outermost points (its call names the
    inner ones; they are its internals)."""
    spans = {(a, b): label for a, b, label in t.spans}   # innermost first: the outermost wins
    level = {}
    for a, b in sorted(spans, key=lambda k: k[1] - k[0]):
        level[a, b] = max((level[k] + 1 for k in level if a <= k[0] and k[1] <= b), default=0)
    lines, ticks = [t.s], []
    for n in range(max(level.values(), default=-1) + 1):
        row = [" "] * len(t.s)
        for c, _ in ticks: row[c] = "│"
        for (a, b), label in sorted(spans.items()):
            if level[a, b] != n: continue
            for c in range(a, b): row[c] = "┼" if row[c] == "│" else "─"
            row[a], row[b - 1] = ("╰", "╯") if b - a > 1 else ("^", "^")
            tick = next((c for c in range(a + (b - a) // 2, b) if c not in {d for d, _ in ticks}), a)
            if b - a > 2: row[tick] = "┬"
            ticks.append((tick, label))
        lines.append("".join(row).rstrip())
    ticks.sort(reverse=True)
    for k, (c, label) in enumerate(ticks):
        row = [" "] * c
        for d, _ in ticks[k + 1:]: row[d] = "│"
        lines.append("".join(row) + "╰ " + label)
    return "\n".join(lines)


def show(outputs, expand=False, describe=False):
    """Readable text for [(destination, expression)] (a None destination prints the expression
    alone): infix operators, shared subexpressions bound once as tN (in evaluation order),
    constants with their width only when it isn't 32 bits. A defined operator prints as a call
    (with the value of a constant expression after it), with `describe` followed by the English
    descriptions of those used; `expand` prints the lowered expressions instead, each expansion
    pointing at the operator it came from (on its tN line when it has one)."""
    origins, used, memo = {}, set(), {}
    if expand:
        outputs = [(d, lowered(e, memo, lambda out, e: origins.__setitem__(out, e))) for d, e in outputs]
    uses, order = {}, []
    def count(e):
        if not isinstance(e, Expr): return
        uses[e] = uses.get(e, 0) + 1
        if uses[e] > 1: return
        for a in e.args: count(a)
        order.append(e)
    for _, e in outputs: count(e)
    names, lines = {}, []
    def printer(expanding, aliases={}):
        def number(e, signed=False):
            v = value(e)
            if signed and e.w > 1 and v >> (e.w - 1): return f"-{(1 << e.w) - v:#x}" + ("" if e.w == 32 else f":{e.w}")
            return text(e)
        def minmax(e):
            c, x, y = e.args
            if c.op in ("ult", "slt") and {c.args[0], c.args[1]} == {x, y}:
                kind = ("s" if c.op == "slt" else "u") + ("min" if c.args[0] is x else "max")
                return cat(f"{kind}(", text(x, True), ", ", number(y, c.op == "slt") if is_const(y) else text(y, True), ")")
            return None
        def text(e, bare=False, define=False):
            t = shape(e, bare, define)
            # An expansion points at its operator; a leaf may be anything's result, so it doesn't.
            if expanding and e in origins and e.op not in ("const", "param", "reg") and (define or e not in names):
                o = origins[e]
                used.add(o.op)
                t = Text(t.s, t.spans + ((0, len(t.s), cat(f"{o.op}(", joined([plain(x, True) for x in o.args]), ")").s),))
            return t
        def shape(e, bare, define):
            if not isinstance(e, Expr): return Text(repr(e))
            if e in names and not define: return Text(names[e])
            if aliases.get(e) in names: return Text(names[aliases[e]])   # an operator's argument, by its expansion's name
            a = e.args
            match e.op:
                case "const": return Text(f"{a[0]:#x}" + ("" if e.w == 32 else f":{e.w}"))
                case "param": return Text(a[0])
                case "reg": return Text(f"{a[0]}{a[1]}" if a[2] == 1 else f"{a[0]}{a[1]}:{a[0]}{a[1] + a[2] - 1}")
                case "load": return cat(f"{a[0]}[", text(a[1], True), f"]:{e.w}")
                case "ite" if (m := minmax(e)): return m
                case "ite": s = cat(text(a[0]), " ? ", text(a[1]), " : ", text(a[2]))
                case "add" if is_const(a[1]) and value(a[1]) >> (e.w - 1) and e.w > 1:
                    s = cat(text(a[0]), " - ", number(const(-value(a[1]), e.w)))
                case "not" | "neg": return cat("!" if e.op == "not" and e.w == 1 else "~" if e.op == "not" else "-", text(a[0]))
                case "zext" | "sext": return cat(f"{e.op}{e.w}(", text(a[0], True), ")")
                case "extract": return cat(text(a[0]), f"[{a[1]}:{a[2]}]" if a[1] != a[2] else f"[{a[1]}]")
                case "concat": return cat("concat(", joined([text(x, True) for x in a]), ")")
                case "primitive": return cat(f"{a[0]}(", joined([text(x, True) for x in a[1:]]), ")")
                case "ult" | "ule" | "slt" | "sle" if is_const(a[0]) and not is_const(a[1]):
                    # Comparisons are canonically < / <=; print `x > k` the way it is usually written.
                    s = cat(text(a[1]), f" {FLIPPED[e.op]} ", number(a[0], e.op[0] == "s"))
                case "slt" | "sle" if is_const(a[1]): s = cat(text(a[0]), f" {INFIX[e.op]} ", number(a[1], True))
                case op if op in INFIX: s = cat(text(a[0]), f" {INFIX[op]} ", text(a[1]))
                case op:
                    if op in DEFINED: used.add(op)
                    return cat(f"{op}(", joined([text(x, True) for x in a]), ")")
            return s if bare else cat("(", s, ")")
        return text
    text, plain = printer(expand), printer(False, memo)
    def line(head, e, define=False):
        t = cat(head, text(e, True, define))
        if not expand and not is_const(e) and is_const(k := lowered(e)): t = cat(t, " = ", text(k))
        return pointed(t)
    for e in order:
        if uses[e] > 1 and e.op not in ("const", "param", "reg"):
            lines.append(line(f"t{len(names)} = ", e, True))
            names[e] = f"t{len(names)}"
    lines += [line("" if d is None else f"{d} := ", e) for d, e in outputs]
    if describe: lines += [f"# {n}: {DEFINED[n].description}" for n in sorted(used)]
    return "\n".join(lines)
