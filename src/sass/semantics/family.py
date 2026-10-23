"""Semantics families: Python functions over one md-level decoded instruction.

    @family('IADD3', 'UIADD3')
    def iadd3(i): ...
        return outputs(Rd=..., Pu=...)

`i` answers questions about the instruction. Static answers (class facts, modifier bits, enum
selectors, yes/no questions about numeric fields) are Python values; dynamic ones (operand values)
are `sass.semantics.bitvec` expressions. Backends supply the answers: `sass.semantics.lifter` from a decoded word,
`sass.sleigh.lower` by enumerating the static questions a family asks.

`@family` rewrites the function's conditional expressions and boolean operators so that one syntax
works for both binding times: `a if c else b`, `and`, `or` and `not` evaluate natively on static
values and build IR on dynamic ones.
"""
import ast, inspect, json, string, textwrap
from dataclasses import dataclass, field
from functools import cache
from sass.md import parse
from sass.decode import ir
from sass.semantics import bitvec
from sass.semantics.bitvec import Expr

FAMILIES = {}   # opcode -> [(class filter or None, family function)]


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
    flow: "Flow | None" = None   # control transfer after the writes
    stores: tuple = ()           # Store(space, address, value), after the register writes
    effects: tuple = ()          # Effect(name, args): side-effecting calls, before everything else

    def ordered(self):
        names = [n for n in self.writes if n not in self.order] + [n for n in self.order if n in self.writes]
        return [(n, self.writes[n]) for n in names]


def outputs(order=(), flow=None, stores=(), effects=(), **writes):
    return Outputs(writes, tuple(order), flow, tuple(stores), tuple(effects))


@dataclass(frozen=True)
class Store:
    """Write value (a whole number of bytes) to memory space `space` at byte `address` (64 bits),
    when cond (1 bit) is nonzero or always (None)."""
    space: str
    address: Expr
    value: Expr
    cond: Expr | None = None


@dataclass(frozen=True)
class Effect:
    """A call to the pcodeop `name` with Expr arguments and no result (memory ordering events,
    synchronization primitives)."""
    name: str
    args: tuple


def store(space, address, value, cond=None): return Store(space, address, value, cond)
def effect(name, *args): return Effect(name, tuple(args))


def primitive_names(result):
    """The runtime primitives an Outputs needs: its primitive values' and primitive effects' names."""
    memo = {}
    roots = [bitvec.lowered(r, memo) for r in (*result.writes.values(), *(e for s in result.stores for e in (s.address, s.value, s.cond)),
             *(a for e in result.effects for a in e.args), *((result.flow.target, result.flow.cond) if result.flow else ()))]
    return {e.name.removeprefix(bitvec.PRIMITIVE_OP) for e in result.effects if e.name.startswith(bitvec.PRIMITIVE_OP)} | \
        {n.args[0] for r in roots if isinstance(r, Expr) for n in bitvec.leaves(r, "primitive")}


@dataclass(frozen=True)
class Flow:
    """A control transfer: kind goto | call | return, to a 64-bit absolute target, taken when
    cond (1 bit) is nonzero or always (None). A target from i.branch_target is a direct edge."""
    kind: str
    target: Expr
    cond: Expr | None = None


def goto(target, cond=None): return Flow("goto", target, cond)
def call(target, cond=None): return Flow("call", target, cond)
def ret(target, cond=None): return Flow("return", target, cond)


# ---- native conditionals: the rewrite target --------------------------------------------------

def _cond(c, a, b):
    if isinstance(c, Expr): return bitvec.ite(c, a(), b())
    return a() if c else b()


def _and(a, b):
    if not isinstance(a, Expr): return b() if a else a
    b = b()
    if not isinstance(b, Expr): return a if b else bitvec.const(0, 1)
    return a & b


def _or(a, b):
    if not isinstance(a, Expr): return a if a else b()
    b = b()
    if not isinstance(b, Expr): return bitvec.const(1, 1) if b else a
    return a | b


