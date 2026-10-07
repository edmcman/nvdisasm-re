#!/usr/bin/env python3
"""Join md_SMxx + latencies_SMxx into per-class operand semantics (role, register span, pipe, latency)."""
import json, re, sys, pathlib
from collections import defaultdict

ENTRY = re.compile(r"(\w+)(?:\[[^\]]*\])?`\{([^}]*)\}|(\w+)")
ROLES = {"TRUE": ("read", "write"), "OUTPUT": ("write", "write"), "ANTI": ("write", "read")}  # (column, row)

def strip_comments(text): return re.sub(r"//[^\n]*", "", text)

# ---------- latencies ----------

def parse_set_exprs(text):
    sets = {}
    for section in re.findall(r"OPERATION SETS(.*?)(?=^\S)", text, re.S | re.M):
        for name, expr in re.findall(r"(\w+)\s*=\s*(.*?);", section, re.S):
            sets[name] = expr
    return sets

def eval_sets(exprs):
    memo = {}
    def ev(name):
        if name in memo: return memo[name]
        memo[name] = set()
        acc, op = set(), "+"
        for tok in re.findall(r"\{[^}]*\}|[+-]|\w+", exprs.get(name, "")):
            match tok:
                case "+" | "-": op = tok; continue
                case _ if tok.startswith("{"): val = {s.strip() for s in tok[1:-1].split(",") if s.strip()}
                case _: val = ev(tok)
            acc = acc | val if op == "+" else acc - val
        memo[name] = acc
        return acc
    return {n: ev(n) for n in exprs}

def parse_conns(body):
    return [(m[1], m[2]) for m in re.finditer(r"(\w+)(?:\s*\{[^}]*\})?\s*(?:@(\w+))?", body) if m[1]]

def parse_entries(text, connector_sets):
    out = []
    for m in ENTRY.finditer(text):
        match m.groups():
            case (opset, conns, None): out.append((opset, parse_conns(conns), opset))
            case (None, None, cset) if cset in connector_sets:
                out += [(o, c, cset) for o, c, _ in connector_sets[cset]]
    return out

def parse_latencies(text):
    text = strip_comments(text)
    ops = eval_sets(parse_set_exprs(text))
    resources = {}
    for section in re.findall(r"CONNECTOR NAMES?(.*?)(?=^\S)", text, re.S | re.M):
        for names, res in re.findall(r"([\w\s,{}().]+?):\s*(\w+)\s*;", section):
            for n in re.findall(r"(\w+)(?:\s*\{[^}]*\})?\s*(?:,|$)", names.strip()):
                resources[n] = res
    ranges = dict(re.findall(r"(\w+Range)\s*=.*?MD_PRED\((\w+)\)", text))
    connector_sets = {}
    for section in re.findall(r"CONNECTOR SETS(.*?)(?=^\S)", text, re.S | re.M):
        for name, expr in re.findall(r"(\w+)\s*=\s*(.*?);", section, re.S):
            connector_sets[name] = parse_entries(expr, {})
    tables = []
    for kind, res, header, body in re.findall(r"TABLE_(TRUE|OUTPUT|ANTI)\((\w+)\)\s*:(.*?)=\s*\{(.*?)\}\s*;", text, re.S):
        cols = parse_entries(header, connector_sets)
        col_groups = [g for g in dict.fromkeys((o, tuple(c), label) for o, c, label in cols)]
        rows = []
        for line in body.strip().splitlines():
            if ":" not in line: continue
            lhs, rhs = line.rsplit(":", 1)
            for o, c, _ in parse_entries(lhs, connector_sets):
                rows.append((o, c, rhs.split()))
        tables.append(dict(kind=kind, resource=res, cols=[(o, list(c)) for o, c, _ in col_groups], rows=rows))
    return ops, resources, ranges, tables

# ---------- md ----------

OPERAND = re.compile(r"(?<![/\w])(\w+)(?:\([^)]*\))?\*?:(\w+)")

