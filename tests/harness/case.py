"""Single-instruction emulator cases: operand placement and the Case/Cases collections
(words come from sass.assemble.encoding)."""
import hashlib, json
from typing import NamedTuple
from sass.decode import ir
from functools import partial
from sass.assemble import encoding
from sass.sleigh.modifiers import modifiers

# Cases include deliberate edge values (wide immediates, misaligned pairs), which the strict encoder rejects.
encode = partial(encoding.encode, permissive=True)
word = partial(encoding.word, permissive=True)

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



def flagged(k, name, flag):
    """Whether operand `name` of `k` takes the md flag `flag` (negate, invert, not, absolute)."""
    return ((name, flag), None) in modifiers(k)

def labels(md, k, operand, wanted):
    """The labels in `wanted` that operand's enum defines, in order."""
    enum = md.enums[k.operand_types[operand].type]
    return [label for label in wanted if label in enum]


def put(k, name, value, fields, regs, memory, reg, ureg=None, bank=None, imm=IMMEDIATES, size=4):
    """Place `value` in operand `name` by its md type: register `reg`, uniform `ureg` (default
    `reg`), constant bank/offset `bank` (if given), or an immediate of a type in `imm`. A size-8
    value fills a register pair. False if the type is not accepted."""
    place = lambda r: regs.update(pair(r, value) if size == 8 else {r: value})
    match k.operand_types[name].type:
        case "Register": fields[name] = reg; place(f"R{reg}")
        case "UniformRegister":
            ureg = reg if ureg is None else ureg
            fields[name] = ureg; place(f"UR{ureg}")
        case "C" if bank:
            fields.update({name + "_bank": bank[0], name + "_addr": bank[1]})
            memory.append(cbank(*bank, value, size))
        case t if t in imm: fields[name] = value
        case _: return False
    return True


def cx_source(arch, k, name, value, ur=4, offset=0, base=0x2000, size=4):
    """Encode a CX header in URur/URur+1 and map the loaded word in GPU virtual memory."""
    atom = k.operand_types[name]; children = []
    for child in k.format[k.format.index(atom) + 1:]:
        if child.kind == "lit" and child.name == ",": break
        if child.kind == "operand": children.append(child)
    ur_name = next(c.name for c in children if c.type in ("UniformRegister", "NonZeroUniformRegister", "ZeroUniformRegister"))
    off_name = next(c.name for c in children if c.type in ("UImm", "SImm"))
    return {ur_name: ur, off_name: offset}, cx_header(arch, ur, base), [ram(base + offset, value, size)]


def cx_header(arch, ur, base, units=1):
    from sass import launch
    bits, shift = launch.cx_layout(arch)
    header = base // (1 << shift) + (units << bits)
    return {f"UR{ur}": header & 0xffffffff, f"UR{ur + 1}": header >> 32}


class Case(NamedTuple):
    """`fields`: the operand values `Cases.add` encodes the word from, once `Cases.encode_words` runs."""
    cls: str
    request: dict
    expected: dict
    hardware: dict | None = None
    fields: dict | None = None

    @property
    def id(self):
        """Stable identity of a scenario, independent of reference and p-code checks, and of
        encoding when its fields determine the word."""
        request = {k: v for k, v in self.request.items() if k != "inspect_pcode" and not (k == "word" and self.fields is not None)}
        key = request if self.fields is None else [request, self.fields]
        return self.cls + "-" + hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


# Mnemonics whose readable lowering differs from the exact one: their exact-mode cases do
# not apply to the readable language, which gets its own (portable=True) cases instead.
READABLE_FLOATS = {"FADD", "FMUL", "FFMA", "FADD2", "FMUL2", "FFMA2", "FHADD2", "FHMUL2", "FHFMA2", "DADD", "DMUL", "DFMA", "HADD2", "HFMA2", "HMUL2", "FSEL", "FMNMX", "FSETP",
                   "UFADD", "UFMUL", "UFFMA", "UFSEL", "UFSETP"}


class Cases(list):
    """Complete candidates for one architecture/lowering, skipping classes it lacks.

    Caller-controlled selection happens after generation, never inside input loops.
    """

    def __init__(self, arch, fp="exact"):
        super().__init__()
        self.arch, self.fp, self.md = arch, fp, ir.load(arch)
        self.classes = {k.name: k for k in self.md.classes if not k.alternate}

    def add(self, cls, fields, registers, expected, portable=False, observe=None, hardware=None, **extras):
        """portable: valid for both lowerings even if the mnemonic is in READABLE_FLOATS. The word
        stays unencoded until `encode_words`, so sampling encodes only the cases it keeps."""
        if cls not in self.classes: return
        if self.fp == "readable" and not portable and self.classes[cls].mnemonic in READABLE_FLOATS: return
        self.append(Case(cls, self.request(None, registers, expected, observe, extras), expected, hardware, dict(fields)))

    def encode_words(self):
        """Encode every case's word from its fields, checking it has the form sampling assumed."""
        from tests.harness import sampling
        for case in self:
            if case.request.get("word") is None and case.fields is not None:
                case.request["word"] = encode(self.arch, case.cls, **case.fields)
                if sampling.form(self.arch, case.request["word"]) != sampling.declared(self.arch, case.cls, case.fields):
                    raise ValueError(f"{case.id}: {case.cls} {case.fields} encode another form than their modifiers declare")
        return self

    @staticmethod
    def request(word, registers, expected, observe=None, extras=None):
        """Build state/observations independently of instruction encoding."""
        extras = dict(extras or {})
        if not extras.get("memory", True): del extras["memory"]
        request = dict(word=word, registers=registers,
                       observe=[n for n in expected if not n.startswith("__")] if observe is None else observe, **extras)
        if "__max_ops" in expected or "__native" in expected: request["inspect_pcode"] = True
        return request

    def probe(self, cls, fields, registers, observe=(), hardware=None, **extras):
        """A draft hardware scenario with explicit observations and no reference values.

        Kept separate from regression suites: tests.probe/device_cases.observe
        returns raw outputs; emulator_cases rejects drafts until expectations exist.
        """
        if not observe and not extras.get('observe_memory'):
            raise ValueError('a probe needs registers or memory to observe')
        if cls not in self.classes: raise ValueError(f'{self.arch}: unknown probe class {cls}')
        self.add(cls, fields, registers, {}, observe=list(observe), hardware=dict(hardware or {}, probe=True), **extras)
        return self.encode_words()[-1]

    def sweep(self, n):
        """Enumerate a complete input domain; the caller chooses the sampling policy."""
        return range(n)

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
