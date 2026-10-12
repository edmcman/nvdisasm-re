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
import itertools, re
from collections import defaultdict
from sass import sem
from sass.sem import Expr
from sass.sem_family import FAMILIES, Instruction, Illegal, Unmodelled, REG_TYPES
import sass.families  # registers the families

ELEMENT = {"R": 32, "UR": 32, "P": 1, "UP": 1}   # bits per register element
def nbytes(w): return (w + 7) // 8


class Branch(Exception):
    def __init__(self, key, domain): self.key, self.domain = key, domain


def builder_key(key):
    """A static key as the Builder spells selectors: name, 'name@attr', or ('zero', name)."""
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


def clauses(b, key, v):
    """Encoding clauses for static key == v; None if no encoding has that value."""
    from sass.gen_sleigh import pattern
    k = b.k
    if isinstance(key, tuple) and key[0] == "zero":
        name = key[1]; atom = k.operand_types[name]
        zero = b.g.register_zero(k, name) if atom.type in REG_TYPES else 0
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
    """A decode-time choice: cases is ((values per key, Expr), ...)."""
    return Expr("sel", (tuple(keys), tuple(cases)), w)


def merge(trees, assignments):
    """One expression generalizing trees[i] (the value under assignments[i])."""
    first = trees[0]
    if all(t is first for t in trees): return first
    if all(isinstance(t, Expr) for t in trees) and len({(t.op, len(t.args), t.w) for t in trees}) == 1 \
            and first.op not in ("const", "param", "sel") \
            and all(all(not isinstance(a, Expr) and a == first.args[j] or isinstance(a, Expr) and isinstance(first.args[j], Expr)
                        for j, a in enumerate(t.args)) for t in trees):
        args = [merge([t.args[j] for t in trees], assignments) if isinstance(first.args[j], Expr) else first.args[j]
                for j in range(len(first.args))]
        return sem.ite(*args) if first.op == "ite" else Expr(first.op, tuple(args), first.w)
    keys = determining_keys(trees, assignments)
    cases = {}
    for t, a in zip(trees, assignments): cases.setdefault(tuple(a.get(k) for k in keys), t)
    return sel(keys, sorted(cases.items(), key=lambda kv: repr(kv[0])), first.w)


def determining_keys(trees, assignments):
    """The fewest static keys whose values determine the tree."""
    universe = sorted({k for a in assignments for k in a}, key=repr)
    for n in range(len(universe) + 1):
        for keys in itertools.combinations(universe, n):
            seen = {}
            if all(seen.setdefault(tuple(a.get(k) for k in keys), t) is t for t, a in zip(trees, assignments)): return keys
    raise AssertionError("assignments do not determine the trees")


# ---- lowering to p-code text ------------------------------------------------------------------

SLEIGH_OP = {"add": "+", "sub": "-", "mul": "*", "and": "&", "or": "|", "xor": "^", "shl": "<<", "shr": ">>",
             "sar": "s>>", "eq": "==", "ne": "!=", "ult": "<", "ule": "<=", "slt": "s<", "sle": "s<="}


