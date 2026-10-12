"""Semantics families: Python functions over one md-level decoded instruction.

    @family('IADD3', 'UIADD3')
    def iadd3(i): ...
        return outputs(Rd=..., Pu=...)

`i` answers questions about the instruction. Static answers (class facts, modifier bits, enum
selectors, yes/no questions about numeric fields) are Python values; dynamic ones (operand values)
are `sass.sem` expressions. Backends supply the answers: `sass.sem_lift` from a decoded word,
`sass.sem_sleigh` by enumerating the static questions a family asks.

`@family` rewrites the function's conditional expressions and boolean operators so that one syntax
works for both binding times: `a if c else b`, `and`, `or` and `not` evaluate natively on static
values and build IR on dynamic ones.
"""
import ast, inspect, json, textwrap
from dataclasses import dataclass, field
from functools import cache
import mdlib
from sass import ir, sem
from sass.sem import Expr

FAMILIES = {}   # opcode -> family function


# ---- results ----------------------------------------------------------------------------------

class Unmodelled(Exception):
    """A valid encoding whose semantics this lifter doesn't implement."""


class Illegal(Exception):
    """An encoding the hardware rejects or leaves undefined although the md accepts it."""
    def __init__(self, reason, evidence):
        super().__init__(reason); self.evidence = evidence


def unmodelled(reason): raise Unmodelled(reason)
def illegal(reason, evidence): raise Illegal(reason, evidence)


@dataclass
class Outputs:
    writes: dict                 # operand name -> Expr, evaluated simultaneously
    order: tuple = ()            # write order for aliased destinations (last wins)

    def ordered(self):
        names = [n for n in self.writes if n not in self.order] + [n for n in self.order if n in self.writes]
        return [(n, self.writes[n]) for n in names]


def outputs(order=(), **writes): return Outputs(writes, tuple(order))


# ---- native conditionals: the rewrite target --------------------------------------------------

def _cond(c, a, b):
    if isinstance(c, Expr): return sem.ite(c, a(), b())
    return a() if c else b()


def _and(a, b):
    if not isinstance(a, Expr): return b() if a else a
    b = b()
    if not isinstance(b, Expr): return a if b else sem.const(0, 1)
    return a & b


def _or(a, b):
    if not isinstance(a, Expr): return a if a else b()
    b = b()
    if not isinstance(b, Expr): return sem.const(1, 1) if b else a
    return a | b


def _not(a): return sem.lnot(a)


class Rewrite(ast.NodeTransformer):
    @staticmethod
    def thunk(node): return ast.Lambda(ast.arguments([], [], None, [], [], None, []), node)

    def call(self, name, *args): return ast.Call(ast.Name(name, ast.Load()), list(args), [])

    def visit_IfExp(self, node):
        self.generic_visit(node)
        return self.call("_sem_cond", node.test, self.thunk(node.body), self.thunk(node.orelse))

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        name = "_sem_and" if isinstance(node.op, ast.And) else "_sem_or"
        out = node.values[-1]
        for v in reversed(node.values[:-1]): out = self.call(name, v, self.thunk(out))
        return out

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        return self.call("_sem_not", node.operand) if isinstance(node.op, ast.Not) else node


def rewrite(fn):
    """fn recompiled with conditionals routed through _cond/_and/_or/_not (same globals, lines)."""
    if fn.__code__.co_freevars: raise TypeError(f"{fn.__name__}: families must be module-level functions")
    lines, first = inspect.getsourcelines(fn)
    tree = ast.parse(textwrap.dedent("".join(lines)))
    (fdef,) = tree.body
    fdef.decorator_list = []
    tree = ast.fix_missing_locations(Rewrite().visit(tree))
    ast.increment_lineno(tree, first - 1)
    env = fn.__globals__
    env.update(_sem_cond=_cond, _sem_and=_and, _sem_or=_or, _sem_not=_not)
    exec(compile(tree, inspect.getsourcefile(fn), "exec"), env)
    out = env[fn.__name__]
    out.__wrapped__ = fn
    return out


