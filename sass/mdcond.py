"""md CONDITIONS as decode constraints: which encodings of a class are legal.

nvdisasm prints condition-violating words; our decoders reject them by default. pydecode checks
decoded values directly (`violation`). SLEIGH, for the classes lifted by semantics families, needs
the legal set as patterns over raw encoding fields (`legal_classes`): every condition subexpression that reads a single field partitions that
field's raw values into classes, the condition is evaluated on one representative per class
combination, and the legal combinations become Cartesian boxes of raw value sets.
"""
import ast, itertools
from functools import cache
from sass import ir, mdexpr, pydecode

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
    names = mdexpr.condition_source(arch_name, expr)[1]
    return bool(names) and all(mdexpr.env_key(n) in hints(k) for n in names)


def operand_errors(arch_name, k):
    """The error conditions on what the instruction does (all but hint_only): what the generated
    SLEIGH enforces, because the scheduling-hint tables are most of the conditions' .sla size."""
    return [c for c in errors(k) if not hint_only(arch_name, k, c[1])]


def violation(arch, k, env, hints=True):
    """The first violated error condition, or None; hints=False skips the hint_only ones."""
    conditions = errors(k) if hints else operand_errors(arch.name, k)
    return next(((kind, msg) for kind, expr, msg in conditions if not mdexpr.holds(arch, expr, env)), None)


def sources(k):
    """env key -> (field, decoder) for every operand value an encoding field sets."""
    out = {}
    for f, r in k.rules:
        match r:
            case ("op", name, scale, conv): out.setdefault(name, (f, lambda v, s=scale, c=conv: v if c else v * s))
            case ("attr", name, attr): out.setdefault((name, attr), (f, lambda v: v))
            case ("table", "IDENTICAL", args):
                for a in args: out.setdefault(pydecode.key(a), (f, lambda v: v))
    return out


def table_sources(arch, k):
    """env key -> (field, raw value -> decoded value) for table-encoded operands (first row wins)."""
    out = {}
    for f, r in k.rules:
        if r[0] != "table" or r[1] == "IDENTICAL" or r[1] not in arch.tables: continue
        rows = pydecode.table_inverse(arch, r[1])
        for i, a in enumerate(r[2]):
            if a[0] != "const":
                out.setdefault(pydecode.key(a), (f, lambda v, rows=rows, i=i: rows[v][0][i] if v in rows else None))
    return out


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
    py, names, tables = mdexpr.condition_source(arch_name, expr)
    src = {**table_sources(arch, k), **sources(k)}
    field_of, fields = {}, {}
    for n in names:
        key = mdexpr.env_key(n)
        if key in src:
            f = src[key][0]
            field_of[n.replace(".", "__")] = f.ranges; fields[f.ranges] = f
    glb = mdexpr.condition_globals(arch_name, tables)
    def env_for(raws):
        values = {}
        for n in names:
            key = mdexpr.env_key(n)
            values[n.replace(".", "__")] = (src[key][1](raws[src[key][0].ranges]) if key in src else None) or 0
        return values
    code = compile(py, expr, "eval")
    if not fields: return None if eval(code, glb, env_for({})) else []
    # Partition each field's raw values by the values of the subexpressions that read only it.
    parts = {}
    for ranges, exprs in atoms(ast.parse(py, mode="eval"), field_of).items():
        f = fields[ranges]
        if f.width > MAX_FIELD_BITS: raise ValueError(f"{cls}: {f.width}-bit field in {expr!r}")
        codes = [compile(e, e, "eval") for e in exprs]
        groups = {}
        for v in range(1 << f.width):
            groups.setdefault(tuple(eval(c, glb, env_for({**{r: 0 for r in fields}, ranges: v})) for c in codes), []).append(v)
        parts[ranges] = [tuple(g) for g in groups.values()]
    order = list(parts)
    combos = list(itertools.product(*(range(len(parts[r])) for r in order)))
    if len(combos) > MAX_COMBINATIONS: raise ValueError(f"{cls}: {len(combos)} combinations in {expr!r}")
    legal = frozenset(c for c in combos if eval(code, glb, env_for({r: parts[r][i][0] for r, i in zip(order, c)})))
    if len(legal) == len(combos): return None
    if not legal: return []
    return tuple(fields[r] for r in order), tuple(tuple(parts[r]) for r in order), legal
