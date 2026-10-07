"""Reference SASS decoder driven by the machine-description IR."""
import math, re, struct
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from sass import ir

IMM = re.compile(r"(?P<rel>R)?(?P<signed>S|U)Imm|(?P<float>F(?:16|32|64))Imm")
PREFIX = {"C": "c", "CX": "cx", "A": "a", "DESC": "desc"}
FLAG_FMT = {"not": "!{}", "negate": "-{}", "invert": "~{}", "absolute": "|{}|"}

class NoMatch(Exception): pass

@dataclass
class Decoded:
    klass: ir.Klass
    text: str
    env: dict
    candidates: list

def table_inverse(arch, name):
    return _inverse(arch.name, name)

@cache
def _inverse(archname, name):
    arch = ir.load(archname)
    inv = defaultdict(list)
    for inputs, out in arch.tables[name]:
        inv[resolve(arch, out)].append(tuple(resolve(arch, t) for t in inputs))
    return inv

def resolve(arch, tok):
    if isinstance(tok, int): return tok
    if m := re.fullmatch(r"-?(0x[0-9a-fA-F]+|0b[01_]+|\d+)", tok): return int(tok.replace("_", ""), 0)
    if "@" in tok:
        enum, name = tok.split("@", 1)
        return arch.enums.get(enum, {}).get(name.strip('"'), tok)
    return tok

def decode_env(arch, k, w):
    env, widths = {}, {}
    for f, r in k.rules:
        v = f.get(w)
        match r:
            case ("op", name, scale, None): env[name] = v * scale; widths[name] = f.width + scale.bit_length() - 1
            case ("op", name, scale, conv): env[name] = v; widths[name] = f.width; env[("__conv", name)] = conv
            case ("attr", name, attr): env[(name, attr)] = v
    for f, r in k.rules:
        if r[0] != "table": continue
        _, tname, args = r
        if tname == "IDENTICAL":
            for a in args: assign(env, a, f.get(w))
            continue
        if tname not in arch.tables: continue
        for row in table_inverse(arch, tname).get(f.get(w), ()):
            if all(consistent(env, a, v) for a, v in zip(args, row)):
                for a, v in zip(args, row): assign(env, a, v)
                break
        else:
            raise NoMatch(f"{k.name}: no {tname} row for {f.get(w):#x}")
    for name in [n for n in env if isinstance(n, tuple) and n[0] == "__conv"]:
        env[("__fmt", name[1])] = float_kind(arch, env, env.pop(name))
    return env, widths

def float_kind(arch, env, conv):
    """convertFloatType(cond1, T1, cond2, T2, ..., Tdefault) -> kind of the first true condition."""
    parts = [p.strip() for p in conv.split(",")]
    for cond, kind in zip(parts[0::2], parts[1::2]):
        if any(env.get(n.strip()) == resolve(arch, v.strip().lstrip("`"))
               for n, v in (c.split("==") for c in cond.split("||"))):
            return kind.removesuffix("Imm")
    return parts[-1].removesuffix("Imm")

def key(a): return a[1] if a[0] == "op" else (a[1], a[2])

def consistent(env, a, v):
    match a:
        case ("const", n): return n == v
        case _: return env.get(key(a), v) == v

def assign(env, a, v):
    if a[0] != "const": env[key(a)] = v

def sext(v, n): return v - (1 << n) if n and v >> (n - 1) & 1 else v

def hexs(v): return f"-{-v:#x}" if v < 0 else f"{v:#x}"

def fmt_float(bits, kind):
    match kind:
        case "F32": x = struct.unpack("<f", struct.pack("<I", bits & 0xffffffff))[0]
        case "F64": x = struct.unpack("<d", struct.pack("<Q", (bits & 0xffffffff) << 32))[0]
        case "F16": x = struct.unpack("<e", struct.pack("<H", bits & 0xffff))[0]
        case "E8M7": x = struct.unpack("<f", struct.pack("<I", (bits & 0xffff) << 16))[0]
        case "E6M9": x = minifloat(bits & 0xffff, 6, 9)
    sign = "-" if math.copysign(1, x) < 0 else "+"
    # nvdisasm prints these with a trailing space, visible before a suffix: `+QNAN .H1`
    if math.isinf(x): return sign + "INF "
    if x == 0 and sign == "-": return "-0.0 "
    if math.isnan(x): return sign + ("QNAN " if bits >> QUIET_BIT[kind] & 1 else "SNAN ")
    return f"{x:.20e}" if abs(x) >= 1e9 else f"{x:.20g}"

