"""Decode IR built from a machine description: per-class bit fields, encoding rules and FORMAT."""
import re
from dataclasses import dataclass, field
from functools import cache, cached_property
import mdlib

# ---------- ENCODING ----------

@dataclass(frozen=True)
class Field:
    name: str
    ranges: tuple  # ((hi, lo), ...), most significant first

    @property
    def width(self): return sum(hi - lo + 1 for hi, lo in self.ranges)

    @property
    def mask(self): return sum(((1 << (hi - lo + 1)) - 1) << lo for hi, lo in self.ranges)

    def get(self, w):
        v = 0
        for hi, lo in self.ranges: v = v << (hi - lo + 1) | (w >> lo) & ((1 << (hi - lo + 1)) - 1)
        return v

    def put(self, v):
        w = 0
        for hi, lo in reversed(self.ranges):
            n = hi - lo + 1
            w |= (v & ((1 << n) - 1)) << lo; v >>= n
        return w

def parse_field(lhs):
    nums = lhs.removeprefix("BITS_").split("_")
    width, rest, ranges, acc = int(nums[0]), nums[1:], [], 0
    while acc < width:
        hi, lo, rest = int(rest[0]), int(rest[1]), rest[2:]
        ranges.append((hi, lo)); acc += hi - lo + 1
    assert acc == width, lhs
    return Field("_".join(rest), tuple(ranges))

# Rule kinds: ("opcode",) ("const", n) ("ignore",) ("op", name, scale, fmt) ("attr", name, attr) ("table", tname, args)
# Multi-field lines become one ("op", arg, scale) rule per field, e.g. ConstBankAddress2 stores offset/4.
BANK_SCALE = {"ConstBankAddress0": 1, "ConstBankAddress2": 4}
ARG = re.compile(r"\s*([\w.]+)(?:@(\w+))?\s*$")

def parse_arg(a):
    a = a.strip()
    if re.fullmatch(r"-?(0x[0-9a-fA-F]+|0b[01_]+|\d+)", a): return ("const", mdlib.literal(a))
    name, attr = ARG.match(a).groups()
    return ("attr", name, attr) if attr else ("op", name, 1, None)

