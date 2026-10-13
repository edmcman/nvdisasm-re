"""SLEIGH backend for semantics families: the only Ghidra-aware part of the semantics.

Ghidra decodes at run time, so a class's semantics must cover every word of the class at once:
1. Enumerate: run the family with operand placeholders, branching depth-first over each static
   question it asks (modifier bits, selectors, zero tests); unsatisfiable answers are skipped.
2. Split on dead outputs: a predicate destination encoded as PT is discarded, so each liveness
   combination gets its own body (all-PT carries leave a 32-bit add).
3. Merge the rows by anti-unification: shared structure is kept; where rows differ, the subterm
   becomes a decode-time choice (`sel`) over the fewest static keys that determine it.
4. Lower to p-code text through the Builder (`sass/semantic.py`): shared nodes become locals,
   dynamic choices labelled diamonds, every value is computed before any write. Unmodelled rows
   keep the complete opaque fallback via `operations.dispatch`.
"""
import itertools
from collections import defaultdict
from dataclasses import dataclass
from sass import sem
from sass.sem import Expr
from sass.sem_family import Instruction, Illegal, Unmodelled, PR_BITS, REG_TYPES, family_for
import sass.families  # registers the families

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

    def operand(self, name, width): return sem.param(name, width)

    def span(self, name):
        v = self.b.outputs.get(name) or self.b.values.get(name)
        return v.size // nbytes(ELEMENT[REG_TYPES[self.atom(name).type]])

    def addr(self): return sem.param("__inst_start", 64)

    def parameter(self, name, values, w):
        # One yes/no question (in the table or not), then a decode-time constant table.
        if not self.answer(("in", name, frozenset(values)), (True, False)): raise Unmodelled(f"{name} outside the table")
        cases = [((v,), sem.const(x, w)) for v, x in sorted(values.items()) if clauses(self.b, name, v) is not None]
        if len({value for _, value in cases}) == 1: return cases[0][1]
        return sel((name,), cases, w)


def clauses(b, key, v):
    """Encoding clauses for static key == v; None if no encoding has that value."""
    from sass.gen_sleigh import pattern
    k = b.k
    if isinstance(key, tuple) and key[0] == "in":
        c = b.g.values_where(k, key[1], (lambda x: x in key[2]) if v else (lambda x: x not in key[2]))
    elif isinstance(key, tuple) and key[0] == "zero":
        name = key[1]; atom = k.operand_types[name]
        zero = b.g.register_zero(k, name) if atom.type in REG_TYPES else 0
        # A register type without its zero (Predicate_vimnmx: P0-P6) is never RZ/PT.
        if v and atom.type in REG_TYPES and zero not in b.g.arch.enums[atom.type].values(): return None
        c = b.g.values_where(k, name, (lambda x: x == zero) if v else (lambda x: x != zero))
    else:
        c = b.g.values_where(k, key, lambda x: x == v)
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
        except Unmodelled as e: rows.append((assignment, e))
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

    def __init__(self): self.choices = defaultdict(list)   # keys -> [(cases, default, choice)]

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

    def bodies(self, trees, assignments, memo=None):
        """The outputs of every row, merged: a tuple of per-output merges, or a decode-time choice
        of whole bodies over one small key (each body merged the same way), whichever costs less.
        Whole bodies keep a value shared by several outputs or lanes in one constructor (PRMT's
        constant operand loads once, not once per byte table)."""
        memo = {} if memo is None else memo
        # Different rows can share trees (ISETP.F with !Pp and ISETP.T with Pp): key by both.
        rows = tuple(sorted((id(t), tuple(sorted(a.items(), key=repr))) for t, a in zip(trees, assignments)))
        if rows in memo: return memo[rows]
        best = Expr("tuple", tuple(self.merge([t.args[j] for t in trees], assignments) for j in range(len(trees[0].args))), 0)
        for key in sorted({k for a in assignments for k in a}, key=repr):
            values = sorted({a[key] for a in assignments if key in a}, key=repr)
            if not 1 < len(values) <= MAX_BODY_SPLIT: continue
            branches = {}
            for v in values:
                rows_v = [j for j, a in enumerate(assignments) if a.get(key, v) == v]
                if len(rows_v) == len(trees): break   # every row is in every branch: no split
                branches[(v,)] = self.bodies([trees[j] for j in rows_v], [assignments[j] for j in rows_v], memo)
            else:
                candidate = self.choice((key,), branches, 0)
                if cost(candidate) < cost(best): best = candidate
        memo[rows] = best
        return best

    def merge(self, trees, assignments):
        """One expression generalizing trees[i] (the value under assignments[i]). Equal shapes merge
        node by node; otherwise the rows split on one deciding key at a time (fewest values first),
        and the cheaper of that and one table of whole trees is kept."""
        first = trees[0]
        if all(t is first for t in trees): return first
        shapes = [shape(t) for t in trees]
        if shapes[0][0] != "leaf" and all(x == shapes[0] for x in shapes):
            # A choice has one width, so children of different widths are chosen between here.
            shapes = [x + tuple(a.w for a in t.args if isinstance(a, Expr)) for x, t in zip(shapes, trees)]
            if all(x == shapes[0] for x in shapes):
                args = [self.merge([t.args[j] for t in trees], assignments) if isinstance(first.args[j], Expr) else first.args[j]
                        for j in range(len(first.args))]
                return sem.ite(*args) if first.op == "ite" else Expr(first.op, tuple(args), first.w)
        keys = determining_keys(shapes, assignments)
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
        whole = determining_keys([id(t) for t in trees], assignments)
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
        out = (sem.ite(*args) if e.op == "ite" else sem.extract(*args) if e.op == "extract"
               else Expr("tuple", tuple(args), 0) if e.op == "tuple"
               else e if all(x is y for x, y in zip(args, e.args)) else sem.op(e.op, *args, w=e.w))
    memo[e] = out
    return out


