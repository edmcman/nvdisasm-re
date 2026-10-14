"""Instruction bytes -> semantics, without Ghidra.

`lift(arch, word, addr)` decodes with pydecode (md-illegal encodings don't decode), runs the
opcode's family once with every static question answered from the decoded fields, and returns the
writes over concrete machine locations. RZ/URZ reads are 0, PT reads 1, writes to them are
dropped; `sass.semantics.evaluate.execute` runs the result.
"""
from dataclasses import dataclass, field
from sass.decode import ir, pydecode
from sass.md import expr as mdexpr
from sass.semantics import bitvec as sem
from sass.semantics.bitvec import Expr
from sass.semantics.family import Instruction, Illegal, Primitive, Unmodelled, PR_BITS, REG_TYPES, family_for, roles
import sass.semantics.families  # registers the families

ELEMENT = {"R": 32, "UR": 32, "P": 1, "UP": 1}


@dataclass
class Semantics:
    arch: str
    cls: str
    text: str
    addr: int
    status: str                                  # native | opaque
    reason: str = ""
    guard: Expr | None = None                    # execute only if nonzero
    writes: list = field(default_factory=list)   # [(register location, value)], simultaneous
    flow: object = None                          # sem_family.Flow over concrete values, or None
    stores: list = field(default_factory=list)   # [sem_family.Store], after the writes