class Lower:
    """Emit statements for expressions into `lines`, materializing shared nodes once."""

    def __init__(self, b, lines, uses):
        # Names are numbered per constructor, so identical subtables are shared.
        self.b, self.lines, self.uses, self.memo, self.serial = b, lines, uses, [{}], 0

    def label(self, stem):
        self.serial += 1
        return f"{stem}{self.serial}"

    def local(self, w, text):
        """A new local of w bits (its whole bytes) holding text."""
        n = nbytes(w)
        name = self.label("se")
        if n > 256:
            self.b.serial += 1
            name = f"SEMTMP{self.b.serial}_{n}"; self.b.g.temporaries[name] = n
            self.lines.append(f"{name} = {text};")
        else: self.lines.append(f"local {name}:{n} = {text};")
        return name

    def lookup(self, e):
        return next((m[e] for m in reversed(self.memo) if e in m), None)

    def value(self, e):
        """Operand text for e: an atom (symbol, constant) or a parenthesized expression. A w-bit
        value occupies its whole bytes with the bits above w zero."""
        if (hit := self.lookup(e)) is not None: return hit
        text = self.text(e)
        if self.uses.get(e, 0) > 1 and not re.fullmatch(r"[\w:]+", text): text = self.local(e.w, text)
        self.memo[-1][e] = text
        return text

    def clean(self, w, text):
        """text, a value in nbytes(w) bytes whose bits above w may be set, with those bits cleared."""
        return text if w % 8 == 0 else f"({text} & {sem.mask(w):#x}:{nbytes(w)})"

    def widened(self, e, w):
        """e's value zero-extended to nbytes(w) bytes (no operation if the byte size is the same)."""
        v = self.value(e)
        return v if nbytes(e.w) == nbytes(w) else self.local(w, f"zext({v})")

    def signed(self, e):
        """e's value sign-extended from bit e.w - 1 to its whole bytes (for signed operations)."""
        v = self.value(e)
        if e.w % 8 == 0: return v
        k = 8 * nbytes(e.w) - e.w
        return self.local(e.w, f"({self.atom(e)} << {k}) s>> {k}")

    def text(self, e):
        a, n = e.args, nbytes(e.w)
        match e.op:
            case "const":
                if n > 8: return self.local(e.w, f"zext({a[0]:#x}:8)")
                return f"{a[0]:#x}:{n}"
            case "param": return self.param(a[0], e.w)
            case "zext": return self.widened(a[0], e.w)
            case "sext":
                v = self.signed(a[0])
                return self.clean(e.w, v if nbytes(a[0].w) == n else self.local(e.w, f"sext({v})"))
            case "extract":
                x, hi, lo = a
                if lo == 0 and (self.uses.get(x, 0) > 1 or self.lookup(x) is not None or (m := sem.narrowed(x, e.w)) is e):
                    # The low bits of a value computed anyway; otherwise compute narrow.
                    v = self.atom(x)
                    return self.clean(e.w, v if nbytes(x.w) == n else f"{v}:{n}")
                if lo == 0: return self.value(m)
                v = f"({self.atom(x)} >> {lo})"
                return self.clean(e.w, v if nbytes(x.w) == n else f"{v}:{n}")
            case "not" if e.w == 1: return f"!{self.atom(a[0])}"
            case "not": return self.clean(e.w, f"~{self.atom(a[0])}")
            case "xor" if sem.is_const(a[1]) and a[0].op == "zext" and a[0].args[0].w % 8 == 0 \
                    and sem.value(a[1]) == sem.mask(a[0].args[0].w):
                # Inverting the low bytes of a zero-extended value: zext(~x), as compact as written.
                return self.local(e.w, f"zext(~{self.atom(a[0].args[0])})")
            case "neg": return self.clean(e.w, f"-{self.atom(a[0])}")
            case "concat":
                hi, lo = a
                return self.local(e.w, f"({self.widened(hi, e.w)} << {lo.w}) | {self.widened(lo, e.w)}")
            case "ite": return self.ite(*a)
            case "sel": return self.sel(e)
            case "load": return self.local(e.w, f"*[{a[0]}]:{n} {self.atom(a[1])}")
            case "sar": return self.clean(e.w, f"({self.signed(a[0])} s>> {self.value(a[1])})")
            case "slt" | "sle": return f"({self.signed(a[0])} {SLEIGH_OP[e.op]} {self.signed(a[1])})"
            case "add" | "sub" | "mul" | "shl": return self.clean(e.w, f"({self.value(a[0])} {SLEIGH_OP[e.op]} {self.value(a[1])})")
            case op if op in SLEIGH_OP: return f"({self.value(a[0])} {SLEIGH_OP[op]} {self.value(a[1])})"
        raise NotImplementedError(f"lowering of {e.op}")

    def atom(self, e):
        v = self.value(e)
        return v if re.fullmatch(r"[\w:]+", v) else f"({v})"

    def param(self, name, w):
        if name == "__inst_start": return "inst_start" if w == 64 else self.clean(w, f"inst_start:{nbytes(w)}")
        v = self.b.values[name]
        n = nbytes(w)
        if REG_TYPES.get(self.b.k.operand_types[name].type) in ("P", "UP") or v.size * 8 == w: return v.symbol  # predicates are 0/1
        if v.size > n: return self.clean(w, f"{v.symbol}:{n}")
        if v.size == n: return self.clean(w, v.symbol)
        return self.local(w, f"zext({v.symbol})")

    def cheap(self, e):
        return e.op in ("const", "param") or self.lookup(e) is not None

    def ite(self, c, x, y):
        """A dynamic choice. With a cheap arm (constant or value at hand) it is the default and the
        other arm overwrites it conditionally (one CBRANCH); otherwise a two-armed diamond."""
        r = self.label("se")
        if self.cheap(x) or self.cheap(y):
            first, other, cond = (x, y, self.value(c)) if self.cheap(x) else (y, x, f"!{self.atom(c)}")
            self.lines.append(f"local {r}:{nbytes(x.w)} = {self.value(first)};")
            done = self.label("done")
            self.lines.append(f"if ({cond}) goto <{done}>;")
            self.memo.append({}); self.lines.append(f"{r} = {self.value(other)};"); self.memo.pop()
            self.lines.append(f"<{done}>")
            return r
        self.lines.append(f"local {r}:{nbytes(x.w)} = 0;")
        then, done = self.label("then"), self.label("done")
        self.lines.append(f"if ({self.value(c)}) goto <{then}>;")
        self.memo.append({}); self.lines.append(f"{r} = {self.value(y)};"); self.memo.pop()
        self.lines += [f"goto <{done}>;", f"<{then}>"]
        self.memo.append({}); self.lines.append(f"{r} = {self.value(x)};"); self.memo.pop()
        self.lines.append(f"<{done}>")
        return r

    def sel(self, e):
        """A decode-time choice: a subtable with one constructor per case, exporting its value."""
        from sass.gen_sleigh import pattern
        keys, cases = e.args
        constructors = []
        for values, tree in cases:
            cl = [c for k, v in zip(keys, values) if v is not None for c in clauses(self.b, k, v)]
            lines = []
            inner = Lower(self.b, lines, count_uses([tree]))
            v = inner.value(tree)
            code = " ".join(lines) + (f" export {v};" if re.fullmatch(r"\w+", v) else f" local t:{nbytes(e.w)} = {v}; export t;")
            refs = [r for r in self.b.refs if r[1] in set(re.findall(r"\b\w+\b", code))]
            constructors.append(('""', pattern(cl, refs), "", code.strip()))
        table = self.b.g.subtable("sel", constructors)
        self.b.refs.append(("sub", table))
        return table


