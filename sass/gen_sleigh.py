"""Generate a SLEIGH decoder from the md decode IR.

Display reuses pydecode.render_items() symbolically: for every FORMAT item, each combination of the
conditions that change its shape (operand at default, flag set, ...) is rendered once with placeholder
values and becomes one subtable constructor guarded by those conditions on the encoding.

usage: python3 -m sass.gen_sleigh SM89 [SM90 ...]
"""
import itertools, json, math, re, sys
import mdlib
from collections import defaultdict
from pathlib import Path
from sass import ir, mdexpr, pydecode
from sass.pydecode import IMM, PREFIX, NoMatch, resolve, sext, value_text

OUT = Path(__import__("os").environ.get("SASS_SLEIGH_OUT") or
           Path(__file__).resolve().parent.parent / "processor" / "SASS" / "data" / "languages")
MAX_ENUM_BITS = 12
PH = "\x00{}\x00"  # placeholder for a value symbol inside a rendered template

# register file -> (register-space offset, element bytes, count, zero/true register index, its name)
REG_FILES = {"R": (0x0000, 4, 256, 255, "RZ"), "UR": (0x0400, 4, 64, 63, "URZ"),
             "P": (0x0800, 1, 8, 7, "PT"), "UP": (0x0808, 1, 8, 7, "UPT")}
SPECIAL_BASE = 0x3000  # SpecialRegister file, named from the md enum
RESOURCE_FILE = {"GPR": "R", "UGPR": "UR", "PRED": "P", "UPRED": "UP"}
ENUM_FILE = {"Register": "R", "NonZeroRegister": "R", "ZeroRegister": "R",
             "UniformRegister": "UR", "NonZeroUniformRegister": "UR", "ZeroUniformRegister": "UR",
             "Predicate": "P", "UniformPredicate": "UP"}
SPANS = tuple(range(1, 9))

# ---------- fields and constraints ----------

def pieces(f: ir.Field):
    """(token, lo, hi, width) per range, MSB first, split at the 64-bit token boundary."""
    out = []
    for hi, lo in f.ranges:
        if lo < 64 <= hi: out += [("h", 0, hi - 64, hi - 63), ("l", lo, 63, 64 - lo)]
        elif hi < 64: out.append(("l", lo, hi, hi - lo + 1))
        else: out.append(("h", lo - 64, hi - 64, hi - lo + 1))
    return out

def split_value(f, v):
    out = []
    for tok, lo, hi, n in reversed(pieces(f)):
        out.append((tok, lo, hi, v & (1 << n) - 1)); v >>= n
    return out[::-1]

# A constraint is a list of clauses (AND); a clause is a list of literals (OR); a literal maps token -> text (AND).
def lit(tok, text): return {tok: text}

def eq(gen, f, v):
    return [[lit(tok, f"{gen.field(tok, lo, hi)}={pv}")] for tok, lo, hi, pv in split_value(f, v)]

def member(gen, f, values):
    """Clauses requiring f in values (a set of raw field values)."""
    universe = 1 << f.width
    values = {v for v in values if 0 <= v < universe}
    if len(values) == universe: return []
    if not values: return [[]]  # unsatisfiable
    ps = pieces(f)
    if len(ps) == 1:
        tok, lo, hi, _ = ps[0]
        # Cover intervals with aligned power-of-two blocks. Each block constrains
        # only its fixed high bits; inequalities expand into many decision states.
        terms = []
        for a, b in intervals(sorted(values)):
            while a <= b:
                size = min(a & -a if a else universe, 1 << ((b - a + 1).bit_length() - 1))
                shift = size.bit_length() - 1
                name = gen.field(tok, lo + shift, hi)
                terms.append(lit(tok, f"{name}={a >> shift}"))
                a += size
        return [terms]
    if (factored := product_split(f, values)) is not None:  # S = S1 x S2 x ...: one small clause per piece
        out = []
        for (tok, lo, hi, n), vs in zip(ps, factored):
            out += member(gen, ir.Field("", ((hi + (64 if tok == "h" else 0), lo + (64 if tok == "h" else 0)),)), vs)
        return out
    return [[merge_lits(eq(gen, f, v)) for v in sorted(values)]]

def product_split(f, values, care=None):
    """Per-piece value sets whose Cartesian product equals `values` on `care` (default: everywhere), else None."""
    proj = [set() for _ in pieces(f)]
    for v in values:
        for j, (_, _, _, pv) in enumerate(split_value(f, v)): proj[j].add(pv)
    inside = lambda v: all(pv in proj[j] for j, (_, _, _, pv) in enumerate(split_value(f, v)))
    universe = range(1 << f.width) if care is None else care
    return proj if all(inside(v) == (v in values) for v in universe) else None

def merge_lits(clauses):
    out = {}
    for (l,) in clauses:
        for tok, t in l.items(): out[tok] = f"{out[tok]} & {t}" if tok in out else t
    return out

def intervals(vs):
    runs = []
    for v in vs:
        if runs and runs[-1][1] == v - 1: runs[-1][1] = v
        else: runs.append([v, v])
    return runs

