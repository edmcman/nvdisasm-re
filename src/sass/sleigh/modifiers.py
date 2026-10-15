"""A class's modifiers, its fields beyond the operand form: `name=LABEL` per modifier but the
scheduling hints (FADD's `ftz`, `rnd`, `sat`), and `operand@flag` per operand flag (`Ra@negate`),
set or unset. A form is one combination of their values. Shared by the generator (each value's
semantics status), tests/evidence.py (the forms the tests ran) and the coverage report."""
import itertools, math
from sass.decode import ir
from sass.md import expression, legal

FLAG = ("unset", "set")


def modifiers(k):
    """[(key, enum type)] in format order: (name, type) per modifier, ((operand, flag), None) per
    operand flag; a flag precedes its operand, and the guard's are skipped."""
    out, flags = [], []
    for a in k.format:
        match a.kind:
            case "flag": flags.append(a.name)
            case "mod" if a.name not in legal.hints(k): out.append((a.name, a.type))
            case "operand": out += [((a.name, f), None) for f in flags]; flags = []
            case "guard": flags = []
    return out


def name(key):
    return f"{key[0]}@{key[1]}" if isinstance(key, tuple) else key


def label(md, etype, v):
    """A modifier value's text: the enum member nvdisasm prints (the last defined), a flag's set/unset."""
    if etype is None: return FLAG[bool(v)]
    return str(md.rev_enums.get(etype, {}).get(v, v))


def settings(arch, d):
    """{modifier: label} of decoded instruction `d`, for the modifiers its encoding holds."""
    md = ir.load(arch)
    return {name(key): label(md, t, v) for key, t in modifiers(d.klass) if (v := d.env.get(key)) is not None}


def form(s):
    """A combination's canonical text, sorted: `modifier=label`, a set flag's name, no unset flag."""
    return " ".join(m if v == FLAG[1] and "@" in m else f"{m}={v}" for m, v in sorted(s.items())
                    if v is not None and not (v == FLAG[0] and "@" in m))


def domain(b):
    """{key: {label: value}} that Builder b's class can encode; modifiers its encoding doesn't hold
    (a fixed default) are left out, as `settings` leaves them out."""
    md, out = b.g.arch, {}
    for key, t in modifiers(b.k):
        if key in b.known: encoded = {b.known[key]}
        elif raw := b.g.raw_values(b.k, key): encoded = set(raw[1].values())
        else: continue
        values = range(2) if t is None else dict.fromkeys(v for _, v in md.enum_defs.get(t, []))
        out[key] = {label(md, t, v): v for v in values if v in encoded}
    return out


def admits(assignment, key, v):
    """A generator row's static assignment allows modifier `key` == v."""
    def ok(k, x):
        match k:
            case ("in", n, vs) if n == key: return (v in vs) == x
            case _ if k == key: return x == v
            case _: return True
    return all(ok(k, x) for k, x in assignment.items())


def combined(ss):
    """One status for statuses that apply together (rows, or a class's variants)."""
    parts = {p for s in ss for p in ((s.removeprefix("partially "), "opaque") if s.startswith("partially ") else (s,))}
    match sorted(parts):
        case [s]: return s
        case ps if "opaque" in ps: return "partially native" if "native" in ps else "partially primitive"
        case _: return "primitive"


def labelled(b, rows):
    """(domain with values, rows): the illegal rows and those admitting no labelled value of some
    modifier (F2FP's srcfmt outside its enum) dropped."""
    dom = {key: values for key, values in domain(b).items() if values}
    return dom, [(a, s) for a, s in rows if s and all(any(admits(a, key, v) for v in vs.values()) for key, vs in dom.items())]


def conditions(b, dom):
    """The class's md error conditions that read only its modifiers, and the modifiers they read:
    the legal combinations (F2FP: srcfmt F32 only with dstfmt F16, E6M9 or BF16)."""
    out, read = [], set()
    for _, expr, _ in legal.operand_errors(b.g.arch.name, b.k):
        names = {expression.env_key(n) for n in expression.condition_source(b.g.arch.name, expr)[1]}
        if names and names <= dom.keys(): out.append(expr); read |= names
    return out, read


def combinations(b, rows):
    """{"labels": {modifier: [label]}, "decides": [modifier], "table": [[label per deciding modifier...,
    status]], "free": n}: the status of every legal combination of the modifiers some row restricts or
    some md condition reads; each entry stands for `free` combinations of the other modifiers, which
    change neither. Combinations only illegal rows admit are left out too."""
    dom, rows = labelled(b, rows)
    exprs, read = conditions(b, dom)
    decides = [key for key in dom if key in read or any(not admits(a, key, v) for a, _ in rows for v in dom[key].values())]
    table = []
    for labs in itertools.product(*(dom[key].items() for key in decides)):
        env = {key: v for key, (_, v) in zip(decides, labs)}
        if not all(expression.holds(b.g.arch, e, env) for e in exprs): continue
        if ss := {s for a, s in rows if all(admits(a, key, v) for key, v in env.items())}:
            table.append([*(lab for lab, _ in labs), combined(ss)])
    labels = {name(k): list(dom[k]) for k in dom}
    return minimal(dict(labels=labels, decides=[name(k) for k in decides], table=table,
                        free=math.prod(len(dom[k]) for k in dom if k not in decides)))


def value_statuses(c):
    """{modifier: {label: status}} from a combination table: the combined status of the legal
    combinations with that choice; a choice no legal combination makes is left out."""
    out = {}
    for m, labs in c["labels"].items():
        i = c["decides"].index(m) if m in c["decides"] else None
        found = {l: {e[-1] for e in c["table"] if i is None or e[i] == l} for l in labs}
        out[m] = {l: combined(ss) for l, ss in found.items() if ss}
    return out


def minimal(c):
    """The table without the modifiers it doesn't depend on: one whose every choice gives the same
    status, the others fixed, multiplies `free` instead."""
    while True:
        for i, m in enumerate(c["decides"]):
            groups = {}
            for entry in c["table"]: groups.setdefault(tuple(entry[:i] + entry[i + 1:]), set()).add(entry[i])
            if all(len(g) == len(c["labels"][m]) for g in groups.values()):
                c = dict(labels=c["labels"], decides=c["decides"][:i] + c["decides"][i + 1:], free=c["free"] * len(c["labels"][m]),
                         table=[list(k) for k in groups])
                break
        else: return c


def merge(a, b):
    """Two variants' combination tables as one: a combination both produce gets their combined status."""
    labels = {m: list(dict.fromkeys(a["labels"].get(m, []) + b["labels"].get(m, []))) for m in {**a["labels"], **b["labels"]}}
    decides = [m for m in labels if m in a["decides"] or m in b["decides"]]
    def expand(c):
        out = {}
        for *labs, status in c["table"]:
            fixed = dict(zip(c["decides"], labs))
            for rest in itertools.product(*(c["labels"].get(m, []) for m in decides if m not in fixed)):
                free = iter(rest)
                out[tuple(fixed[m] if m in fixed else next(free) for m in decides)] = status
        return out
    ea, eb = expand(a), expand(b)
    order = lambda labs: [labels[m].index(l) for m, l in zip(decides, labs)]
    return minimal(dict(labels=labels, decides=decides, free=math.prod(len(labels[m]) for m in labels if m not in decides),
                        table=[[*labs, combined({s for s in (ea.get(labs), eb.get(labs)) if s})] for labs in sorted(ea.keys() | eb.keys(), key=order)]))
