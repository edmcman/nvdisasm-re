"""md CONDITIONS as decode constraints: which encodings of a class are legal.

nvdisasm prints condition-violating words; our decoders reject them by default. pydecode checks
decoded values directly (`violation`). SLEIGH, for the classes lifted by semantics families, needs
the legal set as patterns over raw encoding fields (`legal_classes`): every condition subexpression that reads a single field partitions that
field's raw values into classes, the condition is evaluated on one representative per class
combination, and the legal combinations become Cartesian boxes of raw value sets.
"""
import ast, itertools
from functools import cache
from sass.decode import ir, pydecode
from sass.md import expression

MAX_FIELD_BITS = 16    # raw values enumerated per field
MAX_COMBINATIONS = 1 << 16


def errors(k):
    """The class's conditions that make an encoding illegal (`*_ERROR`; warnings don't)."""
    return [(kind, expr, msg) for kind, expr, msg in k.conditions if kind.endswith("_ERROR")]


def hints(k):
    """Scheduling-hint operands: the `$( ... )$` groups and the .reuse flags."""
    return set(k.sched) | {n for n, a in k.operand_types.items() if a.type == "REUSE"}


def hint_only(arch_name, k, expr):
    """The condition reads only scheduling hints: they change timing, not what the instruction does."""
    names = expression.condition_source(arch_name, expr)[1]
    return bool(names) and all(expression.env_key(n) in hints(k) for n in names)


def operand_errors(arch_name, k):
    """The error conditions on what the instruction does (all but hint_only): what the generated
    SLEIGH enforces, because the scheduling-hint tables are most of the conditions' .sla size."""
    return [c for c in errors(k) if not hint_only(arch_name, k, c[1])]


def violation(arch, k, env, hints=True):
    """The first violated error condition, or None; hints=False skips the hint_only ones."""
    conditions = errors(k) if hints else operand_errors(arch.name, k)
    return next(((kind, msg) for kind, expr, msg in conditions if not expression.holds(arch, expr, env)), None)


def sources(k):
    """env key -> (field, decoder) for every operand value an encoding field sets; a decoder
    (`decode`) is hashable, so conditions on the same fields share their enumeration."""
    out = {}
    for f, r in k.rules:
        match r:
            case ("op", name, scale, conv): out.setdefault(name, (f, ("scale", 1 if conv else scale)))
            case ("attr", name, attr): out.setdefault((name, attr), (f, ("scale", 1)))
            case ("table", "IDENTICAL", args):
                for a in args: out.setdefault(pydecode.key(a), (f, ("scale", 1)))
    return out


def table_sources(arch, k):
    """env key -> (field, decoder) for table-encoded operands (first row wins)."""
    out = {}
    for f, r in k.rules:
        if r[0] != "table" or r[1] == "IDENTICAL" or r[1] not in arch.tables: continue
        for i, a in enumerate(r[2]):
            if a[0] != "const": out.setdefault(pydecode.key(a), (f, ("table", r[1], i)))
    return out


def decode(arch, decoder, v):
    """A field's raw value -> the operand value `decoder` (from `sources`) gives it."""
    match decoder:
        case ("scale", s): return v * s
        case ("table", name, i):
            rows = pydecode.table_inverse(arch, name)
            return rows[v][0][i] if v in rows else None


def names_of(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def atoms(tree, field_of):
    """Maximal subexpressions reading exactly one field: field -> [expression source]."""
    out = {}
    def visit(node):
        fields = {field_of[n] for n in names_of(node) if n in field_of}
        if len(fields) == 1:
            out.setdefault(next(iter(fields)), []).append(ast.unparse(node)); return
        for child in ast.iter_child_nodes(node): visit(child)
    visit(tree)
    return out


@cache
def legal_classes(arch_name, cls, expr):
    """Legal raw values of one error condition: None if always legal, [] if never, else
    (fields, classes, legal): `fields` the encoding fields read, `classes[i]` a partition of field i's
    raw values (tuples) into values the condition can't tell apart, `legal` the set of class-index
    tuples for which it holds.

    Raises ValueError when a field is too wide to enumerate."""
    arch = ir.load(arch_name)
    k = next(c for c in arch.classes if c.name == cls)
    src = {**table_sources(arch, k), **sources(k)}
    names = expression.condition_source(arch_name, expr)[1]
    try: return enumerate_legal(arch_name, expr, tuple((n, src.get(expression.env_key(n))) for n in names))
    except ValueError as e: raise ValueError(f"{cls}: {e}") from None


@cache
def enumerate_legal(arch_name, expr, operands):
    """`legal_classes` given, per operand name the condition reads, its (field, decoder) or None:
    all a class contributes, so classes with the same fields share the enumeration."""
    arch = ir.load(arch_name)
    py, _, tables = expression.condition_source(arch_name, expr)
    field_of, fields = {}, {}
    for n, s in operands:
        if s: field_of[n.replace(".", "__")] = s[0].ranges; fields[s[0].ranges] = s[0]
    glb = expression.condition_globals(arch_name, tables)
    def env_for(raws):
        return {n.replace(".", "__"): (decode(arch, s[1], raws[s[0].ranges]) if s else None) or 0 for n, s in operands}
    code = compile(py, expr, "eval")
    if not fields: return None if eval(code, glb, env_for({})) else []
    # Partition each field's raw values by the values of the subexpressions that read only it.
    parts = {}
    for ranges, exprs in atoms(ast.parse(py, mode="eval"), field_of).items():
        f = fields[ranges]
        if f.width > MAX_FIELD_BITS: raise ValueError(f"{f.width}-bit field in {expr!r}")
        codes = [compile(e, e, "eval") for e in exprs]
        groups = {}
        for v in range(1 << f.width):
            groups.setdefault(tuple(eval(c, glb, env_for({**{r: 0 for r in fields}, ranges: v})) for c in codes), []).append(v)
        parts[ranges] = [tuple(g) for g in groups.values()]
    order = list(parts)
    combos = list(itertools.product(*(range(len(parts[r])) for r in order)))
    if len(combos) > MAX_COMBINATIONS: raise ValueError(f"{len(combos)} combinations in {expr!r}")
    legal = frozenset(c for c in combos if eval(code, glb, env_for({r: parts[r][i][0] for r, i in zip(order, c)})))
    if len(legal) == len(combos): return None
    if not legal: return []
    return tuple(fields[r] for r in order), tuple(tuple(parts[r]) for r in order), legal
