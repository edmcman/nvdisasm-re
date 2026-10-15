"""SLEIGH backend for semantics families: the only Ghidra-aware part of the semantics.

Ghidra decodes at run time, so a class's semantics must cover every word of the class at once:
1. Enumerate: run the family with operand placeholders, branching depth-first over each static
   question it asks (modifier bits, selectors, zero tests); unsatisfiable answers are skipped.
2. Split on dead outputs: a predicate destination encoded as PT is discarded, so each liveness
   combination gets its own body (all-PT carries leave a 32-bit add).
3. Merge the rows by anti-unification: shared structure is kept; where rows differ, the subterm
   becomes a decode-time choice (`sel`) over the fewest static keys that determine it.
4. Lower to p-code text through the Builder (`src/sass/semantics/operands.py`): shared nodes become locals,
   dynamic choices labelled diamonds, every value is computed before any write. Unmodelled rows
   keep the complete opaque fallback via `operations.dispatch`.
"""
import itertools
from functools import cache
from collections import defaultdict
from dataclasses import dataclass
from sass.semantics import bitvec
from sass.semantics.bitvec import Expr
from sass.semantics.family import Primitive, Instruction, Illegal, Unmodelled, PR_BITS, REG_TYPES, family_for
import sass.semantics.families  # registers the families

ELEMENT = {"R": 32, "UR": 32, "P": 1, "UP": 1}   # bits per register element
def nbytes(w): return (w + 7) // 8


class Branch(Exception):
    def __init__(self, key, domain): self.key, self.domain = key, domain


def builder_key(key):
    """A static key as the Builder spells selectors: name, 'name@attr', ('zero', name) or
    ('in', name, values)."""
    if isinstance(key, tuple) and key[0] == "in": return key[1]
    return f"{key[0]}@{key[1]}" if isinstance(key, tuple) and key[0] != "zero" else key


class Symbolic(Instruction):
    """Answers from an assignment of static keys; an unassigned key raises Branch."""

    def __init__(self, b, assignment):
        super().__init__(b.g.arch, b.k, b.g.fp)
        self.b, self.assignment = b, assignment

    def answer(self, key, domain):
        if key not in self.assignment: raise Branch(key, [v for v in domain if clauses(self.b, key, v) is not None])
        return self.assignment[key]

    def operand(self, name, width):
        # A register the class can only encode as RZ/URZ/PT (IMAD.MOV's Ra, Rb) is its constant, so
        # the expression folds; lift likewise reads the zero register as a constant.
        atom = self.atom(name)
        if atom.type in REG_TYPES and name in self._encoded and clauses(self.b, ("zero", name), False) is None:
            return bitvec.const(1 if REG_TYPES[atom.type] in ("P", "UP") else 0, width)
        return bitvec.param(name, width)

    def modified(self, name, attr, v, f):
        # A decode-time choice of this value, not a branch of every row on the bit; except for a
        # loaded operand, whose load each choice table would repeat (rows keep it in one body).
        key = (name, attr)
        if key not in self._encoded: return v
        if getattr(self.b.values.get(name), "kind", None) in LOADS: return super().modified(name, attr, v, f)
        self.asked.add(key)
        cases = [((bit,), t) for bit, t in ((0, v), (1, f(v))) if clauses(self.b, key, bit) is not None]
        return cases[0][1] if len(cases) == 1 or cases[0][1] is cases[-1][1] else sel((key,), cases, v.w)

    def span(self, name):
        v = self.b.outputs.get(name) or self.b.values.get(name)
        return v.size // nbytes(ELEMENT[REG_TYPES[self.atom(name).type]])

    def addr(self): return bitvec.param("__inst_start", 64)

    def choice(self, name, values):
        # One yes/no question (in the table or not), then a decode-time choice of the value.
        if not self.answer(("in", name, frozenset(values)), (True, False)): raise Unmodelled(f"{name} outside the table")
        cases = [((v,), e) for v, e in sorted(values.items()) if clauses(self.b, name, v) is not None]
        if all(e is cases[0][1] for _, e in cases): return cases[0][1]
        return sel((name,), cases, cases[0][1].w)


def clauses(b, key, v):
    """Encoding clauses for static key == v; None if no encoding has that value."""
    from sass.sleigh.gen import pattern
    k = b.k
    def where(name, test):
        # A value the constructor's pattern fixes (a pseudo-op's RZ) holds or not, with no clauses.
        if name in b.known: return [] if test(b.known[name]) else False
        return b.g.values_where(k, name, test)
    if isinstance(key, tuple) and key[0] == "in":
        c = where(key[1], (lambda x: x in key[2]) if v else (lambda x: x not in key[2]))
    elif isinstance(key, tuple) and key[0] == "low":   # the low bits of an immediate's field
        from sass.decode import ir
        from sass.sleigh.gen import eq, pieces
        (tok, lo, hi, width), = pieces(b.g.source(k, key[1])[1])
        return eq(b.g, ir.Field("", ((lo + key[2] - 1, lo),)), v)
    elif isinstance(key, tuple) and key[0] == "zero":
        name = key[1]; atom = k.operand_types[name]
        zero = b.g.register_zero(k, name) if atom.type in REG_TYPES else 0
        # A register type without its zero (Predicate_vimnmx: P0-P6) is never RZ/PT.
        if v and atom.type in REG_TYPES and zero not in b.g.arch.enums[atom.type].values(): return None
        c = where(name, (lambda x: x == zero) if v else (lambda x: x != zero))
    else:
        c = where(key, lambda x: x == v)
    if c is False: return None
    if c is None: return []  # not encoded: every encoding has the (default) value
    return c if pattern(c) else None


def enumerate_rows(b, fam):
    """[(assignment, Outputs | Unmodelled)] over the static questions the family asks."""
    rows, todo = [], [{}]
    while todo:
        assignment = todo.pop()
        try: rows.append((assignment, fam(Symbolic(b, assignment))))
        except Branch as br:
            todo += [{**assignment, br.key: v} for v in reversed(br.domain)]
        except (Unmodelled, Primitive) as e: rows.append((assignment, e))
        except Illegal as e: rows.append((assignment, e))
    return rows


# ---- merging rows: anti-unification -----------------------------------------------------------

def sel(keys, cases, w):
    """A decode-time choice: cases is ((values per key, Expr), ...), all of width w."""
    if any(t.w != w for _, t in cases): raise TypeError(f"choice arms of widths {[t.w for _, t in cases]}")
    return Expr("sel", (tuple(keys), tuple(cases)), w)


def shape(t):
    """What must agree for trees to merge node by node: operator, width and plain arguments."""
    if not isinstance(t, Expr) or t.op in ("const", "param", "reg", "sel"): return ("leaf", id(t))
    return (t.op, t.w, tuple(None if isinstance(a, Expr) else a for a in t.args))