def span_boxes(combos):
    """Disjoint Cartesian rectangles covering exactly the given modifier tuples."""
    if not combos or not combos[0]:
        return [()] if combos else []
    tails = defaultdict(set)
    for head, *tail in combos:
        tails[head].add(tuple(tail))
    groups = defaultdict(set)
    for head, tail in tails.items():
        groups[frozenset(tail)].add(head)
    return [(frozenset(heads),) + box for tail, heads in groups.items()
            for box in span_boxes(sorted(tail))]

def pattern(clauses, refs=()):
    """SLEIGH pattern: DNF over cross-token clauses, each term `(lo-group ; hi-group)`; refs are extra operands:
    ("l"|"h", field) or ("sub", subtable)."""
    if any(not c for c in clauses): return None
    subs = list(dict.fromkeys(r for k, r in refs if k == "sub"))
    refs = list(dict.fromkeys((k, r) for k, r in refs if k != "sub"))
    local = defaultdict(list)
    cross = []
    for c in clauses:
        toks = {t for l in c for t in l}
        if len(toks) == 1 and all(len(l) == 1 for l in c):
            (tok,) = toks
            local[tok].append(c[0][tok] if len(c) == 1 else "(" + " | ".join(l[tok] for l in c) + ")")
        else:
            cross.append(c)
    terms = []
    for choice in itertools.product(*cross):
        groups = {t: list(local[t]) for t in "lh"}
        for l in choice:
            for tok, t in l.items(): groups[tok].append(f"({t})")
        if len(cross) == 0:  # a single term can carry the operand references directly
            for kind, ref in refs: groups[kind].append(ref)
        terms.append(group(groups))
    pat = terms[0] if len(terms) == 1 else "(" + " | ".join(terms) + ")"
    if cross and refs:  # operands may not be defined in several branches of an OR
        pat += " & " + group({t: [r for k, r in refs if k == t] for t in "lh"})
    return pat + "".join(f" & {s}" for s in subs)

def group(groups):
    return "(" + (" & ".join(groups["l"]) or "lany") + " ; " + (" & ".join(groups["h"]) or "hany") + ")"

# ---------- generator ----------