CONSTRUCTOR = 4   # a table row's cost in nodes (pattern, export, local)
MAX_BODY_SPLIT = 8   # keys with more values than this (LOP3's imm8) stay choices of values


RUNTIME = 8   # an operation executed per instruction outweighs a node of .sla


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
    for n in range(len(universe) + 1):
        for keys in itertools.combinations(universe, n):
            if not any(v != w and overlap(a, c, keys) for (v, a), (w, c) in itertools.combinations(rows, 2)): return keys
    raise AssertionError("assignments do not determine the values")


# ---- lowering to p-code text ------------------------------------------------------------------

SLEIGH_OP = {"add": "+", "sub": "-", "mul": "*", "and": "&", "or": "|", "xor": "^", "shl": "<<", "shr": ">>",
             "sar": "s>>", "eq": "==", "ne": "!=", "ult": "<", "ule": "<=", "slt": "s<", "sle": "s<="}


@dataclass(frozen=True)
class Operand:
    """A lowered value: its p-code text, what kind of text it is, and the pattern references
    (('sub', table) or (token, field)) it needs. A w-bit value occupies its whole bytes with the
    bits above w zero."""
    text: str
    kind: str                       # local (this body's), name (a symbol, table or register), const, piece (name:n) or expr
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
        from sass.gen_sleigh import pattern
        return pattern(list(clauses), sorted(self.refs, key=repr))