QUIET_BIT = {"F32": 22, "F64": 19, "F16": 9, "E8M7": 6, "E6M9": 8}

def minifloat(bits, e, m):
    sign, exp, man = bits >> (e + m) & 1, bits >> m & ((1 << e) - 1), bits & ((1 << m) - 1)
    bias = (1 << (e - 1)) - 1
    match exp:
        case 0: x = man * 2.0 ** (1 - bias - m)
        case _ if exp == (1 << e) - 1: x = math.nan if man else math.inf
        case _: x = (1 + man / (1 << m)) * 2.0 ** (exp - bias)
    return -x if sign else x

def value_text(arch, atom, env, widths):
    """Text for an operand value, or None when it equals its default and is omitted."""
    v = env.get(atom.name)
    etype = env.get(("__type", atom.name), atom.type)
    if etype in arch.enums:
        names = arch.rev_enums[etype]
        if v is None:
            if atom.default is None and len(names) != 1: raise NoMatch(f"{atom.name} unset")
            v = arch.enums[etype].get(atom.default, next(iter(names))) if atom.default is not None else next(iter(names))
        if v not in names:
            if atom.name in env.get("__strict", ()): raise NoMatch(f"{atom.name}={v} not in {etype}")
            return f"???{v}", False
        return names[v], arch.enums[etype].get(atom.default) == v
    if m := IMM.match(atom.type):
        width, dflt, *opts = atom.args.split("/") + [""]
        n = widths.get(atom.name) or (int(width) if width.isdigit() else 32)
        v = v or 0
        always = dflt.endswith("*") or "PRINT" in opts  # e.g. UImm(5/0*) bank, UImm(n/0/PRINT)
        is_default = dflt != "" and not always and v == int(dflt, 0)
        if m["float"]: return fmt_float(v, env.get(("__fmt", atom.name), m["float"])), is_default
        if m["rel"]: return sext(v, n), is_default
        return hexs(sext(v, n) if m["signed"] == "S" else v), is_default
    if atom.type == "BITSET":
        dflt = int(atom.args.partition("/")[2] or "0", 0)
        v = v or 0
        return "{" + ",".join(str(i) for i in reversed(range(v.bit_length())) if v >> i & 1) + "}", v == dflt
    if atom.type in PREFIX: return PREFIX[atom.type], False
    return f"<{atom.type}:{atom.name}={v}>", False

def has_suffix(fmt, idx):
    rest = fmt[idx + 1:]
    end = next((j for j, a in enumerate(rest) if a.kind == "lit" and a.name == ","), len(rest))
    return any(a.kind == "mod" for a in rest[:end])

def wrap(t, on):
    for f in sorted(on, key=lambda f: f != "absolute"): t = FLAG_FMT[f].format(t)
    return t

