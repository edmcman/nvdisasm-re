"""Parsers for nvdisasm machine descriptions: classes, enums (REGISTERS) and TABLES."""
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
SECTIONS = ("FORMAT_ALIAS", "FORMAT", "CONDITIONS", "PROPERTIES", "PREDICATES", "OPCODES", "ENCODING")
SECTION = re.compile(rf"^({'|'.join(SECTIONS)})\b", re.M)
CLASS = re.compile(r'^(ALTERNATE\s+)?CLASS\s+"([^"]+)"(.*?)(?=^(?:ALTERNATE\s+)?CLASS\s+"|\Z)', re.S | re.M)
OPERAND = re.compile(r"(?<![/\w])(\w+)(?:\([^)]*\))?\*?:(\w+)")

def strip_comments(text): return re.sub(r"//[^\n]*", "", text)

def split_sections(body):
    parts = SECTION.split(body)
    return {k: v for k, v in zip(parts[1::2], parts[2::2])}

def parse_md(text):
    classes = []
    for m in CLASS.finditer(text):
        alt, name, body = m.groups()
        sec = split_sections(body)
        opcodes = re.findall(r"^\s*([\w.]+)\s*=\s*(0b[01]+)\s*;", sec.get("OPCODES", ""), re.M)
        classes.append(dict(
            cls=name, alternate=bool(alt), text=m.group(0), sections=sec,
            operands=[(t, n) for t, n in OPERAND.findall(sec.get("FORMAT", "")) if t not in {"PREDICATE"}],
            preds={k: int(v) if re.fullmatch(r"-?\d+", v) else v
                   for k, v in re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*;\s*$", sec.get("PREDICATES", ""), re.M)},
            props=dict(re.findall(r"(\w+)\s*=\s*([^;]+?)\s*;", sec.get("PROPERTIES", ""))),
            opcodes=[n for n, _ in opcodes],
            opcode_values={n: int(v, 2) for n, v in opcodes}))
    return classes

def preamble(archdir):
    return "".join(p.read_text() for p in sorted((archdir / "md").glob("_[0-9][0-9]_*.txt")))

def between(text, start, end):
    """Section body from header line `start` up to header line `end` (preamble splitting is not reliable)."""
    return re.search(rf"^{start}\s*$(.*?)^{end}\s*$", text, re.S | re.M)[1]

def load_classes(archdir):
    files = [p for p in sorted((archdir / "md").glob("*.txt")) if not re.match(r"_\d\d_", p.name)]
    return [c for p in files for c in parse_md(p.read_text())]

# ---------- REGISTERS: enums ----------

ENUM_ITEM = re.compile(r"""
    (?P<rname>[\w.]+)\((?P<lo>\d+)\.\.(?P<hi>\d+)\)(?:\s*=\s*\((?P<vlo>\d+)\.\.(?P<vhi>\d+)\))?
  | (?:"(?P<qname>[^"]*)"|(?P<name>[\w.]+)\s*\*?)(?:\s*=\s*(?P<val>-?\w+))?
""", re.X)

def parse_enum_items(body):
    items, nxt = [], 0
    for item in filter(None, (s.strip() for s in body.split(","))):
        m = ENUM_ITEM.fullmatch(item)
        if not m: raise ValueError(f"bad enum item {item!r}")
        g = m.groupdict()
        match g:
            case {"rname": str(n), "lo": lo, "hi": hi, "vlo": vlo}:
                base = int(vlo) if vlo else int(lo)
                pairs = [(f"{n}{i}", base + i - int(lo)) for i in range(int(lo), int(hi) + 1)]
            case {"val": v}: pairs = [(g["qname"] if g["qname"] is not None else g["name"], nxt if v is None else int(v, 0))]
        items += pairs
        nxt = pairs[-1][1] + 1
    return items

def parse_enums(text):
    """Enum name -> [(member, value), ...] in definition order; names and values may repeat."""
    text = strip_comments(text)
    enums, unions = {}, {}
    for stmt in filter(None, (s.strip() for s in text.split(";"))):
        name, rest = re.match(r"([\w.]+)\s*(.*)", stmt, re.S).groups()
        if rest.startswith("="): unions[name] = [s.strip() for s in rest[1:].split("+")]
        else: enums[name] = parse_enum_items(rest)
    def resolve(n):
        if n not in enums: enums[n] = [kv for part in unions[n] for kv in resolve(part)]
        return enums[n]
    for n in unions: resolve(n)
    return enums

# ---------- TABLES ----------

def parse_tables(text):
    tables, cur = {}, None
    for line in strip_comments(text).splitlines():
        line = line.strip()
        match line.split("->"):
            case [""] | [";"]: continue
            case [name]: cur = tables.setdefault(name, [])
            case [lhs, rhs]: cur.append((tuple(re.findall(r"'[^']*'|\S+", lhs)), literal(rhs.strip().rstrip(";").strip())))
    return tables

def literal(tok):
    try: return int(tok.replace("_", ""), 0)
    except ValueError: return tok

def load_arch(arch):
    d = OUT / arch
    pre = preamble(d)
    return dict(arch=arch, classes=load_classes(d),
                enums=parse_enums(between(pre, "REGISTERS", "TABLES")),
                tables=parse_tables(between(pre, "TABLES", "OPERATION PROPERTIES")))