def _not(a): return bitvec.lnot(a)


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


def family(*opcodes, classes=None):
    """Register fn for every md class of `opcodes`, or only those `classes(klass)` accepts (while an
    opcode is partly migrated, the other classes keep their old emitter). Unmodelled encodings use
    the opaque call."""
    def register(fn):
        staged = rewrite(fn)
        def checked(i):
            result = staged(i)
            i.require_unasked_defaults()
            return result
        checked.__name__, checked.__wrapped__ = fn.__name__, fn
        for op in opcodes: FAMILIES.setdefault(op, []).append((classes, checked))
        return checked
    return register


def family_for(klass):
    """The family function covering an md class, or None."""
    import sass.semantics.families  # registers the families (they import this module)
    return next((fn for accepts, fn in FAMILIES.get(klass.mnemonic, ()) if accepts is None or accepts(klass)), None)


def helper(fn):
    """Rewrite a module-level helper used by families, so its conditionals stage too."""
    return rewrite(fn)


DEFINITIONS = {}   # defined operator -> (function, {operand count: (the arguments its name fixes, the kept ones' shapes)})


def same(a, b):
    """Equal plain values or constants (an int and a constant of its value too), identical expressions."""
    if isinstance(a, tuple) or isinstance(b, tuple):
        return type(a) is type(b) and len(a) == len(b) and all(map(same, a, b))
    if isinstance(a, Expr) and isinstance(b, Expr): return a is b
    if isinstance(b, Expr): a, b = b, a
    return bitvec.is_const(a) and isinstance(b, int) and a.args[0] == b & bitvec.mask(a.w) if isinstance(a, Expr) else a == b


def definition(name):
    """A defined operator: a call builds `op(label, *arguments)`, which a consumer sees as one
    operation and backends compute by its expansion (bitvec.lowered: the function, staged like a
    helper, applied to the arguments). `name` is a template over parameters that are plain values,
    which are then not arguments, or namer(arguments) -> (label, kept parameter names) for an
    operation named by its constant decode-time arguments: the others must be the same in every call
    of one label and operand count (checked), since the label stands for them. Tuple arguments
    (Dest, Rounding) are their fields. The docstring's first paragraph describes it."""
    def register(fn):
        staged, signature = rewrite(fn), inspect.signature(fn)
        description = " ".join(inspect.getdoc(fn).split("\n\n")[0].split())
        fields = {f for _, f, _, _ in string.Formatter().parse(name) if f} if isinstance(name, str) else set()
        widths = {}
        def defined(*args, **kwargs):
            bound = signature.bind(*args, **kwargs); bound.apply_defaults()
            arguments = bound.arguments
            if isinstance(name, str): label, kept = name.format(**{f: arguments[f] for f in fields}), [k for k in arguments if k not in fields]
            else: label, kept = name(arguments)
            fixed = {k: v for k, v in arguments.items() if k not in kept}
            shapes = [(k, type(arguments[k]) if isinstance(arguments[k], tuple) else None) for k in kept]
            operands = [x for k in kept for x in (arguments[k] if isinstance(arguments[k], tuple) else (arguments[k],))]
            if label not in DEFINITIONS:
                if label in bitvec.OPS or label in BUILTIN: raise TypeError(f"{label} is an IR operator")
                DEFINITIONS[label] = fn, {}
                bitvec.DEFINED[label] = bitvec.Definition(expander(staged, DEFINITIONS[label][1]), description)
            owner, forms = DEFINITIONS[label]
            if owner is not fn: raise TypeError(f"{label} names {owner.__name__} and {fn.__name__}")
            known, known_shapes = forms.setdefault(len(operands), (fixed, shapes))
            if known_shapes != shapes or known.keys() != fixed.keys() or not all(same(known[k], fixed[k]) for k in fixed):
                raise TypeError(f"{label} doesn't determine {fn.__name__}'s arguments: {known} and {fixed}")
            key = (label, *(x.w if isinstance(x, Expr) else None for x in operands))
            if key not in widths: widths[key] = staged(**arguments).w
            return bitvec.op(label, *operands, w=widths[key])
        defined.__name__, defined.__wrapped__, defined.body = fn.__name__, fn, staged
        return defined
    return register