def family(*opcodes):
    def register(fn):
        out = rewrite(fn)
        for op in opcodes:
            if op in FAMILIES: raise ValueError(f"{op} already has a family")
            FAMILIES[op] = out
        return out
    return register


def helper(fn):
    """Rewrite a module-level helper used by families, so its conditionals stage too."""
    return rewrite(fn)


# ---- md facts about a class -------------------------------------------------------------------

MODIFIERS = ("negate", "absolute", "invert", "not")
REG_TYPES = {"Register": "R", "NonZeroRegister": "R", "ZeroRegister": "R",
             "UniformRegister": "UR", "NonZeroUniformRegister": "UR", "ZeroUniformRegister": "UR",
             "Predicate": "P", "UniformPredicate": "UP"}


@cache
def roles(arch_name):
    return {r["cls"]: r for p in (mdlib.OUT / arch_name / "semantics").glob("*.json") for r in json.loads(p.read_text())}


@cache
def all_labels(enum_type):
    """Every label of an enum type on any architecture (for checking label comparisons)."""
    from sass.build_languages import ARCHES
    return frozenset(l for a in ARCHES for l in ir.load(a).enums.get(enum_type, {}))


def encoded(k):
    """Keys the encoding sets: operand names and (name, attr) pairs."""
    out = set()
    for _, r in k.rules:
        match r:
            case ("op", name, *_): out.add(name)
            case ("attr", name, attr): out.add((name, attr))
            case ("table", _, args): out |= {a[1] if a[0] == "op" else (a[1], a[2]) for a in args if a[0] != "const"}
    return out


class Label(str):
    """An enum label; comparisons check the other side is a label of this enum type somewhere."""
    def __new__(cls, label, names, enum_type):
        s = super().__new__(cls, label); s.names, s.enum_type = names, enum_type; return s

    def check(self, other):
        if other not in all_labels(self.enum_type): raise ValueError(f"{other!r} is not a {self.enum_type} label on any architecture")

    def __eq__(self, other):
        if not isinstance(other, str): return NotImplemented
        self.check(other); return other in self.names

    def __ne__(self, other): r = self.__eq__(other); return r if r is NotImplemented else not r
    __hash__ = str.__hash__


# ---- the instruction view ---------------------------------------------------------------------