class Lower:
    """Lower expressions into one constructor's Code, materializing shared nodes once."""

    def __init__(self, b, code, uses, hoisted=frozenset()):
        # Names are numbered per constructor, so identical subtables are shared.
        self.b, self.code, self.uses, self.memo, self.serial = b, code, uses, [{}], 0
        self.hoisted = hoisted   # subtrees that live in their own value subtable

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

    def lookup(self, e):
        return next((m[e] for m in reversed(self.memo) if e in m), None)

    def value(self, e):
        """The lowered value of e."""
        if (hit := self.lookup(e)) is not None: return hit
        v = value_table(self.b, e) if e in self.hoisted else self.lower(e)
        if self.uses.get(e, 0) > 1 and not v.atom: v = self.local(e.w, v)
        self.memo[-1][e] = v
        return v

    def truncated(self, v, src_w, n):
        """v (a src_w-bit value) truncated to n bytes; SLEIGH's `:n` applies only to names."""
        if nbytes(src_w) == n: return v
        v = self.named(v, src_w)
        return Operand(f"{v.text}:{n}", "piece", v.refs)

    def clean(self, w, v):
        """v, a value in nbytes(w) bytes whose bits above w may be set, with those bits cleared."""
        return v if w % 8 == 0 else expr(f"{v} & {sem.mask(w):#x}:{nbytes(w)}", v)

    def widened(self, e, w):
        """e's value zero-extended to nbytes(w) bytes (no operation if the byte size is the same)."""
        v = self.value(e)
        return v if nbytes(e.w) == nbytes(w) else self.local(w, expr(f"zext({v.text})", v))

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
                                (m := sem.narrowed(x, e.w, frozenset(n for n, c in self.uses.items() if c > 1))) is e):
                    # The low bits of a value computed anyway; otherwise compute narrow.
                    return self.clean(e.w, self.truncated(self.value(x), x.w, n))
                if lo == 0: return self.value(m)
                v = self.value(x)
                return self.clean(e.w, self.truncated(expr(f"{v} >> {lo}", v), x.w, n))
            case "not" if e.w == 1: v = self.value(a[0]); return expr(f"!{v}", v)
            case "not": v = self.value(a[0]); return self.clean(e.w, expr(f"~{v}", v))
            case "xor" if sem.is_const(a[1]) and a[0].op == "zext" and a[0].args[0].w % 8 == 0 \
                    and sem.value(a[1]) == sem.mask(a[0].args[0].w):
                # Inverting the low bytes of a zero-extended value: zext(~x), as compact as written.
                v = self.value(a[0].args[0])
                return self.local(e.w, expr(f"zext(~{v})", v))
            case "neg": v = self.value(a[0]); return self.clean(e.w, expr(f"-{v}", v))
            case "concat":
                hi, lo = self.widened(a[0], e.w), self.widened(a[1], e.w)
                return self.local(e.w, expr(f"({hi} << {a[1].w}) | {lo}", hi, lo))
            case "ite": return self.ite(*a)
            case "sel": return self.sel(e)
            case "load": v = self.value(a[1]); return self.local(e.w, expr(f"*[{a[0]}]:{n} {v}", v))
            case "sar": return self.clean(e.w, self.binary("s>>", self.signed(a[0]), self.amount(a[1])))
            case "slt" | "sle": return self.binary(SLEIGH_OP[e.op], self.signed(a[0]), self.signed(a[1]))
            case "shl": return self.clean(e.w, self.binary("<<", self.value(a[0]), self.amount(a[1])))
            case "shr": return self.binary(">>", self.value(a[0]), self.amount(a[1]))
            case "add" | "sub" | "mul": return self.clean(e.w, self.binary(SLEIGH_OP[e.op], self.value(a[0]), self.value(a[1])))
            case op if op in SLEIGH_OP: return self.binary(SLEIGH_OP[op], self.value(a[0]), self.value(a[1]))
        raise NotImplementedError(f"lowering of {e.op}")

    def amount(self, n):
        """A shift amount: p-code shifts take any size of amount, so a zero-extension is dropped."""
        return self.value(n.args[0]) if n.op == "zext" and n.args[0].w % 8 == 0 else self.value(n)

    def param(self, name, w):
        if name == "__inst_start":
            v = Operand("inst_start", "name")
            return v if w == 64 else self.clean(w, Operand(f"inst_start:{nbytes(w)}", "piece"))
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
        return (e.op in ("const", "param") or self.lookup(e) is not None
                or e.op == "extract" and e.args[2] == 0 and e.args[1] + 1 == 8 * nbytes(e.w) and self.cheap(e.args[0]))

    def precompute(self, nodes):
        """Compute nodes (smallest first) before a branch, so its arms share them."""
        for n in sorted(nodes, key=size):
            if self.lookup(n) is None: self.memo[-1][n] = self.named(self.value(n), n.w)

    def arm(self, target, e):
        """target = e, with e's temporaries local to this arm."""
        self.memo.append({}); v = self.value(e); self.code.add(f"{target.text} = {v.text};", v); self.memo.pop()

    def ite(self, c, x, y):
        """A dynamic choice. Non-trivial subexpressions of both arms are computed before the branch.
        With a cheap arm (constant or value at hand) it is the default and the other arm overwrites
        it conditionally (one CBRANCH); otherwise a two-armed diamond."""
        self.precompute(shared_subtrees([x, y]))
        r = Operand(self.label("se"), "local")
        if self.cheap(x) or self.cheap(y):
            first, other = (x, y) if self.cheap(x) else (y, x)
            # Not c: its complement comparison, or the negation of c when c is computed already.
            known = self.lookup(c)
            cond = (self.value(c) if first is x else expr(f"!{known}", known) if known is not None
                    else self.value(sem.complement(c)))
            init = self.value(first)
            self.code.add(f"local {r.text}:{nbytes(x.w)} = {init.text};", init)
            done = self.label("done")
            self.code.add(f"if ({cond.text}) goto <{done}>;", cond)
            self.arm(r, other)
            self.code.add(f"<{done}>")
            return r
        self.code.add(f"local {r.text}:{nbytes(x.w)} = 0;")
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