BUILTIN = {"const", "param", "reg", "load", "ite", "sel", "primitive", "tuple"}


def expander(staged, forms):
    """The expansion of a label's operands: the fixed arguments of its form (by operand count) and
    the kept ones rebuilt."""
    def expand(*operands):
        fixed, shapes = forms[len(operands)]
        rest, kept = list(operands), {}
        for k, t in shapes:
            if t is None: kept[k] = rest.pop(0)
            else: n = len(t._fields); kept[k], rest = t(*rest[:n]), rest[n:]
        return staged(**fixed, **kept)
    return expand


# ---- md facts about a class -------------------------------------------------------------------

MODIFIERS = ("negate", "absolute", "invert", "not")
PR_BITS = 7   # the md's PR connector: predicates P0-P6 as one value, P0 the low bit
REG_TYPES = {"Register": "R", "NonZeroRegister": "R", "ZeroRegister": "R",
             "UniformRegister": "UR", "NonZeroUniformRegister": "UR", "ZeroUniformRegister": "UR",
             "Predicate": "P", "Predicate_vimnmx": "P", "UniformPredicate": "UP"}


@cache
def roles(arch_name):
    return {r["cls"]: r for p in (parse.MD / arch_name / "semantics").glob("*.json") for r in json.loads(p.read_text())}


def compound(k):
    """Operands with bracketed parts (C:Sb[bank][offset], CX, A, DESC, ...): values, not selectors,
    even where their type names an md enum too (C)."""
    f = k.format
    return {x.name for x, y in zip(f, f[1:]) if x.kind == "operand" and y.kind == "open"}


@cache
def predicates(arch_name):
    """md PREDICATES per class (IDEST_SIZE, ISRC_A_SIZE, ...; bits)."""
    return {name: c["preds"] for name, c in parse.arch_classes(arch_name).items()}


def constant_width(arch_name, cls, name):
    """Bits of a constant-bank operand's value: its source slot's md ISRC_<slot>_SIZE (Sc:
    ISRC_C_SIZE; corrected md errors in src/sass/md/fixes.py), else 32. A CX operand's md size is
    its 64-bit header's, so its value has the width of the same operand in the class group's
    c[][] form (IMAD.WIDE's cx[][] addend: 64 bits, SM89 GPU)."""
    if getattr(md_classes(arch_name)[cls].operand_types.get(name), "type", None) == "CX":
        direct = direct_constant(arch_name, cls, name)
        return constant_width(arch_name, direct, name) if direct else 32
    size = predicates(arch_name).get(cls, {}).get(f"ISRC_{name[-1].upper()}_SIZE") if name[:1] == "S" else None
    return size if size in (32, 64) else 32


@cache
def md_classes(arch_name): return {k.name: k for k in ir.load(arch_name).classes}


@cache
def direct_constant(arch_name, cls, name):
    """The class in `cls`'s md group (its name before "__") whose operand `name` is a c[][] constant."""
    group = cls.split("__")[0]
    return next((k.name for k in md_classes(arch_name).values() if not k.alternate and k.name.split("__")[0] == group
                 and getattr(k.operand_types.get(name), "type", None) == "C"), None)


