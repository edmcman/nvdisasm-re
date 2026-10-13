"""Backend-independent SASS semantics: a small typed expression IR.

Every value has a width in bits and means an unsigned integer modulo 2^w, like an SMT-LIB
bitvector; booleans are 1 bit and signedness lives in operators. `extract(x, hi, lo)` and
`concat(hi, lo)` take bitvectors apart and put them together. Nodes are interned (equal nodes are the same object), so sharing is identity and
backends can memoize by node. `OPS` gives each operator's concrete meaning; constant folding here
and `sass.sem_eval` both use it, so the IR has exactly one definition.

Python operators build IR: + - * & | ^ ~ (unary -) << >> (logical) and the unsigned comparisons
< <= > >= == !=, which give 1-bit booleans. Plain ints take the other operand's width. An IR
value has no truth value: a Python `if` on one raises (use a ternary, which `@family` rewrites).
"""
from functools import reduce


def mask(w): return (1 << w) - 1
def signed(v, w): return v - (1 << w) if v >> (w - 1) & 1 else v


def identity(a):
    """A hashable key for nested tuples of expressions and plain values (expressions by identity)."""
    if isinstance(a, Expr): return ("e", id(a))
    if isinstance(a, tuple): return ("t", tuple(identity(x) for x in a))
    return ("v", a)


class Expr:
    __slots__ = ("op", "args", "w", "__weakref__")
    _interned = {}

    def __new__(cls, op, args, w):
        # Keyed by child identity: children are interned, and `==` on them builds IR.
        key = (op, identity(tuple(args)), w)
        node = cls._interned.get(key)
        if node is None:
            node = object.__new__(cls)
            node.op, node.args, node.w = op, tuple(args), w
            cls._interned[key] = node
        return node

    __hash__ = object.__hash__

    def __bool__(self):
        raise TypeError(f"dynamic condition {self!r} in a Python `if`/`while`/`and`/`or`; "
                        "use a ternary (`a if c else b`) or ite()")

    def __repr__(self):
        match self.op:
            case "const": return f"{self.args[0]:#x}:{self.w}"
            case "param": return f"{self.args[0]}:{self.w}"
            case "reg": return f"{self.args[0]}{self.args[1]}" + (f"x{self.args[2]}" if self.args[2] > 1 else "")
            case _: return f"({self.op} {' '.join(map(repr, self.args))})"

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
def reg(file, index, count, element=32): return Expr("reg", (file, index, count), count * element)
def load(space, address, w):
    """`w` bits (a multiple of 8) of memory space `space` at byte `address`, little-endian."""
    return Expr("load", (space, address), w)
def is_const(e, v=None): return isinstance(e, Expr) and e.op == "const" and (v is None or e.args[0] == v & mask(e.w))
def value(e): return e.args[0]


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
    "concat": lambda v, ws, w: v[0] << ws[1] | v[1],
}


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
        case "concat" if lo >= a[1].w: return extract(a[0], hi - a[1].w, lo - a[1].w)
        case "concat" if hi < a[1].w: return extract(a[1], hi, lo)
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
        out = narrowed(args[0], e.w, frozenset(memo.get(x, x) for x in shared)) if low(e) and uses.get(e.args[0], 0) == 1 else ite(*args) if e.op == "ite" else \
            extract(*args) if e.op == "extract" else Expr(e.op, tuple(args), e.w) if e.op in ("sel", "load") else op(e.op, *args, w=e.w)
        memo[e] = out
        return out
    return [walk(r) for r in roots]


def concat(*parts):
    """The bitvector of `parts`, most significant first."""
    return reduce(lambda hi, lo: op("concat", hi, lo, w=hi.w + lo.w), parts)


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


def complement(c):
    """not c, as the complementary comparison when c is one (one p-code operation, not two)."""
    a = c.args
    match c.op:
        case "eq": return compare("ne", *a)
        case "ne": return compare("eq", *a)
        case "ult": return compare("ule", a[1], a[0])
        case "ule": return compare("ult", a[1], a[0])
        case "slt": return compare("sle", a[1], a[0])
        case "sle": return compare("slt", a[1], a[0])
    return lnot(c)
