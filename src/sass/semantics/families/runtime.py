"""Operations that need runtime primitives (src/sass/semantics/primitive.py): hardware
approximations (MUFU), warp collectives (SHFL, VOTE/VOTEU, MATCH, REDUX, ELECT), synchronization
and convergence barriers, the QSPC address-space query."""
from sass.semantics import primitive
from sass.semantics.bitvec import concat, const, extract, sext, sle, trunc, zext
from sass.semantics.family import compound, family, outputs, unmodelled
from sass.semantics.families.integer import leading_zeros


def half(i, name, h):
    """Half h of operand `name` (an immediate's low 16 bits), with |.| and - applied to it."""
    x = i.value(name, modifiers=False)
    v = trunc(x, 16) if i.kind(name) in ("immediate", "float-immediate") else extract(x, 16 * h + 15, 16 * h)
    v = i.modified(name, "absolute", v, lambda y: y & 0x7fff)
    return i.modified(name, "negate", v, lambda y: y ^ 0x8000)


@family("MUFU")
def mufu(i):
    """Rd = MUFU.fn of the source after |.| and -: a hardware approximation, the runtime primitive
    mufu_<fn> (SIN/COS: the recovered SM89 model, src/sass/emulate/include/sass/mufu.hpp; the 64H forms on a
    double's high word). The 16-bit forms read the H0/H1 half and zero Rd's high half (SM89 GPU);
    the SIMD form (SM103+) does both halves, Sb the high one."""
    fn = str(i.select_fork("mufuop|op"))
    if fn not in primitive.MUFU: unmodelled(f"MUFU.{fn}")
    src = i.resolve("Rb|Sb|URb")
    if i.cls.startswith("mufu__"):
        x = extract(i.fvalue(src), 63, 32) if fn.endswith("64H") and i.kind(src) == "float-immediate" else i.fvalue(src, width=32)
        return outputs(Rd=primitive.mufu(fn, x))
    fmt = str(i.select_fork("fmt|width")).removesuffix("x2")
    if fmt not in primitive.HALVES: unmodelled(f"MUFU.{fmt}")
    if i.cls.startswith("mufu_fp16_simd"):
        hi, lo = (half(i, "Sb", 0), half(i, "Sb1", 0)) if i.has_operand("Sb1") else (half(i, src, 1), half(i, src, 0))
        return outputs(Rd=concat(primitive.mufu(fn, hi, fmt), primitive.mufu(fn, lo, fmt)))
    h = str(i.select_fork("extract")) if i.has_operand("extract") else "H0"
    if h not in ("H0", "H1"): unmodelled(f"extract {h}")
    return outputs(Rd=zext(primitive.mufu(fn, half(i, src, int(h[1])), fmt), 32))


@family("SHFL")
def shfl(i):
    """Rd = Ra as lane `target` holds it (PTX shfl.sync; SM89 GPU, 524288 lane results with random
    b and c): c's bits 12:8 mask the lane's segment and its bits 4:0 clamp it, Pu = the source is
    in bounds (UP's bound is a lower one). A valid source that isn't executing gives 0, an invalid
    one this lane's Ra."""
    lane, a = primitive.lane(), i.value("Ra", modifiers=False)
    b, c = i.value("Rb|Sb") & 31, i.value("Rc|Sc")
    segment = (c >> 8) & 31
    bound = (lane & segment) | (c & 31 & ~segment)
    match i.select_fork("shflmd"):
        case "IDX": target, valid = (lane & segment) | (b & ~segment), None
        case "UP": target = lane - b; valid = sle(bound, target)
        case "DOWN": target, valid = lane + b, None
        case "BFLY": target, valid = lane ^ b, None
    valid = target <= bound if valid is None else valid
    live = ((primitive.active() >> target) & 1) != 0
    return outputs(Rd=(primitive.read_lane(a, target) if live else const(0, 32)) if valid else a, Pu=valid)


@family("VOTE", "VOTEU")
def vote(i):
    """Rd = the ballot: the executing lanes whose Pp (with its !) holds; Pu = ALL: every executing
    lane's does, ANY: some lane's, EQ: all or none (PTX vote.sync)."""
    active = primitive.active()
    ballot = primitive.ballot(i.pred("Pp", modifiers=False))
    if i.operand_notted("Pp"): ballot = active & ~ballot
    match i.select_fork("voteop"):
        case "ALL": r = ballot == active
        case "ANY": r = ballot != 0
        case "EQ": r = (ballot == 0) | (ballot == active)
        case other: unmodelled(f"voteop {other}")
    return outputs(Rd=ballot, Pu=r)


@family("MATCH")
def match(i):
    """ANY: Rd = the executing lanes whose Ra (32 or 64 bits by sz) equals this lane's; ALL: Pu =
    every executing lane's does, and Rd is the executing lanes then, else 0 (PTX match.sync)."""
    i.select_fork("sz")   # Ra's span
    equal = primitive.match(i.value("Ra", modifiers=False))
    if i.select_fork("op") == "ANY": return outputs(Rd=equal)
    active = primitive.active()
    ok = equal == active
    return outputs(Rd=active if ok else const(0, 32), Pu=ok)