def count_uses(roots):
    uses = defaultdict(int)
    def visit(e):
        if not isinstance(e, Expr): return
        uses[e] += 1
        if uses[e] > 1 or e.op == "sel": return
        for a in e.args: visit(a)
    for r in roots: visit(r)
    return uses


def lower_writes(b, writes):
    """Statements writing [(output name, Expr)]: values computed first, the first write inline."""
    lines = []
    low = Lower(b, lines, count_uses([e for _, e in writes]))
    values = [(n, e) for n, e in writes]
    texts = [None] + [low.value(e) for _, e in values[1:]]
    if len(values) > 1:  # capture later values before the first write could alias their inputs
        texts = [None] + [t if re.fullmatch(r"[\w:]+", t) else low.local(e.w, t) for t, (_, e) in zip(texts[1:], values[1:])]
    first = low.value(values[0][1])
    lines.append(f"{b.outputs[values[0][0]].symbol} = {first};")
    lines += [f"{b.outputs[n].symbol} = {t};" for (n, _), t in zip(values[1:], texts[1:])]
    return lines


# ---- the class -------------------------------------------------------------------------------

def emit(b):
    """Lower the class's family into b.body; returns (status, reason) like operations.emit."""
    from sass.gen_sleigh import pattern
    from sass.operations import dispatch
    fam = FAMILIES[b.k.mnemonic]
    rows = enumerate_rows(b, fam)
    native = [(a, r) for a, r in rows if not isinstance(r, Exception)]
    if not native:
        return "opaque", "; ".join(sorted({str(r) for _, r in rows})) or "no modelled encodings"
    i = Symbolic(b, {})
    resolved = [(a, [(i.resolve(n), e) for n, e in r.ordered() if i.has_operand(n)]) for a, r in native]
    for a, writes in resolved:
        for n, e in writes:
            if n not in b.outputs: raise TypeError(f"{b.k.name}: {n} is not an output")
            if e.w != i.width(n): raise TypeError(f"{b.k.name}: {n} is {i.width(n)} bits, value {e!r} is {e.w}")
    # Liveness: when every optional predicate destination is PT, those writes and the work only
    # they need are dropped; otherwise everything is written (PT writes go to a sink).
    names = [n for n, _ in resolved[0][1]]
    optional = [n for n in names if REG_TYPES.get(b.k.operand_types[n].type) in ("P", "UP")
                and clauses(b, ("zero", n), True) not in (None, [])]
    assignments = [a for a, _ in resolved]
    def body(live):
        merged = [(n, merge([dict(w)[n] for _, w in resolved], assignments)) for n in live]
        return lower_writes(b, merged) if merged else []
    bodies = [([], body(names))]
    if optional:
        bodies.insert(0, ([c for n in optional for c in clauses(b, ("zero", n), True)], body([n for n in names if n not in optional])))
    if len(bodies) == 1: b.body += bodies[0][1]
    else:
        cons = []
        for cond, lines in bodies:
            code = " ".join(lines)
            refs = [r for r in b.refs if r[1] in set(re.findall(r"\b\w+\b", code))]
            cons.append(('""', pattern(cond, refs), "", code))
        table = b.g.subtable("live", cons); b.refs.append(("sub", table)); b.body.append(f"build {table};")
    # Unmodelled combinations keep the complete opaque fallback.
    keys = sorted({k for a, _ in rows for k in a}, key=repr)
    if len(native) < len(rows):
        b.selector_patterns = [[c for k, v in a.items() for c in clauses(b, k, v)] for a, _ in native]
    b.selector_combinations = {",".join(map(str, map(builder_key, keys))): [[a.get(k) for k in keys] for a, _ in native]} if keys else {}
    dispatch(b)
    return "native", ("decode-time selector guards retain complete opaque fallbacks" if len(native) < len(rows)
                      else "semantics family")
