"""Single-instruction emulator cases: md-driven encoding and operand placement."""
from functools import cache
from typing import NamedTuple
from sass import ir, pydecode
from sass.gen_sleigh import default_value, ENUM_FILE, REG_FILES

IMMEDIATES = ("SImm", "UImm", "F32Imm")


@cache
def klass(arch, name):
    return next(c for c in ir.load(arch).classes if c.name == name)


def encode(arch, cls, **values):
    """Hex word for `cls` with operand values (enum labels or numbers); `name@attr` sets modifiers."""
    md, k = ir.load(arch), klass(arch, cls)
    desired = {}
    for atom in k.operand_types.values():
        value = values.get(atom.name, default_value(md, atom))
        if value is None and atom.type in ENUM_FILE:
            value = REG_FILES[ENUM_FILE[atom.type]][3] if atom.kind == "guard" or "Predicate" in atom.type else 0
        if value is None: value = min(md.enums[atom.type].values()) if atom.type in md.enums else 0
        if isinstance(value, str): value = md.enums[atom.type][value]
        desired[atom.name] = value
    desired.update({tuple(n.split("@")): v for n, v in values.items() if "@" in n})
    word = k.constraint_bits
    for f, r in k.rules:
        match r[0]:
            case "op": raw = desired.get(r[1], 0) // r[2]
            case "attr": raw = desired.get((r[1], r[2]), 0)
            case "table" if r[1] == "IDENTICAL": raw = desired[r[2][0][1]]
            case "table":
                candidates = [(sum(desired.get(pydecode.key(a), 0) != v for a, v in zip(r[2], row) if a[0] != "const"), raw)
                              for raw, rows in pydecode.table_inverse(md, r[1]).items() for row in rows
                              if all(a[0] != "const" or a[1] == v for a, v in zip(r[2], row))]
                if not candidates: raise ValueError(r[1])
                raw = min(candidates)[1]
            case _: continue
        word = (word & ~f.mask) | f.put(raw & ((1 << f.width) - 1))
    data = ((word & ~k.constraint_mask) | k.constraint_bits).to_bytes(16, "little")
    decoded = pydecode.decode(arch, data)
    if decoded.klass.name != cls: raise ValueError((cls, decoded.klass.name))
    for name, value in values.items():
        key = tuple(name.split("@")) if "@" in name else name
        if isinstance(value, str): value = md.enums[k.operand_types[name].type][value]
        if key in decoded.env and decoded.env[key] != value:
            if isinstance(value, int) and value < 0:
                source = next(((f, r[2]) for f, r in k.rules if r[0] == "op" and r[1] == name), None)
                if source and decoded.env[key] == value % ((1 << source[0].width) * source[1]): continue
            raise ValueError((name, value, decoded.env[key]))
    return data.hex()


def word(arch, cls, **values):
    return int.from_bytes(bytes.fromhex(encode(arch, cls, **values)), "little")


def cbank(bank, address, value, size=4):
    return dict(space="cbank", address=(bank << 32) | address, hex=(value & ((1 << 8 * size) - 1)).to_bytes(size, "little").hex())


def ram(address, value, size=4):
    return dict(space="ram", address=address, hex=(value & ((1 << 8 * size) - 1)).to_bytes(size, "little").hex())


def pair(name, bits):
    """A 64-bit value in register `name` and its successor (R or UR)."""
    prefix = name.rstrip("0123456789")
    return {name: bits & 0xffffffff, f"{prefix}{int(name[len(prefix):]) + 1}": bits >> 32}


def is_cx(k):
    return any(atom.type == "CX" for atom in k.operand_types.values())


def source(k, *names):
    """The first of `names` (e.g. Rb, URb, Sb) that `k` has as an operand."""
    return next((n for n in names if n in k.operand_types), None)


def labels(md, k, operand, wanted):
    """The labels in `wanted` that operand's enum defines, in order."""
    enum = md.enums[k.operand_types[operand].type]
    return [label for label in wanted if label in enum]