class Merger:
    """Merges the rows' trees of one class (every output, every liveness body), remembering the
    choices it built so that a choice agreeing with an earlier one is that one (LOP3's Pu reuses
    Rd's table)."""

    def __init__(self):
        self.choices = defaultdict(list)   # keys -> [(cases, default, choice)]
        self.merged = {}                   # (trees, rows) -> merge: subtrees recur (operands, wide values)
        self.deciding = {}                 # (partition of the rows, rows) -> determining keys

    def keys(self, values, assignments):
        """determining_keys, cached: many nodes split the rows the same way (every node that depends
        only on Ra's modifiers)."""
        index = {}
        partition = tuple(index.setdefault(v, len(index)) for v in values)
        memo = (partition, id(assignments))
        if memo not in self.deciding: self.deciding[memo] = determining_keys(partition, assignments)
        return self.deciding[memo]

    def choice(self, keys, cases, w):
        """sel over keys with cases {values: tree}; the most common arm becomes a default row (no
        constraints) that the specific rows override, when every specific row names all keys."""
        if len(set(cases.values())) == 1: return next(iter(cases.values()))
        for known, default, e in self.choices[keys]:
            if e.w == w and all(known.get(v, default) is t for v, t in cases.items()): return e
        counts = defaultdict(int)
        for t in cases.values(): counts[t] += 1
        default = max(counts, key=lambda t: (counts[t], -size(t))) if all(None not in v for v in cases) else None
        if default is not None:
            # The default also covers rows it reproduces under their key values (LOP3 imm8 = 0:
            # (Rd != 0) | Pp with Rd's table at 0 is Pp).
            covered = {v for v, t in cases.items() if t is default or specialize(default, dict(zip(keys, v))) is t}
            cases = {v: default if v in covered else t for v, t in cases.items()}
            counts[default] = len(covered)
            if len(covered) == len(cases): return default
        rows = sorted(((v, t) for v, t in cases.items() if t is not default), key=lambda kv: repr(kv[0]))
        if default is not None and counts[default] > 1: rows.append(((None,) * len(keys), default))
        else: rows, default = sorted(cases.items(), key=lambda kv: repr(kv[0])), None
        e = sel(keys, rows, w)
        self.choices[keys].append((cases, default, e))
        return e

    def bodies(self, trees, assignments):
        """The outputs of every row, merged: a tuple of per-output merges, or a decode-time choice
        of whole bodies over one small key, whichever costs less. Whole bodies keep a value shared
        by several outputs or lanes in one constructor (PRMT's constant operand loads once, not once
        per byte table). Greedy: the best single-key split one level deep, then the same inside
        each branch (an exhaustive search over key orders is exponential in FFMA's modifier bits)."""
        widths = [tuple(x.w for x in t.args) for t in trees]
        if len(set(widths)) > 1:
            # A choice has one width: rows whose outputs differ in width (a store of sz bits) are
            # bodies of their own, split on a key that decides the widths.
            keys = self.keys(widths, assignments)
            key = min(keys, key=lambda k: (len({a.get(k) for a in assignments}), repr(k)))
            branches = {}
            for v in sorted({a[key] for a in assignments if key in a}, key=repr):
                rows = [j for j, a in enumerate(assignments) if a.get(key, v) == v]
                branches[(v,)] = self.bodies([trees[j] for j in rows], [assignments[j] for j in rows])
            return self.choice((key,), branches, 0)
        def per_output(ts, asg):
            return Expr("tuple", tuple(self.merge([t.args[j] for t in ts], asg) for j in range(len(ts[0].args))), 0)
        def split(key):
            values = sorted({a[key] for a in assignments if key in a}, key=repr)
            if not 1 < len(values) <= MAX_BODY_SPLIT: return None
            parts = {}
            for v in values:
                rows = [j for j, a in enumerate(assignments) if a.get(key, v) == v]
                if len(rows) == len(trees): return None   # every row is in every branch: no split
                parts[(v,)] = rows
            return parts
        best, best_cost, best_key = per_output(trees, assignments), None, None
        best_cost = cost(best)
        for key in sorted({k for a in assignments for k in a}, key=repr):
            if (parts := split(key)) is None: continue
            candidate = self.choice((key,), {v: per_output([trees[j] for j in r], [assignments[j] for j in r]) for v, r in parts.items()}, 0)
            if (c := cost(candidate)) < best_cost: best_cost, best_key = c, key
        if best_key is None: return best
        return self.choice((best_key,), {v: self.bodies([trees[j] for j in r], [assignments[j] for j in r])
                                         for v, r in split(best_key).items()}, 0)

    def merge(self, trees, assignments):
        """One expression generalizing trees[i] (the value under assignments[i]). Equal shapes merge
        node by node; otherwise the rows split on one deciding key at a time (fewest values first),
        and the cheaper of that and one table of whole trees is kept."""
        first = trees[0]
        if all(t is first for t in trees): return first
        memo = (tuple(map(id, trees)), id(assignments))
        if memo not in self.merged: self.merged[memo] = (self.merge_rows(trees, assignments), assignments)   # keep the rows alive
        return self.merged[memo][0]

    def merge_rows(self, trees, assignments):
        first = trees[0]
        shapes = [shape(t) for t in trees]
        if shapes[0][0] != "leaf" and all(x == shapes[0] for x in shapes):
            # A choice has one width, so children of different widths are chosen between here.
            shapes = [x + tuple(a.w for a in t.args if isinstance(a, Expr)) for x, t in zip(shapes, trees)]
            if all(x == shapes[0] for x in shapes):
                args = [self.merge([t.args[j] for t in trees], assignments) if isinstance(first.args[j], Expr) else first.args[j]
                        for j in range(len(first.args))]
                return bitvec.ite(*args) if first.op == "ite" else Expr(first.op, tuple(args), first.w)
        keys = self.keys(shapes, assignments)
        if len(keys) > 1:
            key = min(keys, key=lambda k: (len({a.get(k) for a in assignments}), repr(k)))
            branches = {}
            for v in sorted({a[key] for a in assignments if key in a}, key=repr):
                rows = [j for j, a in enumerate(assignments) if a.get(key, v) == v]  # rows that didn't ask: every branch
                branches[(v,)] = self.merge([trees[j] for j in rows], [assignments[j] for j in rows])
            factored = self.choice((key,), branches, first.w)
        else:
            groups = defaultdict(list)
            for t, a, x in zip(trees, assignments, shapes): groups[x].append((t, a))
            merged = {x: self.merge([t for t, _ in g], [a for _, a in g]) for x, g in groups.items()}
            factored = self.choice(keys, {tuple(a.get(k) for k in keys): merged[x] for a, x in zip(assignments, shapes)}, first.w)
        # Factoring pays when the arms share structure (F2FP's formats); 256 distinct LOP3 functions
        # are cheaper as one table of whole trees than as nested tables over the same key.
        whole = self.keys([id(t) for t in trees], assignments)
        flat = self.choice(whole, {tuple(a.get(k) for k in whole): t for t, a in zip(trees, assignments)}, first.w)
        return min((factored, flat), key=cost)


