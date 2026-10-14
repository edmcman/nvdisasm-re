"""Warp collective references over a whole 32-lane warp, from the PTX ISA definitions of
shfl.sync and vote.sync (all lanes active)."""


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