@cache
def operand_labels(name):
    """Every label any operand called `name` can have, in any class on any architecture. One family
    serves classes whose selectors have different enum types (DSTFMT, DSTFMT_uf2fp), so a label of
    the operand elsewhere is a legitimate comparison; anything else is a typo or a wrong selector."""
    from sass.sleigh.build import ARCHES
    return frozenset(l for a in ARCHES for k in ir.load(a).classes if (t := k.operand_types.get(name))
                     for l in ir.load(a).enums.get(t.type, ()))


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
    """An enum label; comparisons check the other side is a label this operand has somewhere: in its
    own enum (`labels`), else on any architecture (which loads every one, so only then)."""
    def __new__(cls, label, names, operand, labels):
        s = super().__new__(cls, label); s.names, s.operand, s.labels = names, operand, labels; return s

    def check(self, other):
        if other not in self.labels and other not in operand_labels(self.operand):
            raise ValueError(f"{other!r} is not a label of any {self.operand} operand on any architecture")

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
        self.asked = set()   # selectors and (operand, modifier) keys the family asked about

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

    def has_operand(self, names):
        present = [n for n in self.candidates(names) if n in self.k.operand_types]
        self.asked.update(present)
        return bool(present)

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
        if a.type in ("C", "CX"): return constant_width(self.arch.name, self.cls, name)
        if a.type == "PR": return PR_BITS
        bits = int((a.args.split("/")[0] or "32").split(",")[0]) if a.args and a.type.endswith("Imm") else 32
        return {"F16Imm": 16, "F32Imm": 32, "F64Imm": 64}.get(a.type, 64 if bits > 32 else 32)

    # Static facts about the encoding.
    def modifier(self, names, attr):
        name = self.resolve(names)
        self.asked.add((name, attr))
        return bool(self.answer((name, attr), (0, 1))) if (name, attr) in self._encoded else False

    def operand_negated(self, names): return self.modifier(names, "negate")
    def operand_absolute(self, names): return self.modifier(names, "absolute")
    def operand_inverted(self, names): return self.modifier(names, "invert")
    def operand_notted(self, names): return self.modifier(names, "not")

    def select_fork(self, names):
        """The selector's label as a Python value: the family runs once per label, and in SLEIGH
        every label is its own body (expensive for large bodies; select_lookup picks a value instead)."""
        name = self.resolve(names); a = self.atom(name)
        if a.type not in self.arch.enums: raise TypeError(f"{self.cls}.{name} ({a.type}) is not a selector")
        enum = self.arch.enums[a.type]
        self.asked.add(name)
        if name in self._encoded: v = self.answer(name, tuple(sorted(set(enum.values()))))
        elif a.default is not None: v = enum[a.default]
        elif len(set(enum.values())) == 1: v = next(iter(enum.values()))
        else: raise KeyError(f"{self.cls}.{name} is neither encoded nor defaulted")
        names_of = [l for l, x in self.arch.enum_defs[a.type] if x == v]
        return Label(self.arch.rev_enums[a.type][v], frozenset(names_of), name, enum.keys())

    def default(self, names): return self.atom(self.resolve(names)).default

    def select_lookup(self, names, table):
        """The value a selector's label chooses, as one value: `table` maps labels to expressions
        (None or absent: unmodelled). Unlike `sel`, the backend doesn't run the family per label:
        lift gets the chosen value, SLEIGH a decode-time choice, so a computation using it is
        built once (FSETP's comparison serves both predicates)."""
        name = self.resolve(names); a = self.atom(name)
        enum = self.arch.enums[a.type]
        self.asked.add(name)
        table = {l: e for l, e in table.items() if e is not None}
        if name not in self._encoded:
            label = self.select_fork(name)
            e = next((x for l, x in table.items() if label == l), None)
            if e is None: unmodelled(f"{name} {label}")
            return e
        return self.choice(name, {enum[l]: e for l, e in table.items() if l in enum})

    def select_bits(self, names, lo, bits, table):
        """Choose an expression by a small bit slice of an immediate, without enumerating
        unrelated slices (PRMT's four independent selector nibbles)."""
        name = self.resolve(names); self.asked.add(name)
        if self.kind(name) != "immediate": raise TypeError(f"{name} is not an immediate")
        if lo < 0 or bits < 1 or lo + bits > self.width(name): raise ValueError("invalid immediate bit slice")
        return self.choice_bits(name, lo, bits, table)

    def choice_bits(self, name, lo, bits, table):
        raise NotImplementedError

    def select_in(self, names, labels):
        """Whether the selector's label is one of `labels`: one fork for the set, not one per label
        (a select_lookup among them then shares a body)."""
        name = self.resolve(names); a = self.atom(name)
        enum = self.arch.enums[a.type]
        self.asked.add(name)
        if name not in self._encoded: return self.select_fork(name) in labels
        return bool(self.answer(("in", name, frozenset(enum[l] for l in labels if l in enum)), (True, False)))

    def select_lookup_const(self, names, table, w=32):
        """A w-bit number chosen by a selector's label (format constants, rounding-mode flags):
        select_lookup with constants, so one parametric computation serves every label."""
        return self.select_lookup(names, {l: bitvec.const(v, w) for l, v in table.items()})

    def labels(self, names):
        """The labels the class's selector can take (its md enum's printed names)."""
        a = self.atom(self.resolve(names))
        return sorted(set(self.arch.rev_enums[a.type].values()))

    def choice(self, name, values):
        """Backend: values[encoded value] of an encoded selector (encoded value -> expression)."""
        raise NotImplementedError

    def on(self, names):
        """The selector is set to a value other than its md default."""
        name = self.resolve(names); a = self.atom(name)
        if a.default is None: raise TypeError(f"{self.cls}.{name} has no default")
        return self.select_fork(name) != a.default

    def require_unasked_defaults(self):
        """A family that never asked about or read a selector, modifier bit or defaulted numeric
        operand models only its md default: other values are unmodelled (an IADD family silent on
        .SAT must not claim IADD.SAT; MOV ignores PixMaskU04 only at 0xf). Scheduling
        hints and .reuse don't change what the instruction does; the guard is the framework's."""
        from sass.md import legal
        hints = legal.hints(self.k)
        guard = next((a.name for a in self.k.format if a.kind == "guard"), None)   # applied by the framework
        selectors = {n for n, a in self.k.operand_types.items()
                     if a.type in self.arch.enums and a.type not in REG_TYPES and n not in compound(self.k)}
        for key in sorted((self._encoded | selectors) - self.asked, key=repr):
            if guard and (key == guard or key == (guard, "not")): continue
            if isinstance(key, tuple):
                if key[1] in MODIFIERS and self.answer(key, (0, 1)): unmodelled(f"{key[0]}.{key[1]}")
                continue
            a = self.k.operand_types.get(key)
            if a is None or key in hints or a.type in REG_TYPES or key in compound(self.k): continue
            if a.type.endswith("Imm"):
                default = (a.args or "").split("/")[1:2]
                if default and default[0] and not default[0].endswith("*"):
                    if not self.answer(("in", key, frozenset({int(default[0], 0)})), (True, False)): unmodelled(f"{key} other than {default[0]}")
                continue
            if a.type not in self.arch.enums: continue
            # A selector without a default names the class's variant (/ONLY64:size, /XORONLY:xor).
            if a.default is None: unmodelled(f"{key} not modelled")
            enum = self.arch.enums[a.type]
            if a.default not in enum: unmodelled(f"{key} default {a.default} is not a value")
            if key in self._encoded and not self.answer(("in", key, frozenset({enum[a.default]})), (True, False)):
                unmodelled(f"{key} other than {a.default}")

    def ignore(self, names):
        """The operand (and its modifier bits) doesn't affect this encoding's result (ISETP's Pr
        without .EX): considered, so its value isn't required to be the default."""
        name = self.resolve(names)
        self.asked |= {name} | {(name, m) for m in MODIFIERS}

    def static(self, names, max_bits=8):
        """A small immediate's value as a Python int, for a computation specialized per value (LOP3
        truth tables). SLEIGH enumerates every encodable value, so only fields of at most
        `max_bits` bits; other immediates stay dynamic (`value`)."""
        name = self.resolve(names); self.asked.add(name)
        if self.kind(name) != "immediate": raise TypeError(f"{self.cls}.{name} is not an immediate")
        width = next((f.width for f, r in self.k.rules if r[0] == "op" and r[1] == name), None)
        if width is None or width > max_bits: raise TypeError(f"{self.cls}.{name}: {width}-bit field is too wide to enumerate")
        return self.answer(name, tuple(range(1 << width)))

    def static_low(self, names, bits):
        """The low `bits` bits of a wide immediate as a Python int (UP2UR's predicate mask), the
        rest of the field ignored: SLEIGH constrains only those bits."""
        name = self.resolve(names); self.asked.add(name)
        if self.kind(name) != "immediate": raise TypeError(f"{self.cls}.{name} is not an immediate")
        return self.answer(("low", name, bits), tuple(range(1 << bits)))

    def is_zero(self, names):
        """Register operand is RZ/URZ, predicate is PT (the md's constant register), immediate is 0."""
        name = self.resolve(names); self.asked.add(name)
        return bool(self.answer(("zero", name), (False, True))) if name in self._encoded else self.zero_default(name)

    def is_true(self, names):
        if self.kind(names) != "predicate": raise TypeError(f"{names} is not a predicate")
        return self.is_zero(names)

    def zero_default(self, name):
        a = self.atom(name)
        return a.default in ("RZ", "URZ", "PT", "UPT") or (a.default or "").startswith("0")

    # Dynamic values.
    def modified(self, name, attr, v, f):
        """v, or f(v) when the operand's modifier bit `attr` is set. Backends may answer without
        enumerating the bit (SLEIGH: a decode-time choice), since it changes only this value."""
        return f(v) if self.modifier(name, attr) else v

    def value(self, names, modifiers=True, width=None):
        name = self.resolve(names); self.asked.add(name)
        v = self.operand(name, width or self.width(name))
        if not modifiers: return v
        v = self.modified(name, "absolute", v, bitvec.iabs)
        v = self.modified(name, "negate", v, lambda x: -x)
        return self.modified(name, "invert", v, lambda x: ~x)

    def fvalue(self, names, modifiers=True, width=None):
        name = self.resolve(names); self.asked.add(name)
        v = self.operand(name, width or self.width(name))
        if not modifiers: return v
        sign = 1 << v.w - 1
        v = self.modified(name, "absolute", v, lambda x: x & (sign - 1))
        return self.modified(name, "negate", v, lambda x: x ^ sign)

    def pred(self, names, modifiers=True):
        name = self.resolve(names); self.asked.add(name)
        v = self.operand(name, 1)
        return self.modified(name, "not", v, bitvec.lnot) if modifiers else v

    def encoding(self, names, w=32):
        """A selector's encoded value as a w-bit expression, for a call that interprets it itself
        (memory-ordering controls): the family models every label."""
        name = self.resolve(names); self.asked.add(name)
        return bitvec.param(f"__enc:{name}", w)

    def special(self, names, w):
        """The special register (view of w bits) a SpecialRegister operand names; SRZ reads 0."""
        name = self.resolve(names); self.asked.add(name)
        return bitvec.param(f"__sr:{name}", w)

    def special_register(self, name):
        """The 32-bit special register `name` (SR_SWINLO), read whatever the encoding."""
        return bitvec.reg("SR", self.arch.enums["SpecialRegister"][name], 1)

    def branch_target(self, names):
        """The absolute address an RSImm (relative to the next instruction) or UImm (absolute)
        branch operand names: a direct target, unlike an address computed from values."""
        name = self.resolve(names); self.asked.add(name)
        return bitvec.param(f"__target:{name}", 64)

    def imm(self, names, width=None):
        if self.kind(names) not in ("immediate", "float-immediate"): raise TypeError(f"{names} is not an immediate")
        return self.value(names, modifiers=False, width=width)