@family("REDUX")
def redux(i):
    """URd = Ra combined over the executing lanes: AND, OR, XOR, SUM (wrapping), MIN/MAX (signed
    with .S32) (PTX redux.sync)."""
    sz = str(i.select_fork("sz"))
    if sz not in ("U32", "S32"): unmodelled(f"sz {sz}")
    x = i.value("Ra", modifiers=False)
    match str(i.select_fork("op")):
        case "AND" | "OR" | "XOR" as op: r = primitive.redux(op.lower(), x)
        case "SUM": r = primitive.redux("add", x)
        case "MIN" | "MAX" as op: r = primitive.redux(sz[0].lower() + op.lower(), x)
        case other: unmodelled(f"op {other}")
    return outputs(URd=r)


TAGS = {"G": 1, "S": 2, "L": 4, "D": 8}


def address64(i, name):
    """An address operand as 64 bits: a register zero-extended, an offset sign-extended if signed."""
    x = i.value(name)
    return x if x.w == 64 else (sext if i.atom(name).type == "SImm" else zext)(x, 64)


@family("QSPC")
def qspc(i):
    """Pu = whether the generic address Ra (+ Ra_URb) + Ra_offset is in the queried space, Rd its
    space's one-hot tag (G=1, S=2, L=4, SM89 GPU), from the environment's address-space map. .D is
    modelled only with Rd = RZ (its nonzero-Rd tag is not hardware-verified)."""
    for n in ("e", "input_reg_sz_32_dist", "input_reg_sz_64_dist"):
        if i.has_operand(n): i.select_fork(n)
    space = str(i.select_fork("space"))
    if not (space in ("G", "L", "S") or space == "D" and i.is_zero("Rd")): unmodelled(f"space {space}")
    address = address64(i, "Ra") + address64(i, "Ra_offset")
    if i.has_operand("Ra_URb"): address = address + address64(i, "Ra_URb")
    tag = primitive.address_space(address)
    return outputs(Pu=(tag & TAGS[space]) != 0, Rd=tag)


@family("ELECT")
def elect(i):
    """URd = the leader, the lowest executing lane of URa's member mask (~URa with its invert bit),
    Pu = this lane leads (PTX elect.sync, undefined without an executing member: URd is 0 then;
    no hardware here). The Pp form and .IGNOREKILL are unmodelled."""
    if i.has_operand("Pp"): unmodelled("Pp form")
    if i.select_fork("ignoreKill") != "noignore_kill": unmodelled("IGNOREKILL")
    members = primitive.active() & i.value("URa")
    lowest = members & -members
    return outputs(URd=31 - leading_zeros(lowest), Pu=lowest == const(1, 32) << primitive.lane())


def bracketed(k):
    """The operands inside a compound operand's brackets (c[bank][address], cx[header][offset]):
    parts of its value."""
    out, f = set(), k.format
    for j, a in enumerate(f):
        if a.kind != "operand" or a.name not in compound(k): continue
        for b in f[j + 1:]:
            if b.kind == "lit" and b.name == ",": break
            if b.kind == "operand": out.add(b.name)
    return out


def synchronization_inputs(i, skip):
    """The inputs of a synchronization primitive, in md order: a selector's encoding (one with a
    single label is asked, not passed), an operand's value, a branch operand's target."""
    out, parts = [], bracketed(i.k)
    for n, a in i.k.operand_types.items():
        if n in skip or n in parts: continue
        match i.kind(n):
            case "selector" if len(set(i.arch.enums[a.type].values())) == 1: i.select_fork(n)
            case "selector": out.append(i.encoding(n))
            case "predicate": out.append(zext(i.pred(n), 32))
            case "immediate" if a.type == "RSImm": out.append(i.branch_target(n))
            case "register" | "uniform" | "immediate" | "constant" | "cx": out.append(i.value(n))
            case other: unmodelled(f"{other} operand {n}")
    return out


@family("BAR", "BSSY", "BSYNC", "BREAK", "WARPSYNC", "BMOV")
def synchronization(i):
    """The environment's synchronization event (bar, bssy, ...: acknowledged, not a scheduler),
    enabled by Pp, with every other input; BMOV to Rd instead reads the environment's
    convergence-barrier state cbu_state. Barrier results and reductions are the environment's."""
    if i.has_operand("Rd"):
        return outputs(Rd=primitive.bmov(i.encoding("cbu_state"), *synchronization_inputs(i, ("Pg", "Rd", "cbu_state"))))
    enable = i.pred("Pp") if i.has_operand("Pp") else const(1, 1)
    return outputs(effects=[primitive.synchronize(i.opcode.lower(), enable, *synchronization_inputs(i, ("Pg", "Pp")))])