def render(arch, k, env, widths):
    items, flags, guard, omitted = [[k.mnemonic]], [], "", []
    hide_desc = skipping = False
    last = span = None  # span: an operand whose flags enclose it plus its directly following modifiers
    depth = 0

    def close_span():
        nonlocal span
        if span:
            i, start, on, bank_gap = span
            t = "".join(" " + x if bank_gap and x == "[" and items[i][j - 1] == "]" else x
                        for j, x in enumerate(items[i][start:], start))
            items[i][start:] = [wrap(t, on)]
        span = None

    for idx, a in enumerate(k.format):
        if skipping:
            skipping = a.kind != "close"
            continue
        if a.kind in ("lbrace", "flag") or a.kind == "lit" and a.name == "," or a.kind == "operand" and depth == 0:
            close_span()
        match a.kind:
            case "flag": flags.append(a)
            case "guard":
                t, _ = value_text(arch, a, env, widths)
                neg = any(env.get((a.name, f.name)) for f in flags)
                guard = "" if t == a.default and not neg else f"@{'!' if neg else ''}{t} "
                flags = []
            case "opcode" | "lbrace" | "rbrace": pass
            case "mod" if a.type == "EXP_DESC": hide_desc = env.get(a.name, 0) == 0
            case "mod":
                t, dflt = value_text(arch, a, env, widths)
                if not dflt:
                    if last == "omitted": items[-1].append(omitted[-1])  # a modifier keeps its default operand: [RZ.X8]
                    items[-1].append("." + t)
            case "lit" if a.name == ",": items.append([])
            case "lit": items[-1].append(a.name)
            case "open":
                if len(items) == 1: items.append([])
                items[-1].append("["); omitted = []; depth += 1
            case "close":
                while items[-1][-1] == "+": items[-1].pop()
                if items[-1][-1] == "[" and omitted: items[-1].append(omitted[0])
                items[-1].append("]"); depth -= 1
            case "plus": items[-1].append("+")
            case "operand" if a.type == "DESC" and hide_desc:
                if len(items) == 1: items.append([])
                skipping = True
            case "operand":
                if len(items) == 1: items.append([])
                t, dflt = value_text(arch, a, env, widths)
                if a.type == "RSImm": t = hexs(env["__addr"] + 16 + t)
                on = [f.name for f in flags if env.get((a.name, f.name))]
                flags = []
                if dflt and not on:
                    omitted.append(t)
                    if depth and items[-1] and items[-1][-1] == "+": items[-1].pop()  # drop the separator with the operand
                    last = "omitted"
                    continue
                bank = a.type in ("C", "CX")
                if depth == 0 and (on or bank):
                    span = (len(items) - 1, len(items[-1]), on, bank and has_suffix(k.format, idx))
                else:
                    t = wrap(t, on)
                items[-1].append(" " + t if last == "operand" else t)
        last = a.kind
    close_span()
    ops = [re.sub(r"\[\+|\+\]", lambda m: m[0].replace("+", ""), "".join(i)) for i in items[1:]]
    ops = [o for o in ops if o]
    return guard + "".join(items[0]) + (" " + ", ".join(ops) if ops else "")

@cache
def index(archname):
    arch, idx = ir.load(archname), defaultdict(list)
    for k in arch.classes: idx[k.opcode & 0xfff].append(k)
    return idx

def candidates(arch, w):
    return [k for k in index(arch.name)[w & 0xfff] if w & k.constraint_mask == k.constraint_bits]

def decode(archname, word: bytes, addr=0):
    arch, w = ir.load(archname), int.from_bytes(word, "little")
    ok, errs = [], []
    for k in candidates(arch, w):
        try:
            env, widths = decode_env(arch, k, w)
            env["__addr"], env["__strict"] = addr, k.strict
            pseudo = apply_aliases(arch, k, env, widths) and sched_valid(arch, k, env)
            ok.append((k, render(arch, k, env, widths), env, pseudo))
        except NoMatch as e: errs.append(str(e))
    # ALTERNATE classes are assembler syntax; nvdisasm prints one only as an active pseudo-op (e.g. IMAD.MOV).
    ok = [o for o in ok if o[3] or not o[0].alternate]
    if not ok: raise NoMatch("; ".join(errs) or "only ALTERNATE classes match")
    plain = [o for o in ok if not o[3]]
    if len(plain) > 1 and not any(o[3] for o in ok):
        raise NoMatch("ambiguous: " + " and ".join(o[0].name for o in plain))  # nvdisasm: "More than one pattern matched"
    k, text, env, _ = min(ok, key=lambda o: (not o[3], o[0].order))
    return Decoded(k, text, env, [o[0] for o in ok])

def sched_valid(arch, k, env):
    return all(env[n] in arch.rev_enums[t] for n, t in k.sched.items() if t in arch.enums and n in env)

def alias_value(k, env, name, widths):
    a = k.operand_types.get(name)
    v = env.get(name)
    return sext(v, widths.get(name, 32)) if a is not None and a.type == "SImm" and v is not None else v

def apply_aliases(arch, k, env, widths):
    """Evaluate FORMAT_ALIAS pattern tables (first matching row, '-' is a wildcard). True if any is non-default."""
    active = False
    for name, etype, table, args in k.aliases:
        row = next((out for ins, out in arch.tables[table]
                    if all(t == "-" or resolve(arch, t) == alias_value(k, env, a, widths) for t, a in zip(ins, args))), 0)
        env[name], env[("__type", name)] = resolve(arch, row), etype
        active |= env[name] != 0
    return active
