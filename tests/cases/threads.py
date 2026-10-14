"""Semantics involving several threads: SHFL and VOTE with explicit warp context (emulator cases), and
cooperative blocks (warps in lockstep, shared memory, BAR.SYNC) against the SM89 GPU (GPU fixtures)."""
import random
import struct


def emulator_cases(c):
    warp = dict(values=list(range(32)), predicates=[i % 2 for i in range(32)], lane=3, active=0xffffffff)
    c.add("shfl__RRR", dict(Rd=8, Pu=0, Ra=2, Rb=4, Rc=6, shflmd="IDX"), dict(R2=3, R4=5, R6=31), dict(R8=5, P0=1), context={"warp": warp})
    c.add("shfl__RRR", dict(Rd=8, Pu=0, Ra=2, Rb=4, Rc=6, shflmd="IDX"), dict(R2=3, R4=5, R6=31), dict(error="inactive shuffle source"),
          context={"warp": dict(warp, active=0xffffffdf)})
    # SM89 GPU results (lane, b, c -> source lane, Pu): UP's bound is the segment start, BFLY may
    # leave the segment downward, b's bits above 4 are ignored.
    for mode, lane, b, cval, src, ok in [("UP", 13, 35, 0x0, 10, 1), ("UP", 29, 26, 0x4, 29, 0),
                                         ("BFLY", 25, 25, 0x100, 0, 1), ("BFLY", 25, 22, 0x1f10, 15, 1), ("BFLY", 1, 41, 0x0, 1, 0),
                                         ("DOWN", 3, 5, 0xc, 8, 1), ("DOWN", 1, 41, 0x0, 1, 0), ("IDX", 9, 2227890262, 0x181f, 14, 1)]:
        c.add("shfl__RRR", dict(Rd=8, Pu=0, Ra=2, Rb=4, Rc=6, shflmd=mode), dict(R2=lane, R4=b, R6=cval),
              dict(R8=src, P0=ok), context={"warp": dict(warp, lane=lane)})
    c.add("vote_", dict(Rd=8, Pu=0, Pp=1, voteop="ANY"), dict(P1=1), dict(R8=0xaaaaaaaa, P0=1), context={"warp": warp})
    from tests.refs import warp as ref
    # Inactive lanes take no part: the context's active mask, not the reference's whole warp.
    half = dict(warp, active=0x0000ffff)
    for ctx, predicates in ((warp, warp["predicates"]), (half, warp["predicates"][:16] + [0] * 16)):
        ballot, any_, all_, uni = ref.vote(predicates)
        full = ctx["active"] == 0xffffffff
        for mode, result in (("ANY", any_), ("ALL", all_ if full else int(ballot == ctx["active"])), ("EQ", uni if full else int(ballot in (0, ctx["active"])))):
            c.add("voteu_", dict(URd=8, UPu=0, Pp=1, voteop=mode), dict(P1=1), dict(UR8=ballot, UP0=result), context={"warp": ctx})
        c.add("voteu_", dict(URd=8, UPu=0, Pp=1, voteop="ANY", **{"Pp@not": 1}), dict(P1=1), dict(UR8=ctx["active"] & ~ballot, UP0=1), context={"warp": ctx})
    values = [l % 3 for l in range(32)]
    for ctx in (warp, half):
        lanes = [v if ctx["active"] >> l & 1 else None for l, v in enumerate(values)]
        c.add("match__ANY", dict(Rd=8, Ra=2), dict(R2=values[3]), dict(R8=sum(1 << l for l, v in enumerate(lanes) if v == values[3])),
              context={"warp": dict(ctx, values=values)})
        for ws, same in ((values, 0), ([7] * 32, 1)):
            c.add("match__ALL", dict(Pu=0, Rd=8, Ra=2), dict(R2=ws[3]), dict(R8=ctx["active"] if same else 0, P0=same), context={"warp": dict(ctx, values=ws)})
    wide = [(l % 2) << 32 | 5 for l in range(32)]
    c.add("match__ANY", dict(Rd=8, Ra=2, sz="U64"), dict(R2=5, R3=1), dict(R8=ref.match_any(wide)[3]), context={"warp": dict(warp, values=wide)})
    words = [(0x9e3779b9 * l) & 0xffffffff for l in range(32)]
    for op, label, signed in (("add", "SUM", False), ("and", "AND", False), ("or", "OR", False), ("xor", "XOR", False),
                              ("min", "MIN", False), ("max", "MAX", False), ("min", "MIN", True), ("max", "MAX", True)):
        for ctx in (warp, half):
            want = ref.redux(op, [x for l, x in enumerate(words) if ctx["active"] >> l & 1], signed)
            c.add("redux_", dict(URd=8, Ra=2, op=label, sz="S32" if signed else "U32"), dict(R2=words[3]), dict(UR8=want),
                  context={"warp": dict(ctx, values=words)})
    # ELECT (SM90+): the lowest active member of URa's mask (~URa with the invert bit) leads.
    for mask, invert, lane in ((0xffffffff, 0, 0), (0xfff00000, 0, 20), (0xfff00000, 1, 3), (0x00f00000, 0, 20)):
        leader, flags = ref.elect((~mask if invert else mask) & 0xffffffff)
        c.add("elect_", dict(Pu=0, URd=8, URa=4, **{"URa@invert": invert}), dict(UR4=mask), dict(P0=flags[lane], UR8=leader),
              context={"warp": dict(warp, lane=lane)})
    c.add("elect_Pp_", dict(Pu=0, URd=8, Pp=1), {}, dict(error="sass_opaque_ELECT"), context={"warp": warp})


