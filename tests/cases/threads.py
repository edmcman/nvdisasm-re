"""Semantics involving several threads: SHFL and VOTE with explicit warp context (layer 4), and
cooperative blocks (warps in lockstep, shared memory, BAR.SYNC) against the SM89 GPU (layer 5)."""
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


def gpu_fixtures():
    from tests.harness.gpu import Fixture, compile
    from tests.refs import warp
    image = compile("threads_semantics")

    def fixture(name, vs, expected, targets, **kw):
        return Fixture(name=name, image=image, kernel="sem_" + name, data=b"".join(struct.pack("<III", *v) for v in vs),
                       expected=expected, targets=targets, vectors=vs, cooperative=True, **{"output_size": 8, **kw})

    out = []
    for mode in ("idx", "up", "down", "bfly"):
        vs = shfl_inputs(random.Random(f"shfl_{mode}"))
        expected = [d | p << 32 for w in range(WARPS) for d, p in warp.shfl(mode, *zip(*vs[32 * w:32 * w + 32]))]
        out.append(fixture(f"shfl_{mode}", vs, expected, (f"SHFL.{mode.upper()}",)))
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
