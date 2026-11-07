"""Numerical approximations, ordered warp collectives and environment operations."""
from sass.semantics import ordered, primitive
from sass.semantics.bitvec import concat, const, extract, ite, sext, sle, trunc, zext
from sass.semantics.family import family, unmodelled
from sass.semantics.families.integer import leading_zeros


def half(i, name, h):
    """Half h of operand `name` (an immediate's low 16 bits), with |.| and - applied to it."""
    x = i.value(name, modifiers=False)
    v = trunc(x, 16) if i.kind(name) in ("immediate", "float-immediate") else extract(x, 16 * h + 15, 16 * h)
    v = i.modified(name, "absolute", v, lambda y: y & 0x7fff)
    return i.modified(name, "negate", v, lambda y: y ^ 0x8000)


@family("MUFU")
def mufu(i):
    """Rd = MUFU.fn of the source after |.| and -: an argument-only hardware approximation,
    mufu_<fn> (SIN/COS: the recovered SM89 model, src/sass/emulate/include/sass/mufu.hpp; the 64H forms on a
    double's high word). The 16-bit forms read the H0/H1 half and zero Rd's high half (SM89 GPU);
    the SIMD form (SM103+) does both halves, Sb the high one."""
    fn = str(i.select_fork("mufuop|op"))
    if fn not in primitive.MUFU: unmodelled(f"MUFU.{fn}")
    src = i.resolve("Rb|Sb|URb")
    if i.cls.startswith("mufu__"):
        x = extract(i.fvalue(src), 63, 32) if fn.endswith("64H") and i.kind(src) == "float-immediate" else i.fvalue(src, width=32)
        return i.assign_from_inputs(Rd=primitive.mufu(fn, x))
    fmt = str(i.select_fork("fmt|width")).removesuffix("x2")
    if fmt not in primitive.HALVES: unmodelled(f"MUFU.{fmt}")
    if i.cls.startswith("mufu_fp16_simd"):
        hi, lo = (half(i, "Sb", 0), half(i, "Sb1", 0)) if i.has_operand("Sb1") else (half(i, src, 1), half(i, src, 0))
        return i.assign_from_inputs(Rd=concat(primitive.mufu(fn, hi, fmt), primitive.mufu(fn, lo, fmt)))
    h = str(i.select_fork("extract")) if i.has_operand("extract") else "H0"
    if h not in ("H0", "H1"): unmodelled(f"extract {h}")
    return i.assign_from_inputs(Rd=zext(primitive.mufu(fn, half(i, src, int(h[1])), fmt), 32))


@family("SHFL")
def shfl(i):
    """Rd = Ra as lane `target` holds it (PTX shfl.sync; SM89 GPU, 524288 lane results with random
    b and c): c's bits 12:8 mask the lane's segment and its bits 4:0 clamp it, Pu = the source is
    in bounds (UP's bound is a lower one). A valid source that isn't executing gives 0, an invalid
    one this lane's Ra."""
    body = i.body()
    lane, = body.special(ordered.LANE)
    active = i.active_lanes(body)
    a, b, c = body.capture_inputs((i.value("Ra", modifiers=False), i.value("Rb|Sb") & 31, i.value("Rc|Sc")))
    segment = (c >> 8) & 31
    bound = (lane & segment) | (c & 31 & ~segment)
    mode = i.select_fork("shflmd")
    match mode:
        case "IDX": target = (lane & segment) | (b & ~segment)
        case "UP": target = lane - b
        case "DOWN": target = lane + b
        case "BFLY": target = lane ^ b
    target = body.move(target)
    valid = body.move(sle(bound, target) if mode == "UP" else target <= bound)
    live = ((active >> target) & 1) != 0
    snapshot, = body.special(ordered.CAPTURE, lane, active, a)
    result, = body.if_else(valid & live,
                           lambda arm: arm.special(ordered.READ_LANE, snapshot, target),
                           lambda arm: (ite(valid, const(0, 32), a),))
    body.special(ordered.RELEASE_SNAPSHOT, snapshot)
    return i.assign_from_inputs(body, Rd=result, Pu=valid)