class Gen:
    def __init__(self, archname):
        self.arch = ir.load(archname)
        self.fields = {}              # (tok, lo, hi, kind) -> name
        self.attach = {}              # field name -> names list
        self.name_fields = {}         # (token range, names) -> shared display field
        self.tables = {}              # constructor text -> subtable name
        self.table_lines = []
        self.skipped = []
        self.attach_vars = {}         # field name -> register names
        self.spans = {file: {1} for file in REG_FILES}
        self.special_spans = set()
        self.pcodeops = set()
        md = mdlib.load_classes(mdlib.OUT / archname)
        self.props = {c["cls"]: c["props"] for c in md}
        self.sizes = {c["cls"]: c["preds"] for c in md}
        self.coverage = {}
        self.temporaries = {}
        self.roles = {r["cls"]: r for p in (mdlib.OUT / archname / "semantics").glob("*.json") for r in json.loads(p.read_text())}

    def field(self, tok, lo, hi, kind=""):
        name = f"{tok}{lo}_{hi}{kind}"
        self.fields[(tok, lo, hi, kind)] = name
        return name

    def subtable(self, prefix, constructors):
        """Dedupe identical subtables; constructors are (display, pattern, action[, semantics]) tuples."""
        key = (prefix, tuple(constructors))
        if key not in self.tables:
            name = f"{prefix}{len(self.tables)}"
            self.tables[key] = name
            for disp, pat, action, *sem in constructors:
                self.table_lines.append(f"{name}: {disp} is {pat}{action} {{ {sem[0] if sem else ''} }}")
        return self.tables[key]

    def name_field(self, tok, lo, hi, names):
        key = (tok, lo, hi, tuple(names))
        if key not in self.name_fields:
            fname = self.field(tok, lo, hi, f"_{len(self.attach)}")
            self.attach[fname] = names
            self.name_fields[key] = fname
        return self.name_fields[key]

    # --- operand value sources ---

    def source(self, k, name):
        for f, r in k.rules:
            match r:
                case ("op", n, scale, conv) if n == name: return ("field", f, scale)
                case ("table", "IDENTICAL", args) if any(x[1] == name for x in args): return ("field", f, 1)
        for f, r in k.rules:
            match r:
                case ("table", t, args) if t in self.arch.tables:
                    for i, a in enumerate(args):
                        if a[0] != "const" and pydecode.key(a) == name: return ("table", f, t, i)
        return None

    def attr_source(self, k, name, attr):
        for f, r in k.rules:
            if r == ("attr", name, attr): return ("field", f, 1)
        return self.source(k, (name, attr))

    def table_values(self, f, tname, i):
        """Raw table-field value -> decoded value of argument i (first matching row)."""
        out = {}
        for v in range(1 << f.width):
            rows = pydecode.table_inverse(self.arch, tname).get(v)
            if rows: out[v] = rows[0][i]
        return out

    def values_where(self, k, name, test):
        """Clauses for `test(decoded value of name)`; None if the value is not encoded."""
        src = self.source(k, name) if not isinstance(name, tuple) else self.attr_source(k, *name)
        match src:
            case ("field", f, scale): return member(self, f, {v for v in range(1 << f.width) if test(v * scale)}) \
                if f.width <= 16 else (eq(self, f, 0) if test(0) and not test(1) else None)
            case ("table", f, t, i): return member(self, f, {v for v, x in self.table_values(f, t, i).items() if test(x)})
        return None

    # --- display symbols ---

    def vsym(self, k, a, mod):
        """(display symbol, pattern refs) for atom a; mods render their own leading '.' and nothing at default."""
        arch = self.arch
        src = self.source(k, a.name)
        def text(v, widths):
            t, dflt = value_text(arch, a, {a.name: v}, widths)  # strictness is enforced by root constraints
            return ("" if dflt else "." + t) if mod else t
        match src:
            case None:
                return self.const(text(None, {}))
            case ("table", f, t, i):
                rows = self.table_values(f, t, i)
                return self.enum_table([(eq(self, f, v), text(x, {})) for v, x in rows.items()])
            case ("field", f, scale) if a.type in arch.enums or a.type == "BITSET":
                if f.width > MAX_ENUM_BITS: raise NoMatch(f"{a.name}: {f.width}-bit enum")
                widths = {a.name: f.width + scale.bit_length() - 1}
                if len(pieces(f)) == 1:
                    tok, lo, hi, _ = pieces(f)[0]
                    names = [text(v * scale, widths) for v in range(1 << f.width)]
                    fname = self.name_field(tok, lo, hi, names)
                    return fname, [(tok, fname)]
                return self.enum_table([(eq(self, f, v), text(v * scale, widths)) for v in range(1 << f.width)])
            case ("field", f, scale):
                return self.number(a, f, scale)
        raise NoMatch(f"{a.name}: no display for {a.type}")

    def const(self, s):
        return (f'"{s}"', []) if s else (None, [])

    def enum_table(self, cases):
        cons = [(f'"{s}"' if s else '""', pattern(c), "") for c, s in cases]
        name = self.subtable("e", [c for c in cons if c[1]])
        return name, [("sub", name)]

    def number(self, a, f, scale):
        m = IMM.match(a.type)
        if not m: raise NoMatch(f"{a.name}: {a.type}")
        signed = m["signed"] == "S" and not m["float"]
        ps = pieces(f)
        if len(ps) == 1 and scale == 1 and not m["rel"]:
            tok, lo, hi, _ = ps[0]
            fname = self.field(tok, lo, hi, "s" if signed else "u")
            return fname, [(tok, fname)]
        refs, expr, shift = [], None, 0
        for tok, lo, hi, n in reversed(ps):
            fname = self.field(tok, lo, hi, "u")
            refs.append((tok, fname))
            expr = fname if expr is None else f"({fname} << {shift}) | {expr}"
            shift += n
        if signed:
            sb = 1 << (f.width - 1)
            expr = f"((({expr}) ^ {sb}) - {sb})"
        if scale != 1: expr = f"({expr}) * {scale}"
        if m["rel"]: expr = f"inst_next + ({expr})"
        name = self.subtable("n", [("t", pattern([], refs), f" [ t = {expr}; ]")])
        return name, [("sub", name)]

    def branch_target(self, k, a):
        """Invisible subtable exporting a branch's destination (display keeps its own symbol)."""
        _, f, scale = self.source(k, a.name)
        refs, expr, shift = [], None, 0
        for tok, lo, hi, n in reversed(pieces(f)):
            fname = self.field(tok, lo, hi, "u")
            refs.append((tok, fname))
            expr = fname if expr is None else f"({fname} << {shift}) | {expr}"
            shift += n
        sb = 1 << (f.width - 1)
        # RSImm targets are relative to the next instruction; UImm targets are absolute.
        value = f"inst_next + ((({expr}) ^ {sb}) - {sb}) * {scale}" if a.type == "RSImm" else f"inst_start * 0 + ({expr}) * {scale}"
        return self.subtable("b", [("t", pattern([], refs), f" [ t = {value}; ]", "export *[ram]:8 t;")])

    # --- rendering ---

    def predicates(self, k):
        """Item index -> conditions that change that item's shape, found by a discovery render."""
        found = defaultdict(list)
        prev = [None]
        class Discover:
            def value(_, a, item):
                omittable = a.kind == "operand" and can_default(self.arch, a)
                # a modifier only changes shape right after an omittable operand: [RZ.X8+...]
                if omittable or a.kind == "mod" and prev[0] == ("omittable", item) and can_default(self.arch, a):
                    found[item].append(("default", a))
                prev[0] = ("omittable", item) if omittable else None
                return PH.format(a.name), False
            def flag(_, name, attr, item): found[item].append(("flag", name, attr)); return False
            def raw(_, name, item): found["desc"].append(("raw", name)); return 1
            def target(_, a, off): return off
        pydecode.render_items(k, Discover())
        desc = list(dict.fromkeys(found.pop("desc", [])))
        return {i: list(dict.fromkeys(ps)) for i, ps in found.items()}, desc

    def condition(self, k, p):
        """Positive form of predicate p: clauses, or True/False when the encoding fixes it."""
        match p:
            case ("default", a):
                d = default_value(self.arch, a)
                c, fixed = self.values_where(k, a.name, lambda x: x == d), True   # unencoded: value is the default
            case ("flag", name, attr):
                c, fixed = self.values_where(k, (name, attr), bool), False
            case ("raw", name):
                c, fixed = self.values_where(k, name, lambda x: x == 0), True
        if c is None: return fixed
        if any(not cl for cl in c): return False
        return c or True

    def truth(self, k, p):
        """(source field, raw value -> bool or None if undecodable) for predicate p, or a fixed bool."""
        match p:
            case ("default", a):
                d = default_value(self.arch, a); name, test, fixed, want = a.name, lambda x: x == d, True, d
            case ("flag", name, attr):
                name, test, fixed, want = (name, attr), bool, False, 1
            case ("raw", name):
                test, fixed, want = lambda x: x == 0, True, 0
        src = self.source(k, name) if not isinstance(name, tuple) else self.attr_source(k, *name)
        match src:
            case ("field", f, scale):
                truth = lambda r: test(r * scale)
                truth.want = want // scale if want % scale == 0 else None  # the raw value that makes it true
                return f, truth
            case ("table", f, t, i):
                vals = self.table_values(f, t, i)
                return f, lambda r: test(vals[r]) if r in vals else None
        return fixed

    def shape_cases(self, k, ps):
        """(truth assignment, clauses) covering every encoding of an item's predicates.
        A lone predicate on a field nests (positive constraint only, SLEIGH's most-specific match decides);
        several predicates on one field partition its values into disjoint cells."""
        fixed, by_field = {}, defaultdict(list)
        for p in ps:
            t = self.truth(k, p)
            if isinstance(t, bool): fixed[p] = t
            else: by_field[t[0].ranges].append((p, t[0], t[1]))
        axes = []
        for group in by_field.values():
            f = group[0][1]
            if f.width > 16:  # too wide to enumerate: nest on the single raw value that makes it true
                (p, _, test), = group
                raw = getattr(test, "want", None)
                pos = eq(self, f, raw) if raw is not None and test(raw) else [[]]
                axes.append([({p: False}, []), ({p: True}, pos)])
                continue
            cells = defaultdict(set)
            for r in range(1 << f.width):
                tv = tuple(test(r) for _, _, test in group)
                if None not in tv: cells[tv].add(r)
            if len(group) == 1:
                (p, _, _), = group
                axes.append([({p: False}, []), ({p: True}, member(self, f, cells.get((True,), set())))])
            else:
                axes.append([(dict(zip((g[0] for g in group), tv)), member(self, f, rs)) for tv, rs in cells.items()])
        for choice in itertools.product(*axes):
            combo, clauses = dict(fixed), []
            for c, cl in choice: combo |= c; clauses += cl
            if any(not cl for cl in clauses): continue
            yield combo, clauses

    def render_combo(self, k, combo, pseudo):
        class Sym:
            def value(_, a, item):
                if a.name in pseudo: return (pseudo[a.name] or "", pseudo[a.name] is None)
                return PH.format(a.name), combo.get(("default", a), False)
            def flag(_, name, attr, item): return combo.get(("flag", name, attr), False)
            def raw(_, name, item): return 0 if combo.get(("raw", name), False) else 1
            def target(_, a, off): return off
        return pydecode.render_items(k, Sym())

    def display(self, k, template, symbols):
        """SLEIGH display + refs for a rendered template with placeholders."""
        parts, refs = [], []
        for i, chunk in enumerate(template.split("\x00")):
            if i % 2 == 0:
                if chunk: parts.append(f'"{chunk}"')
            else:
                sym, r = symbols[chunk]
                if sym: parts.append(sym)
                refs += r
        return "^".join(parts) or '""', refs

    def build(self, k, pseudo=None, extra=()):
        arch = self.arch
        pseudo = pseudo or {}
        atoms = {a.name: a for a in k.format if a.kind in ("operand", "mod", "guard")}
        symbols = {n: self.vsym(k, a, a.kind == "mod") for n, a in atoms.items()
                   if a.kind != "guard" and n not in pseudo and a.type != "EXP_DESC"}
        preds, desc = self.predicates(k)
        # mnemonic: a mod placeholder `.{m}` displays its own dot
        _, items = self.render_combo(k, {}, pseudo)
        mnem = items[0]
        mnem_disp, refs = self.display(k, re.sub(r"\.\x00([^\x00]+)\x00", "\x00\\1\x00", mnem), symbols)
        # operand items
        n = len(items) - 1
        shapes = []
        for i in range(1, n + 1):
            ps = preds.get(i, []) + (desc if i == desc_item(k) else [])
            cons = []
            for combo, clauses in self.shape_cases(k, ps):
                tmpl = self.render_combo(k, combo, pseudo)[1][i]
                tmpl = re.sub(r"\.\x00([^\x00]+)\x00", "\x00\\1\x00", tmpl)
                disp, r = self.display(k, tmpl, symbols)
                pat = pattern(clauses, r)
                if pat: cons.append((tmpl == "", disp, pat))
            shapes.append(cons)
        fixed = next((i for i, c in enumerate(shapes) if c and not any(e for e, _, _ in c)), None)
        item_syms = []
        for i, cons in enumerate(shapes):
            sep_after, sep_before = fixed is not None and i < fixed, fixed is None and i > 0 or fixed is not None and i > fixed
            out = []
            for empty, disp, pat in cons:
                if not empty and sep_after: disp = f'{disp}^", "'
                if not empty and sep_before: disp = f'", "^{disp}'
                out.append(('""' if empty else disp, pat, ""))
            item_syms.append(self.subtable("o", out))
        clauses = self.class_constraints(k) + list(extra)
        g = self.guard(k)
        disp = (f"^{g}^" if g else "") + mnem_disp
        if item_syms: disp += " " + "^".join(item_syms)
        sem, srefs = self.semantics(k, g)
        pat = pattern(clauses, refs + srefs)
        if pat is None: raise NoMatch("unsatisfiable")
        subs = [s for s in [g] + item_syms if s]
        return f":{disp} is {pat}" + "".join(f" & {s}" for s in subs) + f" {{ {sem} }}", pattern(clauses)

    # ---------- semantics ----------

    def regfield(self, tok, lo, hi, file, span):
        """Field alias attached to register views, with sinks for bank overflow."""
        name = self.field(tok, lo, hi, f"_{file}{span}")
        self.spans[file].add(span)
        if name not in self.attach_vars:
            n = REG_FILES[file][2]
            self.attach_vars[name] = [reg_name(file, i, span) if i + span <= n else f"SINK{REG_FILES[file][1] * span}"
                                      for i in range(1 << (hi - lo + 1))]
        return name

    def regsem(self, f, file, span, write, values=None):
        """Invisible subtable exporting operand register(s); RZ/URZ read as 0 (PT/UPT as 1), writes are dropped."""
        _, size, n, zero, _ = REG_FILES[file]
        nbytes = size * span
        self.spans[file].add(span)
        special = (f"export SINK{nbytes};" if write
                   else f"ZERO{nbytes} = zext(0:8); export ZERO{nbytes};" if nbytes > 256  # SLEIGH temporary-size cap
                   else f"local zero:{nbytes} = zext(0:8); export zero;" if nbytes > 8
                   else f"export {1 if file in ('P', 'UP') else 0}:{nbytes};")
        if values is not None:
            cons = []
            for raw, index in values.items():
                body = special if index == zero else f"export {reg_name(file, index, span) if 0 <= index and index + span <= n else f'SINK{nbytes}'};"
                cons.append(('""', pattern(eq(self, f, raw)), "", body))
            return self.subtable("r", cons)
        (tok, lo, hi, _), = pieces(f)
        var = self.regfield(tok, lo, hi, file, span)
        cons = [('""', pattern([], [(tok, var)]), "", f"export {var};"),
                ('""', pattern(eq(self, f, zero)), "", special)]
        return self.subtable("r", cons)

    def special_name(self, i, span):
        name = self.arch.rev_enums["SpecialRegister"][i]
        return name if span == 1 else f"{name}_{32 * span}"

    def specialsem(self, f, nbytes):
        """Invisible subtable exporting the special register view f selects; SRZ reads as 0."""
        span, count = nbytes // 4, len(self.arch.rev_enums["SpecialRegister"])
        self.special_spans.add(span)
        (tok, lo, hi, _), = pieces(f)
        var = self.field(tok, lo, hi, f"_SR{span}")
        self.attach_vars[var] = [self.special_name(i, span) if i + span <= count else "_" for i in range(1 << (hi - lo + 1))]
        return self.subtable("r", [('""', pattern([], [(tok, var)]), "", f"export {var};"),
                                   ('""', pattern(eq(self, f, self.arch.enums["SpecialRegister"]["SRZ"])), "", f"export 0:{nbytes};")])

    def special_defs(self):
        count = len(self.arch.rev_enums.get("SpecialRegister", ()))
        return [f"define register offset={SPECIAL_BASE + 4 * phase:#x} size={4 * span} [ "
                + " ".join(self.special_name(i, span) for i in range(phase, count - span + 1, span)) + " ];"
                for span in sorted(self.special_spans) for phase in range(span)]

    def dynregsem(self, k, name, f, file, mapping):
        """Read subtable choosing this operand's view from its own span fields, widened to its widest view."""
        variants = self.span_variants(k, {name})
        nbytes = REG_FILES[file][1] * max(spans[name] for spans, _ in variants)
        # Wider views exceed SLEIGH's temporary cap; one internal register per
        # operand name keeps several wide inputs of one instruction distinct.
        if nbytes > 256:
            t = f"DYN_{name}_{nbytes}"; self.temporaries[t] = nbytes
            widen = lambda v: f"{t} = zext({v}); export {t};"
        else: widen = lambda v: f"local t:{nbytes} = zext({v}); export t;"
        cons = []
        for spans, clauses in variants:
            span = spans[name]
            size = REG_FILES[file][1] * span
            if not span: body, refs = (widen("0:8") if nbytes > 8 else f"export 0:{nbytes};"), []
            else:
                sym = self.regsem(f, file, span, False, mapping)
                body, refs = (f"export {sym};" if size == nbytes else widen(sym)), [("sub", sym)]
            cons.append(('""', pattern(clauses, refs), "", body))
        return self.subtable("r", cons), nbytes

    def reg_operands(self, k):
        """(record, register file, source field) for register operands with md roles."""
        gname = next((a.name for a in k.format if a.kind == "guard"), None)
        for o in self.roles.get(k.name, {}).get("operands", []):
            file, src = RESOURCE_FILE.get(o["resource"]), self.source(k, o["name"])
            if (o["name"] != gname and file and ENUM_FILE.get(o["type"]) == file
                    and o["role"] and src):
                yield o, file, src[1]

    def dynamic_inputs(self, k):
        """Expression-sized read-only operands of opaque classes, sized by their own subtables."""
        from sass.operations import REGISTRY
        if k.mnemonic in REGISTRY: return set()
        return {o["name"] for o, _, _ in self.reg_operands(k) if isinstance(o["span"], str) and o["role"] == ["read"]}

    def span_variants(self, k, names=None):
        """(fixed spans for expression-sized operands, clauses) per distinct span signature."""
        names = names or {o["name"] for o, _, _ in self.reg_operands(k)} - self.dynamic_inputs(k)
        exprs = {o["name"]: o["span"] for o, _, _ in self.reg_operands(k) if isinstance(o["span"], str) and o["name"] in names}
        if not exprs: return [({}, [])]
        fields = {}
        for e in exprs.values():
            for v in sorted(mdexpr.free_vars(self.arch, e)):
                src = self.source(k, v)
                if not src or src[0] != "field":
                    raise ValueError(f"{k.name}: unsupported span dependency {v}")
                fields[v] = src
        count = math.prod(1 << src[1].width for src in fields.values())
        if count > 1 << 20:
            raise ValueError(f"{k.name}: span expression needs {count} modifier combinations")
        combos = itertools.product(*(range(1 << src[1].width) for src in fields.values()))
        groups = defaultdict(list)
        for combo in combos:
            vals = {v: x * src[2] for (v, src), x in zip(fields.items(), combo)}
            sig = tuple((n, int(mdexpr.evaluate(e, self.arch, vals))) for n, e in exprs.items())
            if any(s < 0 or s > 256 for _, s in sig):
                raise ValueError(f"{k.name}: unsupported register spans {sig}")
            groups[sig].append(combo)
        if len(groups) == 1: return [(dict(next(iter(groups))), [])]
        out = []
        for sig, cs in groups.items():
            if len(fields) == 1:
                ((_, (_, f, _)),) = fields.items()
                clauses = member(self, f, {c[0] for c in cs})
            else:
                proj = [set(c[i] for c in cs) for i in range(len(fields))]
                if len(cs) == math.prod(map(len, proj)):  # a product of per-field sets
                    clauses = [cl for (_, src), vs in zip(fields.items(), proj) for cl in member(self, src[1], vs)]
                else:
                    # Partition into exact Cartesian rectangles, merging values
                    # whose remaining modifier combinations are identical.
                    for box in span_boxes(cs):
                        clauses = [cl for (_, src), vs in zip(fields.items(), box)
                                   for cl in member(self, src[1], vs)]
                        out.append((dict(sig), clauses))
                    continue
            out.append((dict(sig), clauses))
        return out

    def semantics(self, k, guard):
        # Keep span decisions under their class rather than duplicating root
        # constructors: otherwise SLEIGH mixes modifiers from unrelated opcodes
        # into the global instruction decision tree.
        variants = self.span_variants(k)
        if len(variants) == 1:
            return self.fixed_semantics(k, guard, variants[0][0])
        cons = []
        for fixed, clauses in variants:
            body, refs = self.fixed_semantics(k, None, fixed, nested=True)
            cons.append(('""', pattern(clauses, refs), "", body))
        table = self.subtable("s", cons)
        body = f"build {guard}; " if guard else ""
        return body + f"build {table};", [("sub", table)]

    def fixed_semantics(self, k, guard, spans, nested=False):
        from sass.semantic import emit
        if nested:return emit(self, k, guard, spans)
        body, refs = emit(self, k, None, spans)
        table = self.subtable("s", [('""', pattern([], refs), "", body)])
        return (f"build {guard}; " if guard else "") + f"build {table};", [("sub", table)]

    def guard(self, k):
        g = next((a for a in k.format if a.kind == "guard"), None)
        if g is None: return None
        src = self.source(k, g.name)
        negsrc = self.attr_source(k, g.name, "not")
        if src is None: return None
        _, f, _ = src
        (tok, lo, hi, _), = pieces(f)
        names = [value_text(self.arch, g, {g.name: v}, {})[0] for v in range(1 << f.width)]
        fname = self.name_field(tok, lo, hi, names)
        dv = self.arch.enums[g.type][g.default]
        neg = negsrc[1] if negsrc else None
        pvar = self.regfield(tok, lo, hi, ENUM_FILE[g.type], 1)
        always = eq(self, f, dv)
        cons = [('""', pattern(always + (eq(self, neg, 0) if neg else [])), "", "")]
        cons.append((f'"@"^{fname}^":"', pattern(eq(self, neg, 0) if neg else [], [(tok, fname), (tok, pvar)]), "",
                     f"if ({pvar} == 0) goto inst_next;"))
        if neg:
            cons.append((f'"@!"^{fname}^":"', pattern(always + eq(self, neg, 1), [(tok, fname)]), "", "goto inst_next;"))
            cons.append((f'"@!"^{fname}^":"', pattern(eq(self, neg, 1), [(tok, fname), (tok, pvar)]), "",
                         f"if ({pvar} != 0) goto inst_next;"))
        return self.subtable("g", cons)

    def class_constraints(self, k):
        cl = []
        for f, r in k.rules:
            match r:
                case ("opcode",): cl += eq(self, f, k.opcode)
                case ("const", v): cl += eq(self, f, v)
        for name in k.strict:
            a = k.operand_types.get(name)
            if a is None or a.type not in self.arch.enums: continue
            valid = set(self.arch.rev_enums[a.type])
            c = self.values_where(k, name, lambda x: x in valid)
            if c: cl += c
        return cl

    def sched_invalid(self, k):
        """Clause lists, one per table field, matching rows whose `$( ... )$` scheduling operands are not valid
        enum values. nvdisasm then prints the plain class instead of the pseudo-op."""
        out = []
        for f, r in k.rules:
            if r[0] != "table" or r[1] not in self.arch.tables: continue
            idx = [i for i, a in enumerate(r[2]) if a[0] != "const" and pydecode.key(a) in k.sched
                   and k.sched[pydecode.key(a)] in self.arch.enums]
            rows = pydecode.table_inverse(self.arch, r[1])
            bad = {v for v in rows if not all(rows[v][0][i] in self.arch.rev_enums[k.sched[pydecode.key(r[2][i])]] for i in idx)}
            if bad: out.append(member(self, f, bad))
        return out

    def pseudo_variants(self, k):
        """(alias values, extra clauses) per GetPseudoOp* row, in table order."""
        (name, etype, table, args), = k.aliases
        out = []
        for ins, outv in self.arch.tables[table]:
            val = resolve(self.arch, outv)
            clauses = []
            for t, argname in zip(ins, args):
                if t == "-": continue
                want = resolve(self.arch, t)
                a = k.operand_types.get(argname)
                src = self.source(k, argname)
                if src is None or src[0] != "field": clauses = None; break
                _, f, scale = src
                raw, n = want // scale, f.width
                if a is not None and a.type == "SImm" and not -(1 << n - 1) <= raw < 1 << n - 1:
                    clauses = None; break  # pydecode compares signed: e.g. 2147483648 never equals SImm(32)
                clauses += eq(self, f, raw & ((1 << n) - 1))
            if clauses is None or val == 0: continue
            label = self.arch.rev_enums[etype][val]
            out.append(({name: label}, clauses))
        # blockers first: on invalid scheduling operands the first-matching constructor shows no pseudo-op
        return [({name: None}, c) for c in self.sched_invalid(k)] + out

    def generate(self):
        lines, seen = [], set()
        for k in self.arch.classes:
            variants = self.pseudo_variants(k) if k.alternate and k.aliases else [] if k.alternate else [(None, [])]
            for pseudo, extra in variants:
                try: line, key = self.build(k, pseudo, extra)
                except NoMatch as e: self.skipped.append((k.name, str(e))); continue
                if key in seen: self.skipped.append((k.name, "same constraints as an earlier constructor")); continue
                seen.add(key); lines.append(line)
        # pseudo-op variants first so they win over their plain class (they are special cases anyway)
        return lines

    def slaspec(self):
        cons = self.generate()
        # Decode-time specialization leaves some eagerly registered value tables
        # unused. Emit only the graph reachable from instruction constructors.
        tables=defaultdict(list)
        for line in self.table_lines:
            tables[line.split(':',1)[0]].append(line)
        live=set();pending=set(re.findall(r'\b\w+\b',' '.join(cons))) & tables.keys()
        while pending:
            name=pending.pop()
            if name in live:continue
            live.add(name)
            pending.update(set(re.findall(r'\b\w+\b',' '.join(tables[name]))) & tables.keys() - live)
        table_lines=[line for line in self.table_lines if line.split(':',1)[0] in live]
        toks = defaultdict(set)
        for (tok, lo, hi, kind), name in self.fields.items(): toks[tok].add((lo, hi, kind, name))
        out = [f"# Generated by sass/gen_sleigh.py from out/{self.arch.name}/md -- do not edit.",
               '@include "sass_common.sinc"', ""] + register_defs(self.spans) + self.special_defs() + [
                   f'define register offset={0x1000000+i*0x10000:#x} size={size} [ {name} ];'
                   for i,(name,size) in enumerate(sorted(self.temporaries.items()))] + [""]
        for tok, tname in (("l", "lo"), ("h", "hi")):
            out.append(f"define token {tname}(64)")
            out.append(f"  {tok}any = (0, 63)")
            for lo, hi, kind, name in sorted(toks[tok]):
                out.append(f"  {name} = ({lo}, {hi}){' signed' if kind == 's' else ''}")
            out.append(";")
        for fname, names in self.attach.items():
            out.append(f"attach names [ {fname} ] [ " + " ".join(f'"{n}"' for n in names) + " ];")
        for fname, names in self.attach_vars.items():
            out.append(f"attach variables [ {fname} ] [ " + " ".join(names) + " ];")
        out += [f"define pcodeop {op};" for op in sorted(self.pcodeops)]
        out += [""] + table_lines + [""] + cons
        return "\n".join(out) + "\n"