def umin(a, b): a, b = pair(a, b); return ite(a < b, a, b)
def umax(a, b): a, b = pair(a, b); return ite(b < a, a, b)
def smin(a, b): a, b = pair(a, b); return ite(slt(a, b), a, b)
def smax(a, b): a, b = pair(a, b); return ite(slt(b, a), a, b)
def iabs(a): return ite(slt(a, 0), -a, a)
def any_of(*cs): return reduce(lambda x, y: x | y, cs)
def all_of(*cs): return reduce(lambda x, y: x & y, cs)


def substitute(e, mapping, memo=None):
    """`e` with leaves replaced: mapping(node) -> Expr or None (keep), applied to params and regs."""
    memo = {} if memo is None else memo
    if not isinstance(e, Expr): return e
    if e in memo: return memo[e]
    if e.op in ("param", "reg", "const"):
        m = mapping(e)
        out = e if m is None else m
    else:
        args = [substitute(a, mapping, memo) for a in e.args]
        out = ite(*args) if e.op == "ite" else extract(*args) if e.op == "extract" else op(e.op, *args, w=e.w)
    memo[e] = out
    return out


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


def show(outputs):
    """Readable text for [(destination, expression)]: infix operators, shared subexpressions bound
    once as tN (in evaluation order), constants with their width only when it isn't 32 bits."""
    uses, order = {}, []
    def count(e):
        if not isinstance(e, Expr): return
        uses[e] = uses.get(e, 0) + 1
        if uses[e] > 1: return
        for a in e.args: count(a)
        order.append(e)
    for _, e in outputs: count(e)
    names, lines = {}, []
    def number(e, signed=False):
        v = value(e)
        if signed and e.w > 1 and v >> (e.w - 1): return f"-{(1 << e.w) - v:#x}" + ("" if e.w == 32 else f":{e.w}")
        return text(e)
    def minmax(e):
        c, x, y = e.args
        if c.op in ("ult", "slt") and {c.args[0], c.args[1]} == {x, y}:
            kind = ("s" if c.op == "slt" else "u") + ("min" if c.args[0] is x else "max")
            return f"{kind}({text(x, True)}, {number(y, c.op == 'slt') if is_const(y) else text(y, True)})"
        return None
    def text(e, bare=False, define=False):
        if not isinstance(e, Expr): return repr(e)
        if e in names and not define: return names[e]
        a = e.args
        match e.op:
            case "const": return f"{a[0]:#x}" + ("" if e.w == 32 else f":{e.w}")
            case "param": return a[0]
            case "reg": return f"{a[0]}{a[1]}" if a[2] == 1 else f"{a[0]}{a[1]}:{a[0]}{a[1] + a[2] - 1}"
            case "load": return f"{a[0]}[{text(a[1], True)}]:{e.w}"
            case "ite" if (m := minmax(e)): return m
            case "ite": s = f"{text(a[0])} ? {text(a[1])} : {text(a[2])}"
            case "add" if is_const(a[1]) and value(a[1]) >> (e.w - 1) and e.w > 1:
                s = f"{text(a[0])} - {number(const(-value(a[1]), e.w))}"
            case "not" | "neg": return ("!" if e.op == "not" and e.w == 1 else "~" if e.op == "not" else "-") + text(a[0])
            case "zext" | "sext": return f"{e.op}{e.w}({text(a[0], True)})"
            case "extract": return f"{text(a[0])}[{a[1]}:{a[2]}]" if a[1] != a[2] else f"{text(a[0])}[{a[1]}]"
            case "concat": return f"concat({text(a[0], True)}, {text(a[1], True)})"
            case "ult" | "ule" | "slt" | "sle" if is_const(a[0]) and not is_const(a[1]):
                # Comparisons are canonically < / <=; print `x > k` the way it is usually written.
                s = f"{text(a[1])} {FLIPPED[e.op]} {number(a[0], e.op[0] == 's')}"
            case "slt" | "sle" if is_const(a[1]): s = f"{text(a[0])} {INFIX[e.op]} {number(a[1], True)}"
            case op if op in INFIX: s = f"{text(a[0])} {INFIX[op]} {text(a[1])}"
            case op: return f"{op}({', '.join(text(x, True) for x in a)})"
        return s if bare else f"({s})"
    for e in order:
        if uses[e] > 1 and e.op not in ("const", "param", "reg"):
            lines.append(f"t{len(names)} = {text(e, True, True)}")
            names[e] = f"t{len(names)}"
    lines += [f"{d} := {text(e, True)}" for d, e in outputs]
    return "\n".join(lines)