def parse_rule(rhs):
    rhs = rhs.strip()
    match rhs:
        case "Opcode": return ("opcode",)
        case _ if rhs.startswith("*") and rhs[1:].strip()[:1].isdigit(): return ("ignore",)  # `*T(..)` still decodes
        case _ if re.fullmatch(r"-?(0x[0-9a-fA-F]+|0b[01_]+|\d+)", rhs): return ("const", mdlib.literal(rhs))
    star = rhs.startswith("*")
    rhs = rhs.lstrip("*").strip()
    if m := re.fullmatch(r"(\w+)\((.*)\)", rhs, re.S):
        return ("table", m[1], tuple(parse_arg(a) for a in m[2].split(",")))
    if m := re.fullmatch(r"([\w.]+)\s+convertFloatType\((.*)\)", rhs, re.S):
        return ("op", m[1], 1, m[2])
    if m := re.fullmatch(r"([\w.]+)(?:\s+MULTIPLY\s+(\d+))?(?:\s+SCALE\s+(\d+))?", rhs):
        mul, scale = int(m[2] or 1), int(m[3] or 1)
        return ("op", m[1], scale // mul if scale >= mul else 1, None)
    if m := re.fullmatch(r"([\w.]+)@(\w+)", rhs): return ("attr", m[1], m[2])
    raise ValueError(f"unknown encoding rhs {rhs!r}")

def parse_encoding(text):
    rules = []
    for stmt in filter(None, (s.strip() for s in mdlib.strip_comments(text).split(";"))):
        if stmt.startswith("!") or not stmt.startswith("BITS_"): continue
        lhs, rhs = stmt.split("=", 1)
        fields = [parse_field(f.strip()) for f in lhs.split(",")]
        if len(fields) > 1:
            fn, args = re.fullmatch(r"\s*(\w+)\((.*)\)\s*", rhs, re.S).groups()
            names = [a.strip() for a in args.split(",")]
            scales = [1] * (len(names) - 1) + [BANK_SCALE[fn]]
            rules += [(f, ("op", n, sc, None)) for f, n, sc in zip(fields, names, scales)]
        else:
            rules.append((fields[0], parse_rule(rhs)))
    return rules

def strict_operands(text):
    """Operands of `field=*X` and `field=*T(a, b, ..)`: their values must be valid enum members."""
    text = mdlib.strip_comments(text)
    direct = re.findall(r"^BITS_[\w.]+\s*=\s*\*\s*([A-Za-z_][\w.]*)\s*;", text, re.M)
    tabled = [a.strip().split("@")[0] for args in re.findall(r"^BITS_[\w.]+\s*=\s*\*\s*\w+\(([^)]*)\)\s*;", text, re.M)
              for a in args.split(",")]
    return frozenset(direct + [a for a in tabled if not a[:1].isdigit()])

# ---------- FORMAT ----------

FLAGS = {"[!]": "not", "[-]": "negate", "[~]": "invert", "[||]": "absolute"}
TOK = re.compile(r"""
   (?P<sched>\$\(.*?\)\$)
 | (?P<lit>'[^']*')
 | (?P<flag>\[(?:!|-|~|\|\|)\])
 | /(?P<mtype>[\w.]+)(?:\((?P<margs>[^)]*)\))?[*@]?:(?P<mname>[\w.]+)
 | (?P<otype>[\w.]+)(?:\((?P<oargs>[^)]*)\))?[*@]?:(?P<oname>[\w.]+)
 | (?P<punct>[\[\]{}+@;*])
 | (?P<word>\w+)
""", re.X | re.S)

@dataclass(frozen=True)
class Atom:
    kind: str           # guard opcode mod operand lit flag open close plus
    type: str = ""
    name: str = ""
    default: str | None = None
    args: str = ""

def parse_default(args):
    if args is None: return None, ""
    m = re.fullmatch(r'\s*"([^"]*)"\s*', args) or re.fullmatch(r"\s*([A-Za-z_]\w*)\s*", args)
    return (m[1], "") if m else (None, args.strip())

def parse_format(text):
    atoms, guard_next = [], False
    for m in TOK.finditer(text):
        g = {k: v for k, v in m.groupdict().items() if v is not None}
        match g:
            case {"sched": _}: pass
            case {"word": "PREDICATE"}: guard_next = True
            case {"word": "Opcode"}: atoms.append(Atom("opcode"))
            case {"punct": "@"} | {"punct": ";"} | {"punct": "*"}: pass
            case {"punct": "{"}: atoms.append(Atom("lbrace"))
            case {"punct": "}"}: atoms.append(Atom("rbrace"))
            case {"punct": "["}: atoms.append(Atom("open"))
            case {"punct": "]"}: atoms.append(Atom("close"))
            case {"punct": "+"}: atoms.append(Atom("plus"))
            case {"lit": lit}: atoms.append(Atom("lit", name=lit[1:-1]))
            case {"flag": f}: atoms.append(Atom("flag", name=FLAGS[f]))
            case {"mtype": t, "mname": n}:
                d, a = parse_default(g.get("margs")); atoms.append(Atom("mod", t, n, d, a))
            case {"otype": t, "oname": n}:
                d, a = parse_default(g.get("oargs"))
                atoms.append(Atom("guard" if guard_next else "operand", t, n, d, a)); guard_next = False
            case {"word": w}: atoms.append(Atom("word", name=w))
    return atoms

# ---------- class IR ----------

@dataclass
class Klass:
    name: str
    alternate: bool
    mnemonic: str
    opcode: int
    rules: list
    format: list
    order: int
    constraint_mask: int = 0
    constraint_bits: int = 0
    operand_types: dict = field(default_factory=dict)
    aliases: list = field(default_factory=list)  # (name, enum type, pattern table, arg names)
    sched: dict = field(default_factory=dict)    # typed operands of $( ... )$ scheduling groups: name -> type
    strict: frozenset = frozenset()              # `field=*X`: X must be a valid enum value or the class does not match

def build_class(c, order):
    rules = parse_encoding(c["sections"]["ENCODING"])
    base = [n for n in c["opcodes"] if not n.endswith("_pipe")] or c["opcodes"]
    opcode = c["opcode_values"][base[0]]
    mask = bits = 0
    for f, r in rules:
        match r:
            case ("opcode",): mask |= f.mask; bits |= f.put(opcode)
            case ("const", n): mask |= f.mask; bits |= f.put(n)
    fmt = parse_format(c["sections"]["FORMAT"])
    aliases = [(n, t, f, tuple(a.strip() for a in args.split(",")))
               for n, t, f, args in re.findall(r"(\w+)\s*=\s*(\w+):(\w+)\(([^)]*)\)", c["sections"].get("FORMAT_ALIAS", ""))]
    sched = {n: t for g in re.findall(r"\$\((.*?)\)\$", c["sections"]["FORMAT"], re.S)
             for t, n in re.findall(r"(\w+)(?:\([^)]*\))?:(\w+)", g)}
    return Klass(c["cls"], c["alternate"], base[0], opcode, rules, fmt, order, mask, bits,
                 {a.name: a for a in fmt if a.kind in ("operand", "guard", "mod")}, aliases, sched,
                 strict_operands(c["sections"]["ENCODING"]))

@dataclass
class Arch:
    name: str
    classes: list
    enum_defs: dict  # enum -> [(member, value)] in definition order
    tables: dict

    @cached_property
    def enums(self):  # member -> value of its first definition
        return {e: dict(reversed(defs)) for e, defs in self.enum_defs.items()}

    @cached_property
    def rev_enums(self):  # value -> name; the last definition wins (PT over P7, explicit SR138 over SR_Cga...)
        return {e: dict((v, n) for n, v in defs) for e, defs in self.enum_defs.items()}

@cache
def load(arch):
    md = mdlib.load_arch(arch)
    return Arch(arch, [build_class(c, i) for i, c in enumerate(md["classes"])], md["enums"], md["tables"])
