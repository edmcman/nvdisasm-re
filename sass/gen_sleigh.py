"""Generate a SLEIGH decoder from the md decode IR.

Display reuses pydecode.render_items() symbolically: for every FORMAT item, each combination of the
conditions that change its shape (operand at default, flag set, ...) is rendered once with placeholder
values and becomes one subtable constructor guarded by those conditions on the encoding.

usage: python3 -m sass.gen_sleigh SM89 [SM90 ...]
"""
import itertools, re, sys
from collections import defaultdict
from pathlib import Path
from sass import ir, pydecode
from sass.pydecode import IMM, PREFIX, NoMatch, resolve, sext, value_text

OUT = Path(__file__).resolve().parent.parent / "processor" / "SASS" / "data" / "languages"
MAX_ENUM_BITS = 12
PH = "\x00{}\x00"  # placeholder for a value symbol inside a rendered template

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
        name = gen.field(tok, lo, hi)
        missing = sorted(set(range(universe)) - values)
        runs = intervals(sorted(values))
        if len(missing) <= len(runs) and hi - lo < 8:  # `!=` splits into many states on wide fields
            return [[lit(tok, f"{name}!={v}")] for v in missing]
        return [[lit(tok, f"{name}={a}" if a == b else f"({name}>{a - 1} & {name}<{b + 1})" if a else f"{name}<{b + 1}")
                 for a, b in runs]]
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
        self.tables = {}              # constructor text -> subtable name
        self.table_lines = []
        self.skipped = []

    def field(self, tok, lo, hi, kind=""):
        name = f"{tok}{lo}_{hi}{kind}"
        self.fields[(tok, lo, hi, kind)] = name
        return name

    def subtable(self, prefix, constructors):
        """Dedupe identical subtables; constructors are (display, pattern, action) triples."""
        key = (prefix, tuple(constructors))
        if key not in self.tables:
            name = f"{prefix}{len(self.tables)}"
            self.tables[key] = name
            for disp, pat, action in constructors:
                self.table_lines.append(f"{name}: {disp} is {pat}{action} {{ }}")
        return self.tables[key]

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
                    fname = self.field(tok, lo, hi, f"_{len(self.attach)}")
                    self.attach[fname] = names
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
        pat = pattern(clauses, refs)
        if pat is None: raise NoMatch("unsatisfiable")
        subs = [s for s in [g] + item_syms if s]
        return f":{disp} is {pat}" + "".join(f" & {s}" for s in subs) + " { }", pattern(clauses)

    def guard(self, k):
        g = next((a for a in k.format if a.kind == "guard"), None)
        if g is None: return None
        src = self.source(k, g.name)
        negsrc = self.attr_source(k, g.name, "not")
        if src is None: return None
        _, f, _ = src
        (tok, lo, hi, _), = pieces(f)
        names = [value_text(self.arch, g, {g.name: v}, {})[0] for v in range(1 << f.width)]
        fname = self.field(tok, lo, hi, f"_{len(self.attach)}")
        self.attach[fname] = names
        dv = self.arch.enums[g.type][g.default]
        neg = negsrc[1] if negsrc else None
        cons = [('""', pattern(eq(self, f, dv) + (eq(self, neg, 0) if neg else [])), "")]
        cons.append((f'"@"^{fname}^":"', pattern(eq(self, neg, 0) if neg else [], [(tok, fname)]), ""))
        if neg: cons.append((f'"@!"^{fname}^":"', pattern(eq(self, neg, 1), [(tok, fname)]), ""))
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
        toks = defaultdict(set)
        for (tok, lo, hi, kind), name in self.fields.items(): toks[tok].add((lo, hi, kind, name))
        out = [f"# Generated by sass/gen_sleigh.py from out/{self.arch.name}/md -- do not edit.",
               '@include "sass_common.sinc"', ""]
        for tok, tname in (("l", "lo"), ("h", "hi")):
            out.append(f"define token {tname}(64)")
            out.append(f"  {tok}any = (0, 63)")
            for lo, hi, kind, name in sorted(toks[tok]):
                out.append(f"  {name} = ({lo}, {hi}){' signed' if kind == 's' else ''}")
            out.append(";")
        for fname, names in self.attach.items():
            out.append(f"attach names [ {fname} ] [ " + " ".join(f'"{n}"' for n in names) + " ];")
        out += [""] + self.table_lines + [""] + cons
        return "\n".join(out) + "\n"

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
LANG = """  <language processor="SASS" endian="little" size="64" variant="{v}" version="1.0"
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
        print(f"{arch}: {text.count(chr(10))} lines, {len(g.tables)} subtables, {len(g.skipped)} skipped")
        for name, why in g.skipped[:10]: print("   skipped", name, why)
    write_ldefs()

if __name__ == "__main__":
    main(sys.argv[1:])