class Lifted(Instruction):
    """Answers from one decoded word."""

    def __init__(self, arch, decoded, addr, fp="exact"):
        super().__init__(arch, decoded.klass, fp)
        self.env, self.widths, self.at = decoded.env, decoded.widths or {}, addr

    def answer(self, key, domain):
        if isinstance(key, tuple) and key[0] == "zero": return self.zero(key[1])
        if isinstance(key, tuple) and key[0] == "in": return self.env.get(key[1], 0) in key[2]
        if isinstance(key, tuple) and key[0] == "low": return self.env.get(key[1], 0) & ((1 << key[2]) - 1)
        return self.env.get(key, 0)

    def field_width(self, name):
        return next((f.width for f, r in self.k.rules if r[0] == "op" and r[1] == name), None)

    def zero_index(self, name):
        file = REG_TYPES[self.atom(name).type]
        if file in ("P", "UP"): return 7
        if file == "R": return 255
        width = self.field_width(name)
        zero = self.arch.enums["UniformRegister"]["URZ"]
        return zero if width is None else zero & ((1 << width) - 1)

    def zero(self, name):
        if self.atom(name).type in REG_TYPES: return self.env.get(name) == self.zero_index(name)
        return not self.env.get(name)

    def span(self, name):
        record = next((o for o in roles(self.arch.name).get(self.cls, {}).get("operands", []) if o["name"] == name), {})
        span = record.get("span") or 1
        return int(mdexpr.evaluate(span, self.arch, self.env)) if isinstance(span, str) else span

    def location(self, name):
        """(file, index, count) of a register operand, or None for RZ/URZ/PT."""
        file = REG_TYPES[self.atom(name).type]
        index = self.env.get(name, self.arch.enums[self.atom(name).type].get(self.atom(name).default))
        if index == self.zero_index(name): return None
        return file, index, self.span(name)

    def operand(self, name, width):
        a = self.atom(name)
        if a.type == "PR": return sem.concat(*(sem.reg("P", k, 1, element=1) for k in reversed(range(PR_BITS))))
        if a.type in REG_TYPES:
            loc = self.location(name)
            if loc is None: return sem.const(1 if REG_TYPES[a.type] in ("P", "UP") else 0, width)
            v = sem.reg(*loc, element=ELEMENT[loc[0]])
        elif m := pydecode.IMM.match(a.type):
            raw = self.env.get(name)
            if raw is None: raw = int(a.args.split("/")[1], 0) if a.args.count("/") and a.args.split("/")[1] else 0
            if m["signed"] == "S" or m["rel"]: raw = pydecode.sext(raw, self.widths.get(name, 32))
            # F64 supplies the high word: by the conversion rule when there is one (half
            # immediates are F64Imm converted to F16), else by the type.
            if self.env.get(("__fmt", name), "F64" if a.type == "F64Imm" else None) == "F64": raw <<= 32
            v = sem.const(raw, max(width, 64 if a.type == "F64Imm" else width))
        elif a.type == "C":
            bank, offset = self.env.get(name + "_bank", 0), self.env.get(name + "_addr", 0)
            v = sem.load("cbank", sem.const(bank << 32 | offset & 0xffffffff, 64), width)
        elif a.type == "CX": v = self.cx(name, width)
        else: raise Unmodelled(f"{a.type} operand {name}")
        return v if v.w == width else sem.zext(v, width) if v.w < width else sem.trunc(v, width)

    def cx(self, name, width):
        """A bounded constant-buffer load through a 64-bit uniform header plus a byte offset."""
        if self.arch.name not in ("SM75", "SM80", "SM86", "SM89", "SM90"): raise Unmodelled(f"CX header layout on {self.arch.name}")
        start = self.k.format.index(self.atom(name)) + 1; children = []
        for child in self.k.format[start:]:
            if child.kind == "lit" and child.name == ",": break
            if child.kind == "operand": children.append(child)
        ur = next(c.name for c in children if c.type in ("UniformRegister", "NonZeroUniformRegister", "ZeroUniformRegister"))
        off = next(c.name for c in children if c.type in ("UImm", "SImm"))
        header, offset = self.operand(ur, 64), sem.zext(self.operand(off, 32), 64)
        bits, shift = (51, 6) if self.arch.name == "SM90" else (45, 4)
        limit = (header >> bits) << 4
        address = ((header & ((1 << bits) - 1)) << shift) + offset
        return sem.ite(limit < offset + width // 8, sem.const(0, width), sem.load("ram", address, width))

    def choice(self, name, values):
        v = self.env.get(name)
        if v not in values: raise Unmodelled(f"{name} {self.select_fork(name)}")
        return values[v]

    def addr(self): return sem.const(self.at, 64)

    def encoding(self, names, w=32):
        name = self.resolve(names); self.asked.add(name)
        return sem.const(self.env.get(name, 0), w)

    def special(self, names, w):
        name = self.resolve(names); self.asked.add(name)
        index = self.env.get(name, 0)
        if index == self.arch.enums["SpecialRegister"]["SRZ"]: return sem.const(0, w)
        return sem.reg("SR", index, w // 32, element=32)

    def branch_target(self, names):
        name = self.resolve(names); self.asked.add(name)
        offset = self.imm(name, 64)
        return sem.const(self.at + 16, 64) + offset if self.atom(name).type == "RSImm" else offset

    def target(self, name="sImm"):
        a = self.atom(name); v = pydecode.sext(self.env.get(name, 0), self.widths.get(name, 32))
        return sem.const(self.at + 16 + v if a.type == "RSImm" else v, 64)


def lift(arch_name, word, addr=0, fp="exact", conditions=True):
    """Semantics of one instruction word; pydecode.NoMatch if it doesn't decode (including md-illegal
    encodings unless conditions=False, and encodings a family declares illegal)."""
    d = pydecode.decode(arch_name, word, addr, conditions=conditions)
    arch = ir.load(arch_name)
    i = Lifted(arch, d, addr, fp)
    out = Semantics(arch_name, d.klass.name, d.text, addr, "opaque")
    guard = next((a.name for a in d.klass.format if a.kind == "guard"), None)
    if guard:
        g = i.pred(guard)
        out.guard = None if sem.is_const(g, 1) else g
    fam = family_for(d.klass)
    if fam is None:
        out.reason = "no family"; return out
    try: result = fam(i)
    except Unmodelled as e:
        out.reason = str(e); return out
    except Primitive as e:
        out.status, out.reason = "primitive", str(e); return out
    except Illegal as e: raise pydecode.NoMatch(f"{d.klass.name}: {e} ({e.evidence})")
    for name, value in result.ordered():
        if not i.has_operand(name): continue  # e.g. no second carry output in this class
        if i.atom(i.resolve(name)).type == "PR":
            out.writes += [(sem.reg("P", k, 1, element=1), sem.extract(value, k, k)) for k in range(PR_BITS)]
            continue
        loc = i.location(i.resolve(name))
        if loc is None: continue
        width = loc[2] * ELEMENT[loc[0]]
        if value.w != width: raise TypeError(f"{d.klass.name}: {name} is {width} bits, value {value!r} is {value.w}")
        out.writes.append((sem.reg(*loc, element=ELEMENT[loc[0]]), value))
    out.writes = list(zip([l for l, _ in out.writes], sem.narrow([v for _, v in out.writes])))
    out.flow, out.stores = result.flow, list(result.stores)
    out.status = "native"
    return out