def specialize(e, assignment, memo=None):
    """e under static key values: choices whose keys are all assigned resolve to their arm, and
    the result is rebuilt (so it folds)."""
    memo = {} if memo is None else memo
    if not isinstance(e, Expr) or e.op in ("const", "param", "reg"): return e
    if e in memo: return memo[e]
    if e.op == "sel":
        keys, rows = e.args
        out = e
        if all(k in assignment for k in keys):
            arm = next((t for vals, t in rows if all(v is None or assignment[k] == v for k, v in zip(keys, vals))), None)
            if arm is not None: out = specialize(arm, assignment, memo)
    else:
        args = [specialize(a, assignment, memo) for a in e.args]
        out = (bitvec.ite(*args) if e.op == "ite" else bitvec.extract(*args) if e.op == "extract"
               else Expr("tuple", tuple(args), 0) if e.op == "tuple"
               else e if all(x is y for x, y in zip(args, e.args)) else bitvec.op(e.op, *args, w=e.w))
    memo[e] = out
    return out


CONSTRUCTOR = 4   # a table row's cost in nodes (pattern, export, local)
MAX_BODY_SPLIT = 8   # keys with more values than this (LOP3's imm8) stay choices of values


RUNTIME = 8   # an operation executed per instruction outweighs a node of .sla


@cache
def cost(e):
    """Generated size (nodes outside choices once, every choice arm with its row) plus RUNTIME per
    operation on the longest execution path. A choice is its own subtable: its arms can't reuse
    values computed in the body or another table, so what several tables reference runs again."""
    seen = set()
    def static(n):
        if not isinstance(n, Expr) or n in seen: return 0
        seen.add(n)
        if n.op == "sel": return sum(CONSTRUCTOR + static(t) for _, t in n.args[1])
        return 1 + sum(static(a) for a in n.args)
    def path(n, done):
        if not isinstance(n, Expr) or n in done or n.op in ("const", "param", "reg"): return 0
        done.add(n)
        if n.op == "sel": return 1 + max(path(t, set()) for _, t in n.args[1])
        return (n.op != "tuple") + sum(path(a, done) for a in n.args)
    return static(e) + RUNTIME * path(e, set())


def size(e):
    seen, stack = set(), [e]
    while stack:
        n = stack.pop()
        if isinstance(n, Expr) and n not in seen: seen.add(n); stack.extend(n.args if n.op != "sel" else [t for _, t in n.args[1]])
    return len(seen)


def determining_keys(values, assignments):
    """The fewest static keys whose values determine `values`. A key a row never asked is any value,
    so two rows with different values must disagree on an asked key."""
    universe = sorted({k for a in assignments for k in a}, key=repr)
    rows = list(zip(values, assignments))
    overlap = lambda a, b, keys: all(k not in a or k not in b or a[k] == b[k] for k in keys)
    def determines(keys):
        # Rows that asked every key: one value per projection (linear). Rows that didn't are
        # wildcards on the rest and must not overlap a row of another value (pairwise, few).
        seen, partial = {}, []
        for v, a in rows:
            if all(k in a for k in keys):
                if seen.setdefault(tuple(a[k] for k in keys), v) != v: return False
            else: partial.append((v, a))
        return not any(v != w and overlap(a, c, keys) for v, a in partial for w, c in rows)
    # A key is necessary when two rows differing only in it have different values; every
    # determining set contains the necessary keys, so try those first, then extend them.
    full = [(v, tuple(a[x] for x in universe)) for v, a in rows if len(a) == len(universe)]
    def necessary(i):
        seen = {}
        return any(seen.setdefault(t[:i] + t[i + 1:], v) != v for v, t in full)
    needed = [k for i, k in enumerate(universe) if necessary(i)]
    rest = [k for k in universe if k not in needed]
    for n in range(len(rest) + 1):
        for extra in itertools.combinations(rest, n):
            keys = tuple(k for k in universe if k in needed or k in extra)
            if determines(keys): return keys
    raise AssertionError("assignments do not determine the values")


# ---- lowering to p-code text ------------------------------------------------------------------

FLOAT_CMP = {"flt": "f<", "fle": "f<=", "feq": "f=="}


def scale_constant(k, n):
    """2^k as a float constant of n bytes."""
    return {4: f"{(127 + k) << 23:#x}:4", 8: f"{(1023 + k) << 52:#x}:8", 16: f"{(16383 + k) << 112:#x}:16"}[n]


SLEIGH_OP = {"add": "+", "sub": "-", "mul": "*", "and": "&", "or": "|", "xor": "^", "shl": "<<", "shr": ">>",
             "sar": "s>>", "eq": "==", "ne": "!=", "ult": "<", "ule": "<=", "slt": "s<", "sle": "s<="}


@dataclass(frozen=True)
class Operand:
    """A lowered value: its p-code text, what kind of text it is, and the pattern references
    (('sub', table) or (token, field)) it needs. A w-bit value occupies its whole bytes with the
    bits above w zero."""
    text: str
    kind: str                       # local (this body's), name (a symbol, table or register), const, piece (name:n), expr or
                                    # unsized (float2float(...): sized only by where it is assigned)
    refs: frozenset = frozenset()

    @property
    def atom(self): return self.kind != "expr"      # usable as an operand without parentheses

    def __str__(self): return self.text if self.atom else f"({self.text})"


def expr(text, *parts): return Operand(text, "expr", frozenset().union(*(p.refs for p in parts)))


class Code:
    """Statements of one constructor, with the references they use."""

    def __init__(self): self.lines, self.refs = [], set()

    def add(self, line, *parts):
        self.lines.append(line)
        for p in parts: self.refs |= p.refs

    def body(self, export=None, w=None):
        """The constructor body, optionally exporting a value of w bits."""
        lines = list(self.lines)
        if export is not None:
            lines.append(f"export {export.text};" if export.kind in ("name", "local") else f"local t:{nbytes(w)} = {export.text}; export t;")
            self.refs |= export.refs
        return " ".join(lines)

    def pattern(self, clauses=()):
        from sass.sleigh.gen import pattern
        return pattern(list(clauses), sorted(self.refs, key=repr))