@family("VOTE", "VOTEU")
def vote(i):
    """Rd = the ballot: the executing lanes whose Pp (with its !) holds; Pu = ALL: every executing
    lane's does, ANY: some lane's, EQ: all or none (PTX vote.sync)."""
    body = i.body()
    lane, = body.special(ordered.LANE)
    active = i.active_lanes(body)
    predicate = body.capture(i.pred("Pp", modifiers=False))
    snapshot, = body.special(ordered.CAPTURE_PREDICATE, lane, active, predicate)
    ballot, = body.special(ordered.BALLOT, snapshot)
    body.special(ordered.RELEASE_SNAPSHOT, snapshot)
    if i.operand_notted("Pp"): ballot = active & ~ballot
    match i.select_fork("voteop"):
        case "ALL": r = ballot == active
        case "ANY": r = ballot != 0
        case "EQ": r = (ballot == 0) | (ballot == active)
        case other: unmodelled(f"voteop {other}")
    return i.assign_from_inputs(body, Rd=ballot, Pu=r)


@family("MATCH")
def match(i):
    """ANY: Rd = the executing lanes whose Ra (32 or 64 bits by sz) equals this lane's; ALL: Pu =
    every executing lane's does, and Rd is the executing lanes then, else 0 (PTX match.sync)."""
    i.select_fork("sz")   # Ra's span
    body = i.body()
    lane, = body.special(ordered.LANE)
    active = i.active_lanes(body)
    source = body.capture(i.value("Ra", modifiers=False))
    wide = source.w == 64
    snapshot, = body.special(ordered.CAPTURE64 if wide else ordered.CAPTURE, lane, active, source)
    equal, = body.special(ordered.MATCH64 if wide else ordered.MATCH, snapshot, lane)
    body.special(ordered.RELEASE_SNAPSHOT64 if wide else ordered.RELEASE_SNAPSHOT, snapshot)
    if i.select_fork("op") == "ANY": return i.assign_from_inputs(body, Rd=equal)
    ok = equal == active
    return i.assign_from_inputs(body, Rd=active if ok else const(0, 32), Pu=ok)


@family("REDUX")
def redux(i):
    """URd = Ra combined over the executing lanes: AND, OR, XOR, SUM (wrapping), MIN/MAX (signed
    with .S32) (PTX redux.sync)."""
    sz = str(i.select_fork("sz"))
    if sz not in ("U32", "S32"): unmodelled(f"sz {sz}")
    match str(i.select_fork("op")):
        case "AND" | "OR" | "XOR" as op: operation = op.lower()
        case "SUM": operation = "add"
        case "MIN" | "MAX" as op: operation = sz[0].lower() + op.lower()
        case other: unmodelled(f"op {other}")
    body = i.body()
    lane, = body.special(ordered.LANE)
    active = i.active_lanes(body)
    source = body.capture(i.value("Ra", modifiers=False))
    snapshot, = body.special(ordered.CAPTURE, lane, active, source)
    result, = body.special(ordered.REDUCTIONS[operation], snapshot)
    body.special(ordered.RELEASE_SNAPSHOT, snapshot)
    return i.assign_from_inputs(body, URd=result)


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
    body = i.body()
    tag, = body.special(ordered.ADDRESS_SPACE, body.capture(address))
    return i.assign_from_inputs(body, Pu=(tag & TAGS[space]) != 0, Rd=tag)


@family("ELECT")
def elect(i):
    """URd = the leader, the lowest executing lane of URa's member mask (~URa with its invert bit),
    Pu = this lane leads (PTX elect.sync, undefined without an executing member: URd is 0 then;
    no hardware here). The Pp form and .IGNOREKILL are unmodelled."""
    if i.has_operand("Pp"): unmodelled("Pp form")
    if i.select_fork("ignoreKill") != "noignore_kill": unmodelled("IGNOREKILL")
    body = i.body()
    active = i.active_lanes(body)
    mask = body.capture(i.value("URa"))
    members = active & mask
    lowest = members & -members
    lane, = body.special(ordered.LANE)
    return i.assign_from_inputs(body, URd=31 - leading_zeros(lowest), Pu=lowest == const(1, 32) << lane)


def supported_barrier(k):
    return k.name.startswith(("bar__SYNC_", "bar__RED_", "bar__ARV_")) and \
           not {"Rb", "Rc"} <= k.operand_types.keys()