def reg_name(file, i, span):
    base = REG_FILES[file][4] if i == REG_FILES[file][3] else f"{file}{i}"
    return base if span == 1 else f"{file}{i}_{REG_FILES[file][1] * 8 * span}"

def register_defs(spans=None):
    """Each file as single registers plus overlapping 2/4/8-register views starting at every index."""
    spans = spans or {file: set(SPANS if size == 4 else (1,)) for file, (_, size, *_) in REG_FILES.items()}
    out = []
    for file, (base, size, n, _, _) in REG_FILES.items():
        for span in sorted(spans[file]):
            for phase in range(span):
                names = [reg_name(file, i, span) for i in range(phase, n - span + 1, span)]
                if names: out.append(f"define register offset={base + size * phase:#x} size={size * span} [ {' '.join(names)} ];")
    sizes = sorted({REG_FILES[file][1] * s for file, ss in spans.items() for s in ss})
    out += [f"define register offset=0x10000 size={n} [ SINK{n} ];" for n in sizes]
    out += [f"define register offset={0x20000 + n * 0x1000} size={n} [ ZERO{n} ];"
            for n in sizes if n > 256]
    return out

def can_default(arch, a):
    return default_value(arch, a) is not None

def default_value(arch, a):
    if a.type in arch.enums:
        return arch.enums[a.type].get(a.default) if a.default is not None else None
    if a.type == "BITSET": return int(a.args.partition("/")[2] or "0", 0)
    if IMM.match(a.type):
        _, dflt, *opts = a.args.split("/") + [""]
        if dflt == "" or dflt.endswith("*") or "PRINT" in opts: return None
        return int(dflt, 0)
    return None