def parse_md(text):
    classes = []
    for m in re.finditer(r'^(ALTERNATE\s+)?CLASS\s+"([^"]+)"(.*?)(?=^(?:ALTERNATE\s+)?CLASS\s+"|\Z)', text, re.S | re.M):
        alt, name, body = m.groups()
        sec = lambda s: (re.search(rf"^{s}\b(.*?)(?=^[A-Z_]+\s*$|^[A-Z_]+\b(?=\s)|\Z)", body, re.S | re.M) or [None, ""])[1]
        fmt = sec("FORMAT")
        classes.append(dict(
            cls=name, alternate=bool(alt),
            operands=[(t, n) for t, n in OPERAND.findall(fmt) if t not in {"PREDICATE"}],
            preds={k: int(v) if re.fullmatch(r"-?\d+", v) else v
                   for k, v in re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*;\s*$", sec("PREDICATES"), re.M)},
            props=dict(re.findall(r"(\w+)\s*=\s*([^;]+?)\s*;", sec("PROPERTIES"))),
            opcodes=re.findall(r"^\s*([\w.]+)\s*=\s*0b[01]+\s*;", sec("OPCODES"), re.M)))
    return classes

# ---------- join ----------

def class_semantics(c, explicit_anywhere, ops, resources, ranges, tables):
    opcodes = set(c["opcodes"])
    base = sorted({o for o in opcodes if not o.endswith("_pipe")})
    pipes = sorted({o[len(b):] for o in opcodes for b in base if o.startswith(b) and o != b})
    roles, lat = defaultdict(set), defaultdict(list)
    def width(rng):
        size = c["preds"].get(ranges.get(rng), 32) if rng else 32
        return max(size, 1) + 31 >> 5 if isinstance(size, int) else f"ceil(({size})/32)"
    spans = {}
    for t in tables:
        col_role, row_role = ROLES[t["kind"]]
        for o, conns in t["cols"]:
            if opcodes & ops.get(o, set()):
                for n, rng in conns: roles[n].add(col_role); spans[n] = width(rng)
        for o, conns, vals in t["rows"]:
            if not opcodes & ops.get(o, set()): continue
            for n, rng in conns: roles[n].add(row_role); spans[n] = width(rng)
            if t["kind"] == "TRUE":
                cols = [f"{co}`{{{','.join(n for n, _ in cc)}}}" for co, cc in t["cols"]]
                lat[f"{t['resource']}:{','.join(n for n, _ in conns)}"].append(dict(zip(cols, vals)))
    named = {n for _, n in c["operands"]}
    operand = lambda t, n: dict(name=n, type=t, resource=resources.get(n), role=sorted(roles.get(n, [])) or None, span=spans.get(n))
    return dict(
        cls=c["cls"], alternate=c["alternate"], opcodes=base, pipes=pipes,
        inst_type=c["props"].get("INSTRUCTION_TYPE"), sizes=c["preds"],
        operands=[operand(t, n) for t, n in c["operands"]],
        implicit=[dict(name=n, resource=resources.get(n), role=sorted(r), span=spans.get(n))
                  for n, r in sorted(roles.items()) if n not in named and n not in explicit_anywhere],
        true_latency=lat)

def main(indir="out"):
    d = pathlib.Path(indir)
    for md in sorted(d.glob("md_SM*.txt")):
        arch = md.stem.removeprefix("md_")
        lat = parse_latencies((d / f"latencies_{arch}.txt").read_text())
        classes = parse_md(md.read_text())
        explicit = {n for c in classes for _, n in c["operands"]}
        rows = [class_semantics(c, explicit, *lat) for c in classes]
        (d / f"semantics_{arch}.json").write_text(json.dumps(rows, indent=1))
        unresolved = sum(1 for r in rows for o in r["operands"] if o["resource"] and not o["role"])
        print(f"{arch}: {len(rows)} classes, {len({o for r in rows for o in r['opcodes']})} opcodes, "
              f"{unresolved} register operands without role")

if __name__ == "__main__":
    main(*sys.argv[1:])
