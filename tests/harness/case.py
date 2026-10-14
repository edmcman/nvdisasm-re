"""Single-instruction emulator cases: operand placement and the Case/Cases collections
(words come from sass.decode.pyencode)."""
from typing import NamedTuple
from sass.decode import ir
from sass.decode.pyencode import encode

IMMEDIATES = ("SImm", "UImm", "F32Imm")


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
READABLE_FLOATS = {"FADD", "FMUL", "FFMA", "FADD2", "FMUL2", "FFMA2", "FHADD2", "FHMUL2", "FHFMA2", "DADD", "DMUL", "DFMA", "HADD2", "HFMA2", "HMUL2", "FSEL", "FMNMX", "FSETP"}


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