def shared_subtrees(trees, b=None):
    """Maximal non-trivial subtrees that occur in more than one of `trees`; with the Builder b, not
    those holding a loaded operand: a value table holding one would load again beside the arm's
    own reference to it."""
    def nodes(t):   # in a fixed order: Expr hashes are identities, so sets would vary per run
        out, stack = {}, [t]
        while stack:
            n = stack.pop()
            if isinstance(n, Expr) and n not in out:
                out[n] = None; stack.extend(reversed([a for a in n.args if isinstance(a, Expr)]))
        return out
    counts = defaultdict(int)
    for t in trees:
        for n in nodes(t): counts[n] += 1
    # Even small shared parts: measured on SM89, hoisting everything shared gives the smallest .sla.
    loads = lambda n: b is not None and any(m.op == "param" and getattr(b.values.get(m.args[0]), "kind", None) in LOADS for m in nodes(n))
    shared = [n for n, c in counts.items() if c > 1 and n.op not in ("const", "param", "reg") and not loads(n)]
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


def predicate_writes(i, name, e):
    """A PR write is one write per predicate; other writes are themselves."""
    if i.atom(name).type != "PR": return [(name, e)]
    return [(f"P{k}", sem.extract(e, k, k)) for k in range(PR_BITS)]


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
    captured before the first write (which may alias their inputs), the first write inline."""
    code = Code()
    low = Lower(b, code, count_uses([e for _, e in writes]))
    low.precompute(n for n, c in low.uses.items() if c > 1 and n.op not in ("const", "param", "reg", "sel"))
    # A later value that isn't a local or constant may be a register (a table can export one, Pv's
    # Pp): capture it before the first write can change it.
    later = [low.local(e.w, v) if (v := low.value(e)).kind not in ("local", "const") else v for _, e in writes[1:]]
    first = low.value(writes[0][1])
    for (n, _), v in zip(writes, [first] + later):
        symbol = n if n in FIXED else b.outputs[n].symbol
        target = Operand(symbol, "name", frozenset([r] if (r := b.ref(symbol)) else []))
        code.add(f"{target.text} = {v.text};", target, v)
    return code


# ---- the class -------------------------------------------------------------------------------

def emit(b):
    """Lower the class's family into b.body; returns (status, reason) like operations.emit."""
    from sass.gen_sleigh import pattern
    from sass.operations import dispatch
    fam = family_for(b.k)
    rows = enumerate_rows(b, fam)
    native = [(a, r) for a, r in rows if not isinstance(r, Exception)]
    if not native:
        return "opaque", "; ".join(sorted({str(r) for _, r in rows})) or "no modelled encodings"
    i = Symbolic(b, {})
    resolved = [(a, [w for n, e in r.ordered() if i.has_operand(n) for w in predicate_writes(i, i.resolve(n), e)])
                for a, r in native]
    for a, writes in resolved:
        for n, e in writes:
            if n in FIXED: continue
            if n not in b.outputs: raise TypeError(f"{b.k.name}: {n} is not an output")
            if e.w != i.width(n): raise TypeError(f"{b.k.name}: {n} is {i.width(n)} bits, value {e!r} is {e.w}")
    # Liveness: when every optional predicate destination is PT, those writes and the work only
    # they need are dropped; otherwise everything is written (PT writes go to a sink).
    names = [n for n, _ in resolved[0][1]]
    optional = [n for n in names if n not in FIXED and REG_TYPES.get(b.k.operand_types[n].type) in ("P", "UP")
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
    # Unmodelled combinations keep the complete opaque fallback.
    keys = sorted({k for a, _ in rows for k in a}, key=repr)
    if len(native) < len(rows):
        # The native encodings, over only the keys that decide native vs unmodelled.
        modelled = [not isinstance(r, Exception) for _, r in rows]
        deciding = determining_keys(modelled, [a for a, _ in rows])
        native_values = sorted({tuple(a.get(k) for k in deciding) for a, r in rows if not isinstance(r, Exception)}, key=repr)
        b.selector_patterns = [[c for k, v in zip(deciding, vals) if v is not None for c in clauses(b, k, v)] for vals in native_values]
        (key,) = deciding if len(deciding) == 1 else (None,)
        if isinstance(key, str) and None not in (vs := {v for v, in native_values}):
            # One deciding selector: a single membership pattern instead of a row per value.
            b.selector_patterns = [b.g.values_where(b.k, key, lambda x: x in vs) or []]
    if len(native) < len(rows):
        b.selector_combinations = {",".join(map(str, map(builder_key, deciding))): [list(v) for v in native_values]}
    else: b.selector_combinations = {}
    dispatch(b, refs=sorted(refs, key=repr))
    return "native", ("decode-time selector guards retain complete opaque fallbacks" if len(native) < len(rows)
                      else "semantics family")