def desc_item(k):
    item = 0
    for a in k.format:
        if a.kind == "lit" and a.name == ",": item += 1
        elif a.kind in ("operand", "open") and item == 0: item = 1
        if a.kind == "operand" and a.type == "DESC": return item
    return None

LDEFS = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by sass/gen_sleigh.py -->
<language_definitions>
{}</language_definitions>
"""
LANG = """  <language processor="SASS" endian="little" size="64" variant="{v}" version="1.1"
            slafile="sass_{v}.sla" processorspec="sass.pspec" id="SASS:LE:64:{v}">
    <description>NVIDIA SASS {V} (from nvdisasm machine description)</description>
    <compiler name="default" spec="sass.cspec" id="default"/>
  </language>
"""

OPINION = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by sass/gen_sleigh.py. Cubins are ELF e_machine 190 (EM_CUDA); the SM number is in e_flags,
     bits 0-7 in CUDA ELF ABI v7 and bits 8-15 in v8. The other layout never holds a valid SM there. -->
<opinions>
  <constraint loader="Executable and Linking Format (ELF)" compilerSpecID="default">
{}  </constraint>
</opinions>
"""

def sm_bits(sm, shift):
    bits = ["."] * 32
    for i in range(8): bits[31 - shift - i] = str(sm >> i & 1)
    return "0b " + " ".join("".join(bits[j:j + 4]) for j in range(0, 32, 4))

def write_ldefs():
    variants = sorted(p.stem.removeprefix("sass_") for p in OUT.glob("sass_sm*.slaspec"))
    (OUT / "sass.ldefs").write_text(LDEFS.format("".join(LANG.format(v=v, V=v.upper()) for v in variants)))
    (OUT / "sass.opinion").write_text(OPINION.format("".join(
        f'    <constraint primary="190" processor="SASS" endian="little" size="64" variant="{v}" secondary="{sm_bits(int(v[2:]), shift)}"/>\n'
        for v in variants for shift in (0, 8))))

def main(archs):
    for arch in archs:
        g = Gen(arch)
        text = g.slaspec()
        (OUT / f"sass_{arch.lower()}.slaspec").write_text(text)
        (OUT / f"sass_{arch.lower()}_coverage.json").write_text(json.dumps(list(g.coverage.values()), indent=2) + "\n")
        print(f"{arch}: {text.count(chr(10))} lines, {len(g.tables)} subtables, {len(g.skipped)} skipped")
        for name, why in g.skipped[:10]: print("   skipped", name, why)
    write_ldefs()

if __name__ == "__main__":
    main(sys.argv[1:])
