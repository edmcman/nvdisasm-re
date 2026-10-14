"""Memory and special registers: LDG/STG, LDS/STS, LDL/STL and LDC loads and stores, ULDC,
S2R/CS2R/S2UR. Every access first records a memory-ordering event with its encoded controls (the
runtime rejects INVALID ones). Explicit descriptors, dual registers,
predicate results and non-IA address modes are unmodelled."""
from sass.sem import const, extract, load, sext, trunc, zext
from sass.sem_family import effect, family, outputs, store, unmodelled

SPACES = {"LDG": "ram", "STG": "ram", "LDS": "shared", "STS": "shared", "LDL": "localmem", "STL": "localmem", "LDC": "cbank"}
SIZES = {"U8": (8, False), "S8": (8, True), "U16": (16, False), "S16": (16, True), "32": (32, False), "64": (64, False), "128": (128, False)}
CONTROLS = ("sem", "sco", "cop", "cop2", "private")


def required(i, name, *labels):
    label = i.select_fork(name)
    if label not in labels: unmodelled(f"{name} {label}")


def ordering(i):
    return effect("sass_memory_order", const(i.k.order, 32), *(i.encoding(n) for n in CONTROLS if i.has_operand(n)))


def size(i):
    """(bits, signed) of the access, from sz."""
    label = i.select_fork("sz")
    hit = next((v for n, v in SIZES.items() if label == n), None)
    if hit is None: unmodelled(f"sz {label}")
    return hit


def extended(v, w, signed): return v if v.w == w else (sext if signed else zext)(v, w)


def address(i):
    """Ra (zero-extended to 64 bits, scaled by .X4/.X8/.X16) plus the uniform part and the signed offset."""
    if i.has_operand("Rd2") or i.has_operand("Rb2"): unmodelled("dual registers")
    # .E and the one-label input_reg_sz_* selectors name Ra's width, which its span already gives.
    for n in ("e", "input_reg_sz_32_dist", "input_reg_sz_64_dist", "input_reg_sz_32_bit75_dist", "input_reg_sz_64_bit75_dist"):
        if i.has_operand(n): i.select_fork(n)
    if i.has_operand("memoryDescriptor"):
        if not i.has_operand("e_desc"): unmodelled("explicit descriptor")
        required(i, "e_desc", "noexp_desc")   # the implicit descriptor of ordinary CUDA pointers: ram
        i.ignore("memoryDescriptor")
    if i.has_operand("ad"): required(i, "ad", "IA")
    if i.has_operand("sp2"): required(i, "sp2", "nosp2")
    if i.has_operand("Pnz") and not i.is_true("Pnz"): unmodelled("Pnz")
    base = i.value("Ra", modifiers=False)
    base = zext(base, 64) if base.w < 64 else base
    if i.has_operand("stride"):
        base = base * i.select_lookup_const("stride", {"X1": 1, "X4": 4, "X8": 8, "X16": 16}, 64)
    uniform = next((n for n in ("Ra_URb", "Ra_URc") if i.has_operand(n) and not i.has_operand("memoryDescriptor")), None)
    if uniform: base = base + zext(i.value(uniform, modifiers=False), 64)
    return base + sext(i.imm("Ra_offset|Sa_offset"), 64) if i.has_operand("Ra_offset|Sa_offset") else base


@family("LDG", "STG", "LDS", "STS", "LDL", "STL", "LDC")
def memory(i):
    """Rd = sz bits at the address (sign- or zero-extended), or store Rb's low sz bits there; LDC
    reads constant bank Sa_bank at the low 32 bits of the address."""
    space = SPACES[i.opcode]
    for n in ("Pu", "Pv", "Pq"):   # predicate results are not assumed to mean success
        if i.has_operand(n) and not i.is_zero(n): unmodelled(f"{n} result")
    addr = address(i)
    if space == "cbank":
        if not i.has_operand("Sa_bank"): unmodelled("bank")
        addr = (zext(i.imm("Sa_bank"), 64) << 32) | zext(trunc(addr, 32), 64)
    w, signed = size(i)
    if i.opcode.startswith("LD"):
        if w > i.width("Rd"): unmodelled("size")
        return outputs(effects=[ordering(i)], Rd=extended(load(space, addr, w), i.width("Rd"), signed))
    value = i.value("Rb", modifiers=False)
    if w > value.w: unmodelled("size")
    return outputs(effects=[ordering(i)], stores=[store(space, addr, trunc(value, w) if value.w > w else value)])


@family("ULDC")
def uldc(i):
    """URd = sz bits of constant bank Sa_bank at Sa_addr, sign- or zero-extended."""
    if not i.has_operand("Sa") or i.kind("Sa") != "constant" or not i.has_operand("Sa_addr"): unmodelled("operand form")
    w, signed = size(i)
    if w > i.width("URd"): unmodelled("size")
    addr = (zext(i.imm("Sa_bank"), 64) << 32) | zext(trunc(zext(i.imm("Sa_addr"), 64), 32), 64)
    i.ignore("Sa")
    return outputs(URd=extended(load("cbank", addr, w), i.width("URd"), signed))


@family("S2R", "CS2R", "S2UR")
def s2r(i):
    """Rd/URd = the special register SRa (CS2R.64: a register pair's view); SRZ reads 0."""
    if i.has_operand("sz"): i.select_fork("sz")   # .32/.64: Rd's span already gives the width
    d = i.resolve("Rd|URd")
    return outputs(**{d: i.special("SRa", i.width(d))})