WARPS = 16


def shfl_inputs(rng):
    """Per warp: random values; b per lane, shared or with high bits; c the width encodings ptxas
    emits (UP: clamp 0), an arbitrary clamp/segment pair, or a random word."""
    out = []
    for w in range(WARPS):
        width = 1 << w % 6
        shared = rng.randrange(64)
        for lane in range(32):
            b = [shared, rng.randrange(64), rng.getrandbits(32)][w % 3]
            c = [(32 - width) << 8 | 31, (32 - width) << 8, rng.randrange(32) | rng.randrange(32) << 8, rng.getrandbits(32)][w // 4 % 4]
            out.append((rng.getrandbits(32), b, c))
    return out


def vote_inputs(rng):
    """Per warp: all false, all true, one lane, all but one, random."""
    out = []
    for w in range(WARPS):
        one = rng.randrange(32)
        bits = [[0] * 32, [1] * 32, [int(l == one) for l in range(32)], [int(l != one) for l in range(32)]][w] if w < 4 else [rng.getrandbits(1) for _ in range(32)]
        out += [(rng.getrandbits(31) << 1 | p, rng.getrandbits(32), rng.getrandbits(32)) for p in bits]
    return out


REDUCTIONS = ("add", "min_u32", "min_s32", "max_u32", "max_s32", "and", "or", "xor")


def match_inputs(rng):
    """Per warp: values from a pool of 1, 2, 3, 5, 17 or 32 distinct words (a), and high words (b)
    from a pool of 1 or 2, so 64-bit matches depend on both halves."""
    out = []
    for w in range(WARPS):
        pool = [rng.getrandbits(32) for _ in range((1, 2, 3, 5, 17, 32)[w % 6])]
        highs = [rng.getrandbits(32) for _ in range(1 + w // 8)]
        out += [(rng.choice(pool), rng.choice(highs), 0) for _ in range(32)]
    return out


def redux_inputs(rng):
    """Per warp: zeros, all ones, signed extremes, one distinct lane, then random words."""
    fixed = [[0] * 32, [0xffffffff] * 32, [0x80000000, 0x7fffffff] * 16, [5] * 31 + [0xfffffffb]]
    return [(a, 0, 0) for w in range(WARPS) for a in (fixed[w] if w < len(fixed) else [rng.getrandbits(32) for _ in range(32)])]


def hoisted_voteu(image, name, negate):
    """sem_<name>'s VOTEU.ANY UR, UPT, PT reading P0 (or !P0) instead: the LOP3 setting P0 moves up
    to the VOTEU's slot and the instructions between move down one, each keeping its controls."""
    from tests.harness.gpu import ENCODING, Template, sm89
    from tests.harness.scalar import operands
    t = Template(image, "sem_" + name)
    offsets = range(0, len(t.code), 16)
    v = next(o for o in offsets if t.decoded(o).klass.mnemonic == "VOTEU")
    lop = next(o for o in offsets if o > v and t.decoded(o).klass.mnemonic == "LOP3" and t.decoded(o).env.get("Pu") == 0)
    word = lambda o: int.from_bytes(t.code[o:o + 16], "little") & ENCODING
    vote = sm89("voteu_", **dict(operands(t.decoded(v)), Pp=0, **{"Pp@not": negate}))
    order = [lop] + list(range(v, lop, 16))
    return t.patch({slot: (vote if src == v else word(src), t.control(src)) for slot, src in zip(range(v, lop + 16, 16), order)}, image)


def gpu_fixtures():
    from tests.harness.gpu import Fixture, compile
    from tests.refs import warp
    image = compile("threads_semantics", "-diag-suppress=177")

    def fixture(name, vs, expected, targets, **kw):
        return Fixture(name=name, image=image, kernel="sem_" + name, data=b"".join(struct.pack("<III", *v) for v in vs),
                       expected=expected, targets=targets, vectors=vs, cooperative=True, **{"output_size": 8, **kw})

    out = []
    for mode in ("idx", "up", "down", "bfly"):
        vs = shfl_inputs(random.Random(f"shfl_{mode}"))
        expected = [d | p << 32 for w in range(WARPS) for d, p in warp.shfl(mode, *zip(*vs[32 * w:32 * w + 32]))]
        out.append(fixture(f"shfl_{mode}", vs, expected, (f"SHFL.{mode.upper()}",)))
    for name, values in (("match_any", lambda v: v[0]), ("match_any64", lambda v: v[1] << 32 | v[0])):
        vs = match_inputs(random.Random(name))
        out.append(fixture(name, vs, [d for w in range(WARPS) for d in warp.match_any([values(v) for v in vs[32 * w:32 * w + 32]])],
                           ("MATCH.ANY",)))
    vs = match_inputs(random.Random("match_all"))
    out.append(fixture("match_all", vs, [d | p << 32 for w in range(WARPS) for d, p in [warp.match_all([a for a, _, _ in vs[32 * w:32 * w + 32]])] for _ in range(32)],
                       ("MATCH.ALL",)))
    for name in REDUCTIONS:
        op, signed = name.split("_")[0], name.endswith("_s32")
        vs = redux_inputs(random.Random(f"redux_{name}"))
        out.append(fixture(f"redux_{name}", vs, [warp.redux(op, [a for a, _, _ in vs[32 * w:32 * w + 32]], signed)
                                                 for w in range(WARPS) for _ in range(32)], ("REDUX",)))
    voteu = image
    for name, negate in (("voteu", 0), ("voteu_not", 1)): voteu = hoisted_voteu(voteu, name, negate)
    vs = [(0, 0, a) for a, _, _ in vote_inputs(random.Random("voteu"))]
    for name, negate in (("voteu", 0), ("voteu_not", 1)):
        votes = [warp.vote([(c & 1) ^ negate for _, _, c in vs[32 * w:32 * w + 32]]) for w in range(WARPS)]
        alls = [warp.vote([c & 1 for _, _, c in vs[32 * w:32 * w + 32]])[2] for w in range(WARPS)]
        out.append(Fixture(name=name, image=voteu, kernel="sem_" + name, data=b"".join(struct.pack("<III", *v) for v in vs),
                           expected=[votes[w][0] | alls[w] << 32 for w in range(WARPS) for _ in range(32)], targets=("VOTEU.ANY",), vectors=vs,
                           cooperative=True, output_size=8))
    vs = vote_inputs(random.Random("vote"))
    votes = [warp.vote([a & 1 for a, _, _ in vs[32 * w:32 * w + 32]]) for w in range(WARPS)]
    out.append(fixture("vote", vs, [ballot | (any_ | all_ << 1 | uni << 2) << 32 for ballot, any_, all_, uni in votes for _ in range(32)],
                       ("VOTE.ANY", "VOTE.ALL", "VOTE.EQ")))
    rng = random.Random("shared_exchange")
    vs = [(rng.getrandbits(32), rng.getrandbits(32), 0) for _ in range(4 * 128)]
    out.append(fixture("shared_exchange", vs, [shared_exchange(vs[b:b + 128])[t] for b in range(0, len(vs), 128) for t in range(128)],
                       ("STS", "LDS", "BAR.SYNC.DEFER_BLOCKING"), output_size=4, block=128))
    return out


def shared_exchange(block):
    """sem_shared_exchange for one 128-thread block."""
    first = [block[(t + b) % 128][0] for t, (_, b, _) in enumerate(block)]
    return [r ^ (first[(t * 5 + 7) % 128] * 3 + 1) & 0xffffffff for t, r in enumerate(first)]
