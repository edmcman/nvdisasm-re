"""Memory and special registers: LDG/STG, LDS/STS, LDL/STL, generic LD/ST and LDC loads and stores, ULDC
and SM100+'s LDCU, S2R/CS2R/S2UR. Every access first records a memory-ordering event with its encoded controls (the
runtime rejects INVALID ones); an explicit descriptor (desc[URx]) is passed to the runtime, which rejects
range-form ones, and otherwise doesn't change the access (SM89 GPU). Dual registers,
predicate results and non-IA address modes are unmodelled; a load's Pnz input zero-fills."""
from sass.semantics.bitvec import concat, const, extract, ite, load, sext, trunc, zext
from sass.semantics.family import effect, family, outputs, store, unmodelled

SPACES = {"LDG": "ram", "STG": "ram", "LDS": "shared", "STS": "shared", "LDL": "localmem", "STL": "localmem", "LDC": "cbank", "LDCU": "cbank",
          "LD": "generic", "ST": "generic"}
# A generic address in a window [SR_*WINHI:SR_*WINLO, + SR_*WINSZ) is that offset of its space (SM89 GPU).
WINDOWS = (("shared", "SR_SWINLO", "SR_SWINHI", "SR_SWINSZ"), ("localmem", "SR_LWINLO", "SR_LWINHI", "SR_LWINSZ"))
SIZES = {"U8": (8, False), "S8": (8, True), "U16": (16, False), "S16": (16, True), "32": (32, False), "64": (64, False), "128": (128, False)}
# SM75 LDS's .U sizes read what the plain ones do: ptxas 13.4 gives every sm_75 shared load .U, per-thread addresses included.
SIZES |= {"U": SIZES["32"], **{"U." + n: v for n, v in SIZES.items()}}
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


def descriptor(i):
    """[the sass_memory_descriptor effect] of a class with a descriptor operand, else []: an L2 cache
    policy that never changes the value accessed (SM89 GPU, docs/hardware-facts.md)."""
    if not i.has_operand("memoryDescriptor"): return []
    i.ignore("memoryDescriptor")
    # SM89 reads the selected UR pair even when noexp_desc hides it in the
    # disassembly. A range descriptor can trap with either value of bit 101.
    if i.has_operand("e_desc"): i.ignore("e_desc")
    return [effect("sass_memory_descriptor", i.value(i.resolve("Ra_URb|Ra_URc"), modifiers=False))]


def address(i):
    """Ra (zero-extended to 64 bits, scaled by .X4/.X8/.X16) plus the uniform part and the offset (SImm
    sign-extended, UImm zero-extended)."""
    if i.has_operand("Rd2") or i.has_operand("Rb2"): unmodelled("dual registers")
    # .E and the one-label input_reg_sz_* selectors name Ra's width, which its span already gives.
    for n in ("e", "input_reg_sz_32_dist", "input_reg_sz_64_dist", "input_reg_sz_32_bit75_dist", "input_reg_sz_64_bit75_dist"):
        if i.has_operand(n): i.select_fork(n)
    if i.has_operand("ad"): required(i, "ad", "IA")
    # sp2 (.LTC64B/.LTC128B/.LTC256B) is an L2 prefetch-size hint; it doesn't change the value.
    if i.has_operand("sp2"): i.ignore("sp2")
    base = i.value("Ra", modifiers=False)
    base = zext(base, 64) if base.w < 64 else base
    if i.has_operand("stride"):
        base = base * i.select_lookup_const("stride", {"X1": 1, "X4": 4, "X8": 8, "X16": 16}, 64)
    uniform = next((n for n in ("Ra_URb", "Ra_URc") if i.has_operand(n) and not i.has_operand("memoryDescriptor")), None)
    if uniform: base = base + zext(i.value(uniform, modifiers=False), 64)
    if not i.has_operand("Ra_offset|Sa_offset"): return base
    offset = i.resolve("Ra_offset|Sa_offset")
    return base + (sext if i.atom(offset).type == "SImm" else zext)(i.imm(offset), 64)


def windows(i, space, addr):
    """[(space, offset, inside)] for the windows a generic address is tried against, in order; then
    (space, address) of an access outside them all."""
    if space != "generic": return [], (space, addr)
    offsets = [(s, addr - concat(i.special_register(hi), i.special_register(lo)), size) for s, lo, hi, size in WINDOWS]
    return [(s, a, a < zext(i.special_register(size), 64)) for s, a, size in offsets], ("ram", addr)


@family("LDG", "STG", "LDS", "STS", "LDL", "STL", "LDC", "LDCU", "LD", "ST")
def memory(i):
    """Rd = sz bits at the address (sign- or zero-extended), or store Rb's low sz bits there; LDC
    (LDCU into URd) reads constant bank Sa_bank at the low 32 bits of the address. LD/ST route a
    generic address through the shared and local windows."""
    space = SPACES[i.opcode]
    for n in ("Pu", "Pv", "Pq"):   # predicate results are not assumed to mean success
        if i.has_operand(n) and not i.is_zero(n): unmodelled(f"{n} result")
    effects = [ordering(i), *descriptor(i)]
    addr = address(i)
    if space == "generic" and i.has_operand("e") and i.select_fork("e") == "noe": unmodelled("32-bit generic address")
    if space == "cbank":
        if not i.has_operand("Sa_bank"): unmodelled("bank")
        addr = (zext(i.imm("Sa_bank"), 64) << 32) | zext(trunc(addr, 32), 64)
    w, signed = size(i)
    if i.opcode.startswith("LD"):
        if w > i.width("Rd"): unmodelled("size")
        tried, (s, a) = windows(i, space, addr)
        loaded = load(s, a, w)
        for s, a, inside in reversed(tried): loaded = ite(inside, load(s, a, w), loaded)
        # A false Pnz gives 0 without accessing memory (SM89 GPU: an unmapped address doesn't trap).
        if i.has_operand("Pnz"): loaded = ite(i.pred("Pnz"), loaded, const(0, w))
        # The md also writes Pnz; SM89 hardware leaves its value unchanged.
        kept = {"Pnz": i.pred("Pnz", modifiers=False)} if i.has_operand("Pnz") else {}
        return outputs(effects=effects, Rd=extended(loaded, i.width("Rd"), signed), **kept)
    value = i.value("Rb", modifiers=False)
    if w > value.w: unmodelled("size")
    value = trunc(value, w) if value.w > w else value
    tried, (s, a) = windows(i, space, addr)
    stores, outside = [], None
    for ws, wa, inside in tried:
        stores.append(store(ws, wa, value, inside if outside is None else outside & inside))
        outside = ~inside if outside is None else outside & ~inside
    return outputs(effects=effects, stores=[*stores, store(s, a, value, outside)])


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