def put(k, name, value, fields, regs, memory, reg, ureg=None, bank=None, imm=IMMEDIATES):
    """Place `value` in operand `name` by its md type: register `reg`, uniform `ureg` (default
    `reg`), constant bank/offset `bank` (if given), or an immediate of a type in `imm`.
    False if the type is not accepted."""
    match k.operand_types[name].type:
        case "Register": fields[name] = reg; regs[f"R{reg}"] = value
        case "UniformRegister":
            ureg = reg if ureg is None else ureg
            fields[name] = ureg; regs[f"UR{ureg}"] = value
        case "C" if bank:
            fields.update({name + "_bank": bank[0], name + "_addr": bank[1]})
            memory.append(cbank(*bank, value))
        case t if t in imm: fields[name] = value
        case _: return False
    return True


def cx_source(arch, k, name, value, ur=4, offset=0, base=0x2000):
    """Encode a CX header in URur/URur+1 and map the loaded word in GPU virtual memory."""
    atom = k.operand_types[name]; children = []
    for child in k.format[k.format.index(atom) + 1:]:
        if child.kind == "lit" and child.name == ",": break
        if child.kind == "operand": children.append(child)
    ur_name = next(c.name for c in children if c.type in ("UniformRegister", "NonZeroUniformRegister", "ZeroUniformRegister"))
    off_name = next(c.name for c in children if c.type in ("UImm", "SImm"))
    return {ur_name: ur, off_name: offset}, cx_header(arch, ur, base), [ram(base + offset, value)]


def cx_header(arch, ur, base, units=1):
    bits, shift = (51, 6) if arch == "SM90" else (45, 4)
    header = base // (1 << shift) + (units << bits)
    return {f"UR{ur}": header & 0xffffffff, f"UR{ur + 1}": header >> 32}


class Case(NamedTuple):
    cls: str
    request: dict
    expected: dict


# Mnemonics whose readable lowering differs from the exact one: their exact-mode cases do
# not apply to the readable language, which gets its own (portable=True) cases instead.
READABLE_FLOATS = {"FADD", "FMUL", "FFMA", "DADD", "DMUL", "DFMA", "HADD2", "HFMA2", "HMUL2", "FSEL", "FMNMX", "FSETP"}


class Cases(list):
    """Collects Cases for one architecture and fp lowering, skipping classes it lacks."""

    def __init__(self, arch, fp="exact"):
        super().__init__()
        self.arch, self.fp, self.md = arch, fp, ir.load(arch)
        self.classes = {k.name: k for k in self.md.classes if not k.alternate}

    def add(self, cls, fields, registers, expected, portable=False, observe=None, **extras):
        """portable: valid for both lowerings even if the mnemonic is in READABLE_FLOATS."""
        if cls not in self.classes: return
        if self.fp == "readable" and not portable and self.classes[cls].mnemonic in READABLE_FLOATS: return
        if not extras.get("memory", True): del extras["memory"]
        request = dict(word=encode(self.arch, cls, **fields), registers=registers,
                       observe=[n for n in expected if not n.startswith("__")] if observe is None else observe, **extras)
        if "__max_ops" in expected or "__native" in expected: request["inspect_pcode"] = True
        self.append(Case(cls, request, expected))

    def kernel(self, name, cls, fields, error, max_steps=4):
        """A one-instruction kernel expected to stop with `error`."""
        if cls not in self.classes: return
        request = dict(kernel=dict(name=name, code=encode(self.arch, cls, **fields), threads=1, max_steps=max_steps),
                       output=dict(address=0x200000000))
        self.append(Case(name, request, dict(error=error)))

    def of(self, mnemonic, prefix="", cx=False):
        """Non-alternate classes of `mnemonic` named `prefix...`; cx=None keeps both CX and direct forms."""
        return [k for k in self.md.classes if k.mnemonic == mnemonic and k.name.startswith(prefix)
                and not k.alternate and (cx is None or is_cx(k) == cx)]

    def named(self, *prefixes):
        """Non-alternate classes whose names start with any of `prefixes`."""
        return [k for k in self.md.classes if not k.alternate and k.name.startswith(prefixes)]

    def enum(self, k, operand):
        return self.md.enums[k.operand_types[operand].type]

    def labels(self, k, operand, wanted):
        return labels(self.md, k, operand, wanted)