class Instruction:
    """Questions a family may ask. Backends implement `answer` (static facts about the encoding),
    `operand` (an operand's dynamic value as encoded) and `span`."""

    def __init__(self, arch, klass, fp="exact"):
        self.arch, self.k, self.fp = arch, klass, fp
        self.cls, self.opcode = klass.name, klass.mnemonic
        self._encoded = encoded(klass)

    # Backend interface.
    def answer(self, key, domain):
        """The value of a static encoding fact: key is a name, (name, attr) or ('zero', name)."""
        raise NotImplementedError

    def operand(self, name, width):
        """Dynamic value of operand `name` as encoded (no modifiers), `width` bits."""
        raise NotImplementedError

    def span(self, name):
        """Register count of a register operand."""
        raise NotImplementedError

    # Names. Each name also matches its uniform-register spelling (Ra: URa, Pu: UPu), so one
    # family covers an opcode and its uniform twin (IADD3, UIADD3).
    def candidates(self, names):
        ns = names.split("|")
        return ns + ["U" + n for n in ns]

    def resolve(self, names):
        for n in self.candidates(names):
            if n in self.k.operand_types: return n
        raise KeyError(f"{self.cls} has none of the operands {names}")

    def has_operand(self, names): return any(n in self.k.operand_types for n in self.candidates(names))

    def atom(self, name): return self.k.operand_types[name]

    def kind(self, names):
        a = self.atom(self.resolve(names))
        if a.type in REG_TYPES:
            return {"R": "register", "UR": "uniform", "P": "predicate", "UP": "predicate"}[REG_TYPES[a.type]]
        if a.type in ("C", "CX", "SpecialRegister"): return {"C": "constant", "CX": "cx", "SpecialRegister": "special-register"}[a.type]
        if a.type.startswith("F") and a.type.endswith("Imm"): return "float-immediate"
        if a.type.endswith("Imm"): return "immediate"
        return "selector" if a.type in self.arch.enums else "other"

    def width(self, names):
        name = self.resolve(names); a = self.atom(name)
        if a.type in REG_TYPES: return self.span(name) * (1 if REG_TYPES[a.type] in ("P", "UP") else 32)
        bits = int((a.args.split("/")[0] or "32").split(",")[0]) if a.args and a.type.endswith("Imm") else 32
        return {"F16Imm": 16, "F64Imm": 64}.get(a.type, 64 if bits > 32 else 32)

    # Static facts about the encoding.
    def modifier(self, names, attr):
        name = self.resolve(names)
        return bool(self.answer((name, attr), (0, 1))) if (name, attr) in self._encoded else False

    def operand_negated(self, names): return self.modifier(names, "negate")
    def operand_absolute(self, names): return self.modifier(names, "absolute")
    def operand_inverted(self, names): return self.modifier(names, "invert")
    def operand_notted(self, names): return self.modifier(names, "not")

    def sel(self, names):
        name = self.resolve(names); a = self.atom(name)
        if a.type not in self.arch.enums: raise TypeError(f"{self.cls}.{name} ({a.type}) is not a selector")
        enum = self.arch.enums[a.type]
        if name in self._encoded: v = self.answer(name, tuple(sorted(set(enum.values()))))
        elif a.default is not None: v = enum[a.default]
        elif len(set(enum.values())) == 1: v = next(iter(enum.values()))
        else: raise KeyError(f"{self.cls}.{name} is neither encoded nor defaulted")
        names_of = [l for l, x in self.arch.enum_defs[a.type] if x == v]
        return Label(self.arch.rev_enums[a.type][v], frozenset(names_of), a.type)

    def default(self, names): return self.atom(self.resolve(names)).default

    def on(self, names):
        """The selector is set to a value other than its md default."""
        name = self.resolve(names); a = self.atom(name)
        if a.default is None: raise TypeError(f"{self.cls}.{name} has no default")
        return self.sel(name) != a.default if a.default in all_labels(a.type) else False

    def is_zero(self, names):
        """Register operand is RZ/URZ, predicate is PT (the md's constant register), immediate is 0."""
        name = self.resolve(names)
        return bool(self.answer(("zero", name), (False, True))) if name in self._encoded else self.zero_default(name)

    def is_true(self, names):
        if self.kind(names) != "predicate": raise TypeError(f"{names} is not a predicate")
        return self.is_zero(names)

    def zero_default(self, name):
        a = self.atom(name)
        return a.default in ("RZ", "URZ", "PT", "UPT") or (a.default or "").startswith("0")

    # Dynamic values.
    def value(self, names, modifiers=True, width=None):
        name = self.resolve(names)
        v = self.operand(name, width or self.width(name))
        if not modifiers: return v
        if self.operand_absolute(name): v = sem.iabs(v)
        if self.operand_negated(name): v = -v
        if self.operand_inverted(name): v = ~v
        return v

    def fvalue(self, names, modifiers=True, width=None):
        name = self.resolve(names)
        v = self.operand(name, width or self.width(name))
        if not modifiers: return v
        sign = 1 << v.w - 1
        if self.operand_absolute(name): v = v & (sign - 1)
        if self.operand_negated(name): v = v ^ sign
        return v

    def pred(self, names, modifiers=True):
        name = self.resolve(names)
        v = self.operand(name, 1)
        return sem.lnot(v) if modifiers and self.operand_notted(name) else v

    def imm(self, names, width=None):
        if self.kind(names) not in ("immediate", "float-immediate"): raise TypeError(f"{names} is not an immediate")
        return self.value(names, modifiers=False, width=width)