class Lower:
    """Lower expressions into one constructor's Code, materializing shared nodes once."""

    def __init__(self, b, code, uses, hoisted=frozenset()):
        # Names are numbered per constructor, so identical subtables are shared.
        self.b, self.code, self.uses, self.memo, self.serial = b, code, uses, [{}], 0
        self.hoisted = hoisted   # subtrees that live in their own value subtable

    def folded(self, e):
        """e computed while decoding, when its leaves are constants and plain immediate fields: a
        subtable whose constructor action evaluates it and exports the constant (one COPY where it
        is used). Actions have no comparisons: they are carries of masked sums (see Fold)."""
        if not isinstance(e, Expr) or e.op in ("const", "param") or e.w > 64: return None
        fold = Fold(self.b)
        try: text = fold.text(e)
        except Fold.No: return None
        if not fold.refs: return None   # constant already: bitvec simplifies it
        from sass.sleigh.gen import pattern
        table = self.b.g.subtable("fold", [("t", pattern([], sorted(fold.refs)), f" [ t = inst_start * 0 + ({text}); ]",
                                            f"export *[const]:{nbytes(e.w)} t;")])
        self.b.refs.append(("sub", table))
        return Operand(table, "name", frozenset([("sub", table)]))

    def label(self, stem):
        self.serial += 1
        return f"{stem}{self.serial}"

    def local(self, w, value):
        """A new local of w bits (its whole bytes) holding value."""
        n = nbytes(w)
        name = self.label("se")
        if n > 256:
            self.b.serial += 1
            name = f"SEMTMP{self.b.serial}_{n}"; self.b.g.temporaries[name] = n
            self.code.add(f"{name} = {value.text};", value)
        else: self.code.add(f"local {name}:{n} = {value.text};", value)
        return Operand(name, "local")

    def named(self, v, w):
        """v as a name (a local if it isn't one)."""
        return v if v.kind in ("name", "local") else self.local(w, v)

    @staticmethod
    def key(e):
        """e's memo key: a zero-extension by its byte width, since zext(x, 31) and zext(x, 32) lower
        alike (narrowing a hoisted zext must find it)."""
        return Expr("zext", e.args, 8 * nbytes(e.w)) if e.op == "zext" else e

    def remember(self, e, v):
        self.memo[-1][self.key(e)] = v
        if e.op == "zext":   # also findable by a narrower zero-extension of the same value
            wide = ("zext", e.args[0])
            if nbytes(e.w) > nbytes(self.memo[-1].get(wide, (0, None))[0]): self.memo[-1][wide] = (e.w, v)

    def lookup(self, e):
        k = self.key(e)
        if (hit := next((m[k] for m in reversed(self.memo) if k in m), None)) is not None or e.op != "zext": return hit
        # A wider zero-extension of the same value computed already (hoisted before a branch whose
        # arms were then narrowed): its low bytes.
        w, v = next((m[("zext", e.args[0])] for m in reversed(self.memo) if ("zext", e.args[0]) in m), (0, None))
        return self.truncated(v, w, nbytes(e.w)) if v is not None and nbytes(w) > nbytes(e.w) else None

    def value(self, e):
        """The lowered value of e."""
        if (hit := self.lookup(e)) is not None: return hit
        if (folded := self.folded(e)) is not None: return folded
        v = value_table(self.b, e) if e in self.hoisted else self.lower(e)
        if self.uses.get(e, 0) > 1 and not v.atom: v = self.local(e.w, v)
        self.remember(e, v)
        return v

    def truncated(self, v, src_w, n):
        """v (a src_w-bit value) truncated to n bytes; SLEIGH's `:n` applies only to names."""
        if nbytes(src_w) == n: return v
        v = self.named(v, src_w)
        return Operand(f"{v.text}:{n}", "piece", v.refs)

    def clean(self, w, v):
        """v, a value in nbytes(w) bytes whose bits above w may be set, with those bits cleared."""
        return v if w % 8 == 0 else expr(f"{v} & {bitvec.mask(w):#x}:{nbytes(w)}", v)

    def widened(self, e, w):
        """e's value zero-extended to nbytes(w) bytes (no operation if the byte size is the same)."""
        v = self.value(e)
        if nbytes(e.w) == nbytes(w): return v
        if v.kind == "unsized": v = self.local(e.w, v)   # zext() can't size a float2float
        return self.local(w, expr(f"zext({v.text})", v))

    def signed(self, e):
        """e's value sign-extended from bit e.w - 1 to its whole bytes (for signed operations)."""
        v = self.value(e)
        if e.w % 8 == 0: return v
        k = 8 * nbytes(e.w) - e.w
        return self.local(e.w, expr(f"({v} << {k}) s>> {k}", v))

    def binary(self, oper, x, y): return expr(f"{x} {oper} {y}", x, y)

    def lower(self, e):
        a, n = e.args, nbytes(e.w)
        match e.op:
            case "const":
                if n > 8: return self.local(e.w, Operand(f"zext({a[0]:#x}:8)", "expr"))
                return Operand(f"{a[0]:#x}:{n}", "const")
            case "param": return self.param(a[0], e.w)
            case "reg" if a[2] == 1: return Operand(f"{a[0]}{a[1]}", "name")   # a fixed register (UP0)
            case "zext": return self.widened(a[0], e.w)
            case "sext" if a[0].w == 1:
                v = self.widened(a[0], e.w)                    # 0 or 1, upper bits clear
                return self.clean(e.w, expr(f"-{v}", v))  # 0 or all ones
            case "sext":
                v = self.signed(a[0])
                return self.clean(e.w, v if nbytes(a[0].w) == n else self.local(e.w, expr(f"sext({v.text})", v)))
            case "extract":
                x, hi, lo = a
                if lo == 0 and (self.uses.get(x, 0) > 1 or self.lookup(x) is not None or
                                (m := bitvec.narrowed(x, e.w, frozenset(n for n, c in self.uses.items() if c > 1))) is e):
                    # The low bits of a value computed anyway; otherwise compute narrow.
                    return self.clean(e.w, self.truncated(self.value(x), x.w, n))
                if lo == 0: return self.value(m)
                # Only the bytes up to bit hi: a register operand is read no wider than the field.
                v, w = self.value(x), 8 * min(nbytes(hi + 1), nbytes(x.w))
                if w < 8 * nbytes(x.w): v = self.truncated(v, x.w, w // 8)
                return self.clean(e.w, self.truncated(expr(f"{v} >> {lo}", v), w, n))
            case "not" if e.w == 1: v = self.value(a[0]); return expr(f"!{v}", v)
            case "not": v = self.value(a[0]); return self.clean(e.w, expr(f"~{v}", v))
            case "xor" if bitvec.is_const(a[1]) and a[0].op == "zext" and a[0].args[0].w % 8 == 0 \
                    and bitvec.value(a[1]) == bitvec.mask(a[0].args[0].w):
                # Inverting the low bytes of a zero-extended value: zext(~x), as compact as written.
                v = self.value(a[0].args[0])
                return self.local(e.w, expr(f"zext(~{v})", v))
            case "neg": v = self.value(a[0]); return self.clean(e.w, expr(f"-{v}", v))
            case "concat":
                hi, lo = self.widened(a[0], e.w), self.widened(a[1], e.w)
                return self.local(e.w, expr(f"({hi} << {a[1].w}) | {lo}", hi, lo))
            case "ite": return self.ite(*a)
            case "fadd" | "fmul" | "ffma": return self.float_op(e)
            case "fconv": v = self.value(a[0]); return self.local(e.w, expr(f"float2float({v})", v))   # sized by its local
            case "flt" | "fle" | "feq": return self.binary(FLOAT_CMP[e.op], self.value(a[0]), self.value(a[1]))
            case "fnan": v = self.value(a[0]); return expr(f"nan({v})", v)
            case "sel": return self.sel(e)
            case "load": v = self.value(a[1]); return self.local(e.w, expr(f"*[{a[0]}]:{n} {v}", v))
            case "sar": return self.clean(e.w, self.binary("s>>", self.signed(a[0]), self.amount(a[1])))
            case "slt" | "sle": return self.binary(SLEIGH_OP[e.op], self.signed(a[0]), self.signed(a[1]))
            case "shl": return self.clean(e.w, self.binary("<<", self.value(a[0]), self.amount(a[1])))
            case "shr": return self.binary(">>", self.value(a[0]), self.amount(a[1]))
            case "add" | "sub" | "mul": return self.clean(e.w, self.binary(SLEIGH_OP[e.op], self.value(a[0]), self.value(a[1])))
            case op if op in SLEIGH_OP: return self.binary(SLEIGH_OP[op], self.value(a[0]), self.value(a[1]))
        raise NotImplementedError(f"lowering of {e.op}")

    # ---- floating point: exactly rounded (the exact language) or the plain formula (readable) ----
    # Ghidra's float ops round to nearest at the operands' size. Exact results come from a wider
    # format: products of these formats are exact in twice the width, so one rounding of a wide
    # sum or a narrowing of an exact product is a correct single rounding (FFMA's wide sum may
    # double-round in rare midpoint cases). Directed modes narrow with round-to-nearest, then
    # step the encoding by one toward the requested direction when the exact residual says so.

    def float_op(self, e):
        a, n = e.args, nbytes(e.w)
        xs = [self.value(x) for x in a if isinstance(x, Expr)]
        rnd = next(x for x in a if isinstance(x, str))
        scale = a[3] if e.op == "fmul" else 0
        if self.b.g.fp == "readable": v = self.float_formula(e.op, xs, n, scale)   # native width, binary16 too
        elif n == 2: return self.half_op(e.op, xs, scale)
        elif rnd == "RN": v = self.nearest(e.op, xs, n, scale)
        else: return self.directed(e.op, xs, n, rnd, scale)
        return v   # materialized by value() when used more than once

    def float_formula(self, op, xs, n, scale):
        match op:
            case "fadd": x = f"{xs[0]} f+ {xs[1]}"
            case "fmul": x = f"{xs[0]} f* {xs[1]}"
            case "ffma": x = f"{xs[0]} f* {xs[1]} f+ {xs[2]}"
        if scale: x = f"({x}) f* {scale_constant(scale, n)}"
        return expr(x, *xs)

    def widen_float(self, x, n): return self.local(8 * n, expr(f"float2float({x})", x))

    def nearest(self, op, xs, n, scale):
        if op == "fadd" or op == "fmul" and not scale: return self.float_formula(op, xs, n, 0)
        wide = [self.widen_float(x, 2 * n) for x in xs]
        x = (f"({wide[0]} f* {wide[1]}) f* {scale_constant(scale, 2 * n)}" if op == "fmul"
             else f"{wide[0]} f* {wide[1]} f+ {wide[2]}")
        return Operand(f"float2float({x})", "unsized", frozenset().union(*(p.refs for p in wide)))

    def half_op(self, op, xs, scale):
        """binary16: a native product (as the FP32 one), else computed in binary64 (sums exact) or
        binary128 (FMA) and narrowed once."""
        if op == "fmul" and not scale: return self.local(16, self.float_formula(op, xs, 2, 0))
        n = 16 if op == "ffma" else 8
        wide = [self.widen_float(x, n) for x in xs]
        x = self.local(8 * n, self.float_formula(op, wide, n, scale))
        return self.local(16, expr(f"float2float({x})", x))

    def directed(self, op, xs, n, rnd, scale):
        """RZ/RP/RM: s = RN_wide(exact), residual e (TwoSum; zero for exact wide products),
        r = RN(s), then r +- 1 ulp by the sign of the remaining error."""
        w = 2 * n
        zero = self.local(8 * w, Operand("zext(0:8)", "expr")) if w > 8 else Operand(f"0:{w}", "const")
        wide = [self.widen_float(x, w) for x in xs]
        if op == "fmul":
            s = self.local(8 * w, expr(f"{wide[0]} f* {wide[1]}", *wide))
            if scale: s = self.local(8 * w, expr(f"{s} f* {scale_constant(scale, w)}", s))
            residual = None
        else:
            p, c = (self.local(8 * w, expr(f"{wide[0]} f* {wide[1]}", *wide)), wide[2]) if op == "ffma" else (wide[0], wide[1])
            s = self.local(8 * w, expr(f"{p} f+ {c}", p, c))
            v = self.local(8 * w, expr(f"{s} f- {p}", s, p))
            residual = self.local(8 * w, expr(f"({p} f- ({s} f- {v})) f+ ({c} f- {v})", p, s, v, c))
        r = self.local(8 * n, expr(f"float2float({s})", s))
        d = self.local(8 * w, expr(f"({s} f- float2float({r}))" + (f" f+ {residual}" if residual else ""), s, r, *([residual] if residual else [])))
        negative = self.local(8, expr(f"{r} s< 0", r))
        # Infinite inputs leave a NaN residual: the result is exact. Ghidra 12.1.4 orders NaN in
        # binary128 comparisons, so test it explicitly.
        ordered = self.local(8, expr(f"!nan({d})", d))
        # RP needs only the error's upward test, RM only the downward one.
        above = self.local(8, expr(f"{ordered} && ({zero} f< {d})", ordered, zero, d)) if rnd != "RM" else None
        below = self.local(8, expr(f"{ordered} && ({d} f< {zero})", ordered, zero, d)) if rnd != "RP" else None
        step = {"RZ": f"{r} - zext(({below} && !{negative}) || ({above} && {negative}))",
                "RP": f"{r} + zext({above} && !{negative}) - zext({above} && {negative})",
                "RM": f"{r} + zext({below} && {negative}) - zext({below} && !{negative})"}[rnd]
        out = self.local(8 * n, expr(step, r, *(t for t in (above, below) if t is not None), negative))
        if rnd == "RM" and op != "fmul":
            # An exact zero sum is -0 under RM unless both addends are +0.
            sign = f"({xs[0]} ^ {xs[1]}) | {xs[2]}" if op == "ffma" else f"{xs[0]} | {xs[1]}"
            out = self.local(8 * n, expr(f"{out} | (zext(({s} f== {zero}) && (({sign}) s< 0)) << {8 * n - 1})", out, s, zero, *xs))
        return out

    def amount(self, n):
        """A shift amount: p-code shifts take any size of amount, so a zero-extension is dropped
        (unless it was computed already, e.g. hoisted out of a choice: then it is read)."""
        if n.op == "zext" and n.args[0].w % 8 == 0 and self.lookup(n) is None: return self.value(n.args[0])
        return self.value(n)

    def param(self, name, w):
        if name == "__inst_start":
            v = Operand("inst_start", "name")
            return v if w == 64 else self.clean(w, Operand(f"inst_start:{nbytes(w)}", "piece"))
        if name.startswith("__enc:"): return self.param(name.split(":", 1)[1], w)   # the field itself
        if name.startswith("__sr:"):      # the special register the field selects
            real = name.split(":", 1)[1]
            table = self.b.g.specialsem(self.b.g.source(self.b.k, real)[1], nbytes(w))
            self.b.refs.append(("sub", table))
            return Operand(table, "name", frozenset([("sub", table)]))
        if name.startswith("__target:"):   # a direct branch target: the subtable exporting it
            table = self.b.g.branch_target(self.b.k, self.b.k.operand_types[name.split(":", 1)[1]])
            self.b.refs.append(("sub", table))
            return Operand(table, "target", frozenset([("sub", table)]))
        if (atom := self.b.k.operand_types.get(name)) and atom.type == "PR":
            text = " | ".join(f"(P{k} << {k})" if k else "P0" for k in reversed(range(PR_BITS)))
            return self.local(w, Operand(text, "expr"))
        v = self.b.values[name]
        r = self.b.ref(v.symbol)
        sym = Operand(v.symbol, "const" if v.kind == "default" else "name", frozenset([r] if r else []))
        atom = self.b.k.operand_types.get(name)   # None for attribute selectors (Ra@negate)
        if atom and REG_TYPES.get(atom.type) in ("P", "UP") or v.size * 8 == w: return sym  # predicates are 0/1
        if v.size > nbytes(w): return self.clean(w, self.truncated(sym, 8 * v.size, nbytes(w)))
        if v.size == nbytes(w): return self.clean(w, sym)
        return self.local(w, expr(f"zext({sym.text})", sym))

    def cheap(self, e):
        """At hand without computing: constants, operands, values computed, and their low parts."""
        return (e.op in ("const", "param", "sel") or self.lookup(e) is not None   # a choice is a table reference
                or e.op == "extract" and e.args[2] == 0 and e.args[1] + 1 == 8 * nbytes(e.w) and self.cheap(e.args[0]))

    def precompute(self, nodes):
        """Compute nodes (smallest first) before a branch, so its arms share them."""
        for n in sorted(nodes, key=size):
            if self.lookup(n) is None: self.remember(n, self.named(self.value(n), n.w))

    def arm(self, target, e):
        """target = e, with e's temporaries local to this arm; a nested choice assigns target
        itself (no local and copy per level)."""
        self.memo.append({})
        if e.op == "ite" and self.uses.get(e, 0) <= 1 and self.lookup(e) is None: self.ite(*e.args, into=target)
        else: v = self.value(e); self.code.add(f"{target.text} = {v.text};", v)
        self.memo.pop()

    def ite(self, c, x, y, into=None):
        """A dynamic choice. Non-trivial subexpressions of both arms are computed before the branch.
        With a cheap arm (constant or value at hand) it is the default and the other arm overwrites
        it conditionally (one CBRANCH); otherwise a two-armed diamond."""
        self.precompute(shared_subtrees([x, y], leaves=self.hoisted))
        r = into or Operand(self.label("se"), "local")
        declare = "" if into else "local "
        if self.cheap(x) or self.cheap(y):
            first, other = (x, y) if self.cheap(x) else (y, x)
            # Not c: its complement comparison, or the negation of c when c is computed already.
            known = self.lookup(c)
            cond = (self.value(c) if first is x else expr(f"!{known}", known) if known is not None
                    else self.value(bitvec.complement(c)))
            init = self.value(first)
            self.code.add(f"{declare}{r.text}{'' if into else f':{nbytes(x.w)}'} = {init.text};", init)
            done = self.label("done")
            self.code.add(f"if ({cond.text}) goto <{done}>;", cond)
            self.arm(r, other)
            self.code.add(f"<{done}>")
            return r
        if not into: self.code.add(f"local {r.text}:{nbytes(x.w)} = 0;")
        then, done = self.label("then"), self.label("done")
        cond = self.value(c)
        self.code.add(f"if ({cond.text}) goto <{then}>;", cond)
        self.arm(r, y)
        self.code.add(f"goto <{done}>;"); self.code.add(f"<{then}>")
        self.arm(r, x)
        self.code.add(f"<{done}>")
        return r

    def sel(self, e):
        """A decode-time choice: a subtable with one constructor per case, exporting its value."""
        keys, cases = e.args
        constructors = []
        shared = shared_subtrees([t for _, t in cases], self.b)
        for values, tree in cases:
            cl = [c for k, v in zip(keys, values) if v is not None for c in clauses(self.b, k, v)]
            code = Code()
            v = Lower(self.b, code, count_uses([tree]), shared).value(tree)
            text = code.body(v, e.w)
            constructors.append(('""', code.pattern(cl), "", text))
        table = self.b.g.subtable("sel", constructors)
        self.b.refs.append(("sub", table))
        return Operand(table, "name", frozenset([("sub", table)]))


class Fold:
    """SLEIGH pattern-expression text for an Expr over constants and immediate fields, every value
    masked to its width (up to 64 bits: action arithmetic wraps at 64, and comparisons use borrow
    formulas that need no spare bit). An action
    expression has no locals, so a shared subterm is written out at each use: text past MAX_TEXT
    (deep ite chains double per level) is not folded, its run-time p-code is kept."""
    class No(Exception): pass
    MAX_TEXT = 4096

    def __init__(self, b): self.b, self.refs, self.memo = b, set(), {}

    def field(self, name, w):
        found = self.b.g.immediate_text(self.b.k, name)
        if found is None: raise Fold.No
        text, refs = found; self.refs.update(refs)
        return self.mask(text, w)

    @staticmethod
    def mask(text, w): return f"({text})" if w >= 64 else f"(({text}) & {(1 << w) - 1:#x})"   # actions wrap at 64 bits

    @staticmethod
    def nonzero(d): return f"(((({d}) | (0 - ({d}))) >> 63) & 1)"   # the sign of d | -d: 1 iff d != 0

    @staticmethod
    def below(x, y):
        """1 iff x < y unsigned: the borrow out of x - y at 64 bits (Hacker's Delight 2-13), so any
        width up to 64 works without a spare bit."""
        return f"((((~{x} & {y}) | (~({x} ^ {y}) & ({x} - {y}))) >> 63) & 1)"

    def text(self, e):
        if e not in self.memo:
            t = self.compose(e)
            if len(t) > Fold.MAX_TEXT: raise Fold.No
            self.memo[e] = t
        return self.memo[e]

    def compose(self, e):
        if e.w > 64: raise Fold.No
        a, w = e.args, e.w
        sub = lambda x: self.text(x)
        match e.op:
            case "const": return f"{e.args[0]:#x}"
            case "param": return self.field(a[0], w)
            case "and" | "or" | "xor":
                return f"({sub(a[0])} {dict(zip(('and', 'or', 'xor'), '&|^'))[e.op]} {sub(a[1])})"
            case "not": return self.mask(f"~{sub(a[0])}", w)
            case "neg": return self.mask(f"0 - {sub(a[0])}", w)   # two's complement, then masked
            case "add" | "sub" | "mul": return self.mask(f"{sub(a[0])} {dict(zip(('add', 'sub', 'mul'), '+-*'))[e.op]} {sub(a[1])}", w)
            case "shl" | "shr" if bitvec.is_const(a[1]):
                k = a[1].args[0]
                # Action arithmetic is signed: a right shift is masked so no sign bits come in.
                return "0x0" if k >= w else self.mask(f"{sub(a[0])} << {k}", w) if e.op == "shl" else self.mask(f"{sub(a[0])} >> {k}", w - k)
            case "extract": return self.mask(f"{sub(a[0])} >> {a[2]}", w)
            case "zext": return sub(a[0])
            case "concat": return f"(({sub(a[0])} << {a[1].w}) | {sub(a[1])})"
            case "eq" | "ne":
                d = self.nonzero(f"{sub(a[0])} ^ {sub(a[1])}")
                return d if e.op == "ne" else f"(1 - {d})"
            case "ult": return self.below(sub(a[0]), sub(a[1]))
            case "ule": return f"(1 - {self.below(sub(a[1]), sub(a[0]))})"
            case "ite": return f"({sub(a[2])} ^ (({sub(a[1])} ^ {sub(a[2])}) * {sub(a[0])}))"
        raise Fold.No


def value_table(b, e):
    """A subtable exporting e (shared by every constructor that uses it), as an operand."""
    tables = b.__dict__.setdefault("value_tables", {})
    if e not in tables:
        code = Code()
        v = Lower(b, code, count_uses([e])).value(e)
        text = code.body(v, e.w)
        tables[e] = b.g.subtable("val", [('""', code.pattern(), "", text)])
        b.refs.append(("sub", tables[e]))
    return Operand(tables[e], "name", frozenset([("sub", tables[e])]))


LOADS = ("constant-load", "cx-load")   # operands whose subtable does a memory access when built


def shared_subtrees(trees, b=None, leaves=frozenset()):
    """Maximal non-trivial subtrees that occur in more than one of `trees`; with the Builder b, not
    those holding a loaded operand: a value table holding one would load again beside the arm's
    own reference to it. A subtree that the body itself never reads (it occurs only inside `leaves`,
    nodes computed in their own value tables) is not hoisted: it would be a dead temporary."""
    def nodes(t, leaves=leaves):   # in a fixed order: Expr hashes are identities, so sets would vary per run
        out, stack = {}, [t]
        while stack:
            n = stack.pop()
            if isinstance(n, Expr) and n not in out:
                out[n] = None
                if n not in leaves: stack.extend(reversed([a for a in n.args if isinstance(a, Expr)]))
        return out
    counts, direct = defaultdict(int), set()
    for t in trees:
        for n in nodes(t, frozenset()): counts[n] += 1
        direct |= nodes(t).keys()
    # Even small shared parts: measured on SM89, hoisting everything shared gives the smallest .sla.
    loads = lambda n: b is not None and any(m.op == "param" and getattr(b.values.get(m.args[0]), "kind", None) in LOADS for m in nodes(n, frozenset()))
    shared = [n for n, c in counts.items() if c > 1 and n in direct and n.op not in ("const", "param", "reg") and not loads(n)]
    def below(n, seen):
        for a in n.args:
            if isinstance(a, Expr) and a not in seen: seen.add(a); below(a, seen)
        return seen
    covered = set()
    for n in shared: covered |= below(n, set())
    return dict.fromkeys(n for n in shared if n not in covered)   # ordered, with set membership


def count_uses(roots):
    uses = defaultdict(int)
    def visit(e):
        if not isinstance(e, Expr): return
        uses[e] += 1
        if uses[e] > 1 or e.op == "sel": return
        for a in e.args: visit(a)
    for r in roots: visit(r)
    return uses


FIXED = {f"P{k}" for k in range(PR_BITS)}   # register symbols written directly (PR)
FLOW = ("__flow_target", "__flow_cond")      # a control transfer, carried through merging as values


def pseudo(n): return n.startswith("__")       # flow, store and effect values merged like outputs


def predicate_writes(i, name, e):
    """A PR write is one write per predicate; other writes are themselves."""
    if i.atom(name).type != "PR": return [(name, e)]
    return [(f"P{k}", bitvec.extract(e, k, k)) for k in range(PR_BITS)]


def lower_body(b, names, e):
    """Code for a merged body: a tuple writes `names`; a choice is a table of whole bodies."""
    if e.op == "tuple": return lower_writes(b, list(zip(names, e.args)))
    keys, rows = e.args
    constructors = []
    for values, arm in rows:
        code = lower_body(b, names, arm)
        cl = [c for k, v in zip(keys, values) if v is not None for c in clauses(b, k, v)]
        constructors.append(('""', code.pattern(cl), "", code.body()))
    table = b.g.subtable("body", constructors)
    b.refs.append(("sub", table))
    out = Code(); out.lines.append(f"build {table};"); out.refs |= {("sub", table)}
    return out


def lower_writes(b, writes):
    """Code writing [(output name, Expr)]: shared values computed before any branch, later values
    captured before the first write (which may alias their inputs), the first write inline; then
    the control transfer (b.flow_kind) to the FLOW target if its condition holds."""
    code = Code()
    low = Lower(b, code, count_uses([e for _, e in writes]))
    extra = dict(w for w in writes if pseudo(w[0]))
    # Effects (memory-ordering calls) come before any load the values do.
    for k, (name, nargs) in enumerate(b.effect_shapes):
        args = [low.value(extra[f"__effect{k}_{j}"]) for j in range(nargs)]
        b.g.pcodeops.add(name); code.add(f"{name}({', '.join(a.text for a in args)});", *args)
    low.precompute(n for n, c in low.uses.items() if c > 1 and n.op not in ("const", "param", "reg", "sel"))
    flow = {n: extra[n] for n in FLOW if n in extra}
    writes = [w for w in writes if not pseudo(w[0])]
    # A value used after the first write that isn't a local, constant or direct target may be a
    # register (a table can export one, Pv's Pp): capture it before the first write can change it.
    keep = lambda v, e: low.local(e.w, v) if v.kind not in ("local", "const", "target") and writes else v
    later = [keep(low.value(e), e) for _, e in writes[1:]]
    target, cond = (keep(low.value(flow[n]), flow[n]) for n in FLOW) if flow else (None, None)
    stores = [(space, *(keep(low.value(e), e) for e in (extra[f"__store{k}_addr"], extra[f"__store{k}_value"])), extra[f"__store{k}_value"].w)
              for k, space in enumerate(b.store_spaces)]
    first = [low.value(writes[0][1])] if writes else []
    for (n, _), v in zip(writes, first + later):
        symbol = n if n in FIXED else b.outputs[n].symbol
        dest = Operand(symbol, "name", frozenset([r] if (r := b.ref(symbol)) else []))
        code.add(f"{dest.text} = {v.text};", dest, v)
    for space, address, v, w in stores: code.add(f"*[{space}]:{nbytes(w)} {address.text} = {v.text};", address, v)
    if flow: lower_flow(low, b.flow_kind, target, cond, bitvec.is_const(flow["__flow_cond"], 1))
    return code


def lower_flow(low, kind, target, cond, always):
    """goto / call / return to target (a direct target table, or an address value) if cond."""
    where = target.text if target.kind == "target" and kind != "return" else f"[{target.text}]"
    transfer = {"goto": f"goto {where};", "call": f"call {where};", "return": f"return {where};"}[kind]
    if always: low.code.add(transfer, target)
    elif kind == "goto": low.code.add(f"if ({cond.text}) {transfer}", target, cond)
    else:
        skip = low.label("skip")
        low.code.add(f"if (!{cond}) goto <{skip}>; {transfer} <{skip}>", target, cond)


# ---- the class -------------------------------------------------------------------------------

def statuses(rows, unmodelled):
    """[(assignment, status)]: what each row lowers to, with `unmodelled` for the fallback's and
    None for an illegal encoding's."""
    def status(r):
        match r:
            case Primitive(): return "primitive"
            case Illegal(): return None
            case Exception(): return unmodelled
            case _: return "native"
    return [(a, status(r)) for a, r in rows]


def emit_primitive(b, rows):
    """The runtime primitive for the encodings the family sends there (behind selector guards),
    the opaque call for the rest."""
    from sass.semantics.operations import dispatch
    if any(not isinstance(r, Exception) for _, r in rows): raise TypeError(f"{b.k.name}: primitive rows mixed with native ones")
    b.rows = statuses(rows, "opaque")
    lines = b.call("prim"); refs = list(dict.fromkeys(b.call_refs)); b.body += lines
    guard(b, rows, [isinstance(r, Primitive) for _, r in rows])
    dispatch(b, "opaque", refs=sorted(refs, key=repr))
    return "primitive", "; ".join(sorted({str(r) for _, r in rows if isinstance(r, Primitive)}))


def guard(b, rows, modelled):
    """Selector patterns admitting exactly the rows `modelled` marks (the rest fall back)."""
    if all(modelled): b.selector_combinations = {}; return
    deciding = determining_keys(modelled, [a for a, _ in rows])
    values = sorted({tuple(a.get(k) for k in deciding) for (a, _), m in zip(rows, modelled) if m}, key=repr)
    b.selector_patterns = [[c for k, v in zip(deciding, vals) if v is not None for c in clauses(b, k, v)] for vals in values]
    (key,) = deciding if len(deciding) == 1 else (None,)
    if isinstance(key, str) and None not in (vs := {v for v, in values}):
        b.selector_patterns = [b.g.values_where(b.k, key, lambda x: x in vs) or []]
    b.selector_combinations = {",".join(map(str, map(builder_key, deciding))): [list(v) for v in values]}


def emit(b):
    """Lower the class's family into b.body; returns (status, reason) like operations.emit."""
    from sass.sleigh.gen import pattern
    from sass.semantics.operations import dispatch
    fam = family_for(b.k)
    rows = enumerate_rows(b, fam)
    if any(isinstance(r, Primitive) for _, r in rows): return emit_primitive(b, rows)
    b.rows = statuses(rows, "primitive" if fam.fallback == "prim" else "opaque")
    native = [(a, r) for a, r in rows if not isinstance(r, Exception)]
    why = "; ".join(sorted({str(r) for _, r in rows if isinstance(r, Exception)}))
    if not native:
        if fam.fallback != "prim": return "opaque", why or "no modelled encodings"
        b.body += b.call("prim")
        return "primitive", why
    i = Symbolic(b, {})
    kinds = {r.flow.kind if r.flow else None for _, r in native}
    if len(kinds) > 1: raise TypeError(f"{b.k.name}: rows differ in control transfer {sorted(map(str, kinds))}")
    b.flow_kind = kinds.pop()
    for attr, shape in (("store_spaces", lambda r: tuple(s.space for s in r.stores)),
                        ("effect_shapes", lambda r: tuple((e.name, len(e.args)) for e in r.effects))):
        shapes = {shape(r) for _, r in native}
        if len(shapes) > 1: raise TypeError(f"{b.k.name}: rows differ in {attr} {sorted(shapes)}")
        setattr(b, attr, shapes.pop())
    flow = lambda r: ([("__flow_target", r.flow.target), ("__flow_cond", r.flow.cond if r.flow.cond is not None else bitvec.const(1, 1))] if r.flow else []) \
        + [w for k, s in enumerate(r.stores) for w in ((f"__store{k}_addr", s.address), (f"__store{k}_value", s.value))] \
        + [(f"__effect{k}_{j}", a) for k, e in enumerate(r.effects) for j, a in enumerate(e.args)]
    resolved = [(a, [w for n, e in r.ordered() if i.has_operand(n) for w in predicate_writes(i, i.resolve(n), e)] + flow(r))
                for a, r in native]
    for a, writes in resolved:
        for n, e in writes:
            if n in FIXED or pseudo(n): continue
            if n not in b.outputs: raise TypeError(f"{b.k.name}: {n} is not an output")
            if e.w != i.width(n): raise TypeError(f"{b.k.name}: {n} is {i.width(n)} bits, value {e!r} is {e.w}")
    # Liveness: when every optional predicate destination is PT, those writes and the work only
    # they need are dropped; otherwise everything is written (PT writes go to a sink).
    names = [n for n, _ in resolved[0][1]]
    optional = [n for n in names if n not in FIXED and not pseudo(n) and REG_TYPES.get(b.k.operand_types[n].type) in ("P", "UP")
                and clauses(b, ("zero", n), True) not in (None, [])]
    assignments = [a for a, _ in resolved]
    merger = Merger()
    def body(live):
        if not live: return Code()
        merged = merger.bodies([Expr("tuple", tuple(dict(w)[n] for n in live), 0) for _, w in resolved], assignments)
        return lower_body(b, live, merged)
    bodies = [([], body(names))]
    if optional:
        bodies.insert(0, ([c for n in optional for c in clauses(b, ("zero", n), True)], body([n for n in names if n not in optional])))
    if len(bodies) == 1:
        b.body += bodies[0][1].lines; refs = bodies[0][1].refs
    else:
        table = b.g.subtable("live", [('""', code.pattern(cond), "", code.body()) for cond, code in bodies])
        b.refs.append(("sub", table)); b.body.append(f"build {table};"); refs = {("sub", table)}
    # Unmodelled combinations keep the complete opaque fallback, behind the native encodings'
    # patterns over only the keys that decide native vs unmodelled.
    guard(b, rows, [not isinstance(r, Exception) for _, r in rows])
    dispatch(b, fam.fallback, refs=sorted(refs, key=repr))
    return "native", ("decode-time selector guards retain complete opaque fallbacks" if len(native) < len(rows)
                      else "semantics family")