@family("BAR", classes=supported_barrier)
def barrier(i):
    body = i.body()
    mode = str(i.select_fork("barmode"))
    deferred = i.has_operand("defer_blocking")
    if deferred: i.select_fork("defer_blocking")
    reduction = 0
    if mode == "RED":
        match str(i.select_fork("op")):
            case "AND": reduction = 1
            case "OR": reduction = 2
            case "POPC": reduction = 3
            case other: unmodelled(f"reduction {other}")
    predicate = body.capture(i.pred("Pp")) if i.has_operand("Pp") else const(1, 1)
    count_name = i.resolve("Sc|Rc")
    id, count = body.capture_inputs((i.value("Sb|Rb"), i.value(count_name)))
    if i.kind(count_name) == "immediate":
        ticket, = body.special(ordered.BLOCK_ARRIVE_IMPLICIT, id, predicate, const(reduction, 32)) \
            if i.is_zero(count_name) else body.special(ordered.BLOCK_ARRIVE_COUNTED, id, count, predicate, const(reduction, 32))
    else:
        ticket, = body.if_else(count == 0,
            lambda arm: arm.special(ordered.BLOCK_ARRIVE_IMPLICIT, id, predicate, const(reduction, 32)),
            lambda arm: arm.special(ordered.BLOCK_ARRIVE_COUNTED, id, count, predicate, const(reduction, 32)))
    if mode != "ARV": body.special(ordered.BLOCK_DEFER if deferred else ordered.BLOCK_WAIT_READY, ticket)
    body.special(ordered.BLOCK_RELEASE, ticket)
    return body.block()


def supported_warp_sync(k):
    return k.name in ("warpsync__RIR", "warpsync__RRR", "warpsync__RUR")


@family("WARPSYNC", classes=supported_warp_sync)
def warpsync(i):
    arrival = ordered.WARP_ARRIVE
    for name in ("ex", "div"):
        if i.has_operand(name):
            match str(i.select_fork(name)):
                case "noexclusive" | "nodiv__exclusive": pass
                case "EXCLUSIVE": arrival = ordered.WARP_ARRIVE_EXCLUSIVE
                case _: unmodelled("warp synchronization selector")
    all_lanes = i.has_operand("all")
    if all_lanes and i.select_fork("all") != "ALL": unmodelled("warp synchronization selector")
    body = i.body()
    enabled = body.capture(i.pred("Pp"))
    mask = const(0xffffffff, 32) if all_lanes else body.capture(i.value("Ra|Rb|URb|Sb", modifiers=False))

    def arrive(arm):
        ticket, = arm.special(arrival, mask)
        arm.special(ordered.WARP_WAIT, ticket)
        arm.special(ordered.WARP_RELEASE, ticket)
        return ()

    body.if_else(enabled, arrive, lambda arm: ())
    return body.block()


@family("BSSY", "BSYNC")
def convergence(i):
    body = i.body()
    enabled = body.capture(i.pred("Pp"))
    coordinated, = body.special(ordered.COORDINATED)
    id = i.encoding("barReg")
    entering = i.opcode == "BSSY"
    target = i.branch_target("Sa") if entering else None
    if i.has_operand("rel"): i.select_fork("rel")
    mode = str(i.select_fork("reliability")) if i.has_operand("reliability") else None
    enter = ordered.CONVERGENCE_ENTER_MODES[mode] if mode else ordered.CONVERGENCE_ENTER
    arrive = ordered.CONVERGENCE_ARRIVE_MODES[mode] if mode else ordered.CONVERGENCE_ARRIVE

    def cooperative(arm):
        if entering:
            lane, = arm.special(ordered.LANE)
            active = i.active_lanes(arm)
            snapshot, = arm.special(ordered.CAPTURE_PREDICATE, lane, active, enabled)
            members, = arm.special(ordered.BALLOT, snapshot)
            arm.special(ordered.RELEASE_SNAPSHOT, snapshot)
            arm.if_else(enabled, lambda selected: selected.special(enter, id, target, members), lambda selected: ())
        else:
            def wait(selected):
                ticket, = selected.special(arrive, id)
                selected.special(ordered.CONVERGENCE_WAIT, ticket)
                selected.special(ordered.CONVERGENCE_RELEASE, ticket)
                return ()
            arm.if_else(enabled, wait, lambda selected: ())
        return ()

    def standalone(arm):
        arguments = (id, target) if entering else (id,)
        if mode:
            spec = ordered.CONVERGENCE_ACK_ENTER_MODE if entering else ordered.CONVERGENCE_ACK_WAIT_MODE
            arguments = (i.encoding("reliability"), *arguments)
        else:
            spec = ordered.CONVERGENCE_ACK_ENTER if entering else ordered.CONVERGENCE_ACK_WAIT
        arm.if_else(enabled, lambda selected: selected.special(spec, *arguments), lambda selected: ())
        return ()

    body.if_else(coordinated, cooperative, standalone)
    return body.block()


@family("BREAK")
def convergence_break(i):
    body = i.body()
    if i.has_operand("reliability"): i.select_fork("reliability")
    enabled = body.capture(i.pred("Pp"))
    body.if_else(enabled, lambda arm: arm.special(ordered.CONVERGENCE_BREAK, i.encoding("barReg")),
                 lambda arm: ())
    return body.block()


