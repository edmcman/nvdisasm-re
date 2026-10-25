"""Warp collective references over a whole 32-lane warp, from the PTX ISA definitions of
shfl.sync, vote.sync, match.sync, redux.sync and elect.sync (all lanes active)."""


def shfl(mode, a, b, c):
    """shfl.sync.mode: lane l receives a[j] and ok, j from b[l] and c[l] (clamp c[4:0], segment
    mask c[12:8]); an out-of-range j gives the lane's own a and ok 0."""
    out = []
    for lane in range(32):
        bval, clamp, segment = b[lane] & 31, c[lane] & 31, c[lane] >> 8 & 31
        low = lane & segment
        bound = low | (clamp & ~segment)
        match mode:
            case "idx": j, ok = low | (bval & ~segment), None
            case "up": j, ok = lane - bval, lane - bval >= bound
            case "down": j, ok = lane + bval, None
            case "bfly": j, ok = lane ^ bval, None
        ok = j <= bound if ok is None else ok
        out.append((a[j] if ok else a[lane], int(ok)))
    return out


def vote(predicates):
    """vote.sync ballot, any, all, uni of 32 predicates."""
    ballot = sum(bool(p) << lane for lane, p in enumerate(predicates))
    return ballot, int(ballot != 0), int(ballot == 0xffffffff), int(ballot in (0, 0xffffffff))


def vote_members(predicates, members):
    """vote.sync ballot, any, all over the member lanes only (members: iterable of lane numbers)."""
    ballot = sum(bool(predicates[lane]) << lane for lane in members)
    return ballot, int(ballot != 0), int(all(predicates[lane] for lane in members))


def match_any(values):
    """match.any.sync: per lane, the mask of lanes holding the same value."""
    return [sum(1 << j for j, y in enumerate(values) if y == x) for x in values]


def match_all(values):
    """match.all.sync (d, p): the full mask and true if every lane holds the same value, else 0 and false."""
    same = len(set(values)) == 1
    return 0xffffffff if same else 0, int(same)


def redux(op, values, signed=False):
    """redux.sync: the 32-bit reduction of every lane's value (min/max signed with .s32)."""
    xs = [x - (1 << 32) if signed and x >> 31 else x for x in values]
    match op:
        case "add": r = sum(xs)
        case "min": r = min(xs)
        case "max": r = max(xs)
        case "and" | "or" | "xor":
            r = xs[0]
            for x in xs[1:]: r = {"and": r & x, "or": r | x, "xor": r ^ x}[op]
    return r & 0xffffffff


def elect(mask):
    """elect.sync: the lowest lane of the member mask leads; (its lane id, per-lane is-leader)."""
    leader = (mask & -mask).bit_length() - 1
    return leader, [int(lane == leader) for lane in range(32)]