@family("BMOV")
def convergence_move(i):
    body = i.body()
    i.select_fork("sz")
    match i.cls:
        case "bmov_clear__Rd":
            clear = str(i.select_fork("clear")) == "CLEAR"
            result, = body.special(ordered.CONVERGENCE_READ_CLEAR if clear else ordered.CONVERGENCE_READ,
                                   i.encoding("cbu_state"))
            return i.assign_from_inputs(body, Rd=result)
        case "bmov_clear_barrier_":
            i.select_fork("clear")
            body.special(ordered.CONVERGENCE_COPY, i.encoding("barReg"), i.encoding("Ba"))
        case "bmov_clear_bd__Bd":
            body.special(ordered.CONVERGENCE_IMPORT, i.encoding("barReg"), i.encoding("cbu_state"))
        case name if name.startswith("bmov_dst64__"):
            i.select_fork("atexit_pc")
            body.special(ordered.CONVERGENCE_EXIT_PC, body.capture(zext(i.value("Sb|Rb|URb"), 64)))
        case name if name.startswith("bmov_pquad"):
            mode, state = i.encoding("pquad"), i.encoding("cbu_state")
            if i.has_operand("barReg"):
                body.special(ordered.CONVERGENCE_WRITE_BARRIER, mode, state, i.encoding("barReg"))
            else:
                body.special(ordered.CONVERGENCE_WRITE, mode, state, body.capture(i.value("Sb|Rb|URb")))
        case _: unmodelled("convergence state move")
    return body.block()


@family("BAR", "WARPSYNC",
        classes=lambda k: (k.mnemonic != "BAR" or not supported_barrier(k)) and
                          (k.mnemonic != "WARPSYNC" or not supported_warp_sync(k)))
def synchronization(i):
    """Unresolved barrier forms retain their explicit standalone environment boundary."""
    body = i.body()
    if i.opcode == "BAR":
        mode = str(i.select_fork("barmode"))
        deferred = i.has_operand("defer_blocking")
        if deferred: i.select_fork("defer_blocking")
        match mode:
            case "RED":
                arguments = body.capture_inputs((i.encoding("op"), i.value("Rb"), i.value("Rc"), i.pred("Pp")))
                body.special(ordered.BARRIER_ACKS["packed_reduce_defer" if deferred else "packed_reduce"], *arguments)
            case "SYNC" | "ARV":
                arguments = body.capture_inputs((i.value("Rb"), i.value("Rc")))
                name = "packed_arrive" if mode == "ARV" else "packed_defer" if deferred else "packed_wait"
                body.special(ordered.BARRIER_ACKS[name], *arguments)
            case "SYNCALL": body.special(ordered.BARRIER_ACKS["all_defer" if deferred else "all"])
            case "SCAN":
                enabled = body.capture(i.pred("Pp"))
                def scan(arm):
                    return arm.special(ordered.BARRIER_ACKS["scan"],
                                       *arm.capture_inputs((i.value("Sb|Rb"), i.value("Sc|Rc"))))
                body.if_else(enabled, scan, lambda arm: ())
            case _: unmodelled("barrier acknowledgement")
        return body.block()

    enabled = body.capture(i.pred("Pp"))
    if i.has_operand("div"): i.select_fork("div")
    all_lanes = i.has_operand("all")
    if all_lanes: i.select_fork("all")

    def acknowledge(arm):
        if i.has_operand("sImm"):
            arguments = () if all_lanes else (arm.capture(i.value("Ra")),)
            if i.has_operand("rel"):
                arguments += arm.capture_inputs((i.value("sImm"), i.encoding("rel")))
                name = "relative_all" if all_lanes else "relative_mask"
            else:
                arguments += (arm.capture(i.branch_target("sImm")),)
                name = "collective_all" if all_lanes else "collective_mask"
        else:
            arguments, name = (arm.capture(i.value("Sb")),), "mask"
        return arm.special(ordered.WARP_ACKS[name], *arguments)

    body.if_else(enabled, acknowledge, lambda arm: ())
    return body.block()


@family("B2R")
def b2r(i):
    body = i.body()
    match str(i.select_fork("mode")):
        case "BAR":
            result, = body.special(ordered.BLOCK_STATE, body.capture(i.value("barname")))
            return i.assign_from_inputs(body, Rd=result)
        case "WARP":
            result, = body.special(ordered.BLOCK_RESULT)
            return i.assign_from_inputs(body, Rd=result & 0x7ff)
        case "RESULT":
            result, = body.special(ordered.BLOCK_RESULT)
            return i.assign_from_inputs(body, Rd=result, Pu=result != 0)
