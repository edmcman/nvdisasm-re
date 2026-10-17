"""Exhaustive MUFU survey on the SM89 GPU: every 32-bit input through raw MUFU.<op> R11, R2, compared with the
correctly rounded result computed in float64 (SIN/COS take their argument in turns, 64H ops work on the high word of a
double). Writes the per-op ulp-distance histograms and a fixed input sample with its outputs.

usage: PYTHONPATH=. uv run --with numpy python research/mufu/survey.py [--ops RCP SIN ...] [--chunk-bits 22] [--limit-chunks N]
"""
import argparse, json, random, sys, time
from pathlib import Path
import numpy as np
from research.mufu.probe import OPS, Runner
from tests.harness.gpu import Driver

CLASSES = ("zero", "subnormal", "normal", "inf", "nan")
CAP = 64  # distances above this share the last histogram bucket
OUT = Path(__file__).parents[2] / "tests" / "refs"  # the results are reference data


def classify(u, exponent_bits, fraction_bits):
    """Class index per bit pattern (the sign bit above the exponent is ignored)."""
    e = (u >> fraction_bits) & ((1 << exponent_bits) - 1); f = u & ((1 << fraction_bits) - 1)
    full = (1 << exponent_bits) - 1
    return np.select([e == full, e == 0], [np.where(f == 0, 3, 4), np.where(f == 0, 0, 1)], 2)


def ordered(u):
    """Sign-magnitude 32-bit patterns as integers ordered like the values (+0 and -0 coincide)."""
    m = (u & 0x7fffffff).astype(np.int64)
    return np.where(u >> 31, -m, m)


def sin_turns(x, cosine=False):
    """sin (or cos) of 2*pi*x with exact range reduction: r = x - n/2 is exact, cos(2 pi r) = sin(2 pi (1/4 - |r|))."""
    n = np.rint(2 * x); r = x - n / 2
    v = np.sin(2 * np.pi * (0.25 - np.abs(r))) if cosine else np.sin(2 * np.pi * r)
    return np.where(np.fmod(n, 2) == 0, v, -v)


def reference(op, u):
    """(expected bit patterns, exponent bits, fraction bits) of the correctly rounded result."""
    with np.errstate(all="ignore"):
        if op.endswith("64H"):
            x = (u.astype(np.uint64) << 32).view(np.float64)
            y = 1 / x if op == "RCP64H" else 1 / np.sqrt(x)
            return (y.view(np.uint64) >> 32).astype(np.uint32), 11, 20
        x = u.view(np.float32).astype(np.float64)
        y = {"RCP": lambda: 1 / x, "RSQ": lambda: 1 / np.sqrt(x), "SQRT": lambda: np.sqrt(x), "EX2": lambda: np.exp2(x), "LG2": lambda: np.log2(x),
             "TANH": lambda: np.tanh(x), "SIN": lambda: sin_turns(x), "COS": lambda: sin_turns(x, True)}[op]()
        return y.astype(np.float32).view(np.uint32), 8, 23


class Stats:
    """Per (input class, expected-result class): count, bit-exact count, distance histogram, NaN agreement, examples."""

    def __init__(self): self.groups = {}

    def add(self, u, got, want, ebits, fbits):
        ci, cw, cg = classify(u, ebits, fbits), classify(want, ebits, fbits), classify(got, ebits, fbits)
        dist = np.abs(ordered(got) - ordered(want))
        for i in range(5):
            for w in range(5):
                m = (ci == i) & (cw == w)
                if not m.any(): continue
                g = self.groups.setdefault((CLASSES[i], CLASSES[w]), dict(n=0, exact=0, hist=np.zeros(CAP + 1, np.int64), comparable=0, got_nan=0, examples=[]))
                g["n"] += int(m.sum()); g["exact"] += int((got[m] == want[m]).sum())
                if w == 4: g["got_nan"] += int((cg[m] == 4).sum()); continue
                cmp_ = m & (cg != 4); g["comparable"] += int(cmp_.sum())
                g["hist"] += np.bincount(np.minimum(dist[cmp_], CAP), minlength=CAP + 1)
                if len(g["examples"]) < 4 and (bad := np.flatnonzero(cmp_ & (dist > (0 if w != 2 else 4))))[:1].size:
                    j = bad[0]; g["examples"].append([f"{u[j]:08x}", f"{got[j]:08x}", f"{want[j]:08x}"])

    def summary(self):
        out = {}
        for (i, w), g in sorted(self.groups.items()):
            h = g["hist"]; nz = np.flatnonzero(h)
            entry = dict(n=g["n"], exact=g["exact"])
            if w == "nan": entry["hw_nan"] = g["got_nan"]
            else:
                entry.update(comparable=g["comparable"], hist={str(k): int(h[k]) for k in nz}, max_distance=int(nz[-1]) if len(nz) else 0)
            if g["examples"]: entry["examples"] = g["examples"]
            out[f"{i}->{w}"] = entry
        return out


def sample_inputs():
    """The fixed fixture inputs: specials, powers of two and their neighbours, values around 1, large arguments, random patterns."""
    f = lambda x: int(np.float32(x).view(np.uint32))
    neighbours = lambda b: [b - 1, b, b + 1]
    out = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x807fffff, 0x00800000, 0x80800000, 0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000,
           0x7fc00000, 0xffc00000, 0x7f800001, 0xff800001, 0x7fffffff, 0xffffffff, 0x7fa5a5a5, 0xffbfffff, 0x00400000, 0x00000100]
    for k in range(-126, 128): out += neighbours(((k + 127) << 23) & 0x7fffffff)
    for k in range(-126, 128, 8): out += [b | 0x80000000 for b in neighbours(((k + 127) << 23))]
    out += [f(1 + n * 2.0 ** -23) for n in range(-16, 17) if n] + [f(-(1 + n * 2.0 ** -23)) for n in range(-16, 17) if n]
    out += [f(v) for v in (0.25, 0.5, 0.75, 1.5, 2.5, 3, 10, 100, 1000, 0.001, 0.1, 1 / 3, 6.283185307, 3.14159265, 1.5707963, -0.25, -0.75, -1.5, -3, -10, 88.7, -87.3, 127.99, -126.5, 128.0, -150.0)]
    rng = random.Random(0x4d554655)
    out += [f(rng.choice((-1, 1)) * rng.uniform(1, 2) * 2.0 ** rng.randrange(10, 40)) for _ in range(100)]
    out += [rng.getrandbits(32) for _ in range(400)]
    return list(dict.fromkeys(out))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ops", nargs="*", default=list(OPS)); p.add_argument("--chunk-bits", type=int, default=22); p.add_argument("--limit-chunks", type=int)
    a = p.parse_args(); n = 1 << a.chunk_bits
    samples = sample_inputs(); padded = samples + [0] * (-len(samples) % 256)
    driver = Driver(); results, sampled = {}, {}
    try:
        for op in a.ops:
            runner, stats, t0 = Runner(driver, op, n), Stats(), time.time()
            sampled[op] = [int(x) for x in runner(padded)[:len(samples)]]
            chunks = 1 << (32 - a.chunk_bits) if a.limit_chunks is None else a.limit_chunks
            for c in range(chunks):
                u = (np.arange(n, dtype=np.uint64) + c * n).astype(np.uint32)
                got = runner(u); want, eb, fb = reference(op, u); stats.add(u, got, want, eb, fb)
                if c % 64 == 0: print(f"{op} chunk {c}/{chunks} {time.time() - t0:.0f}s", flush=True)
            runner.close(); results[op] = stats.summary(); print(op, "done", f"{time.time() - t0:.0f}s", flush=True)
            if a.limit_chunks is None:
                (OUT / "mufu_survey.json").write_text(json.dumps(results, indent=1) + "\n")
                (OUT / "mufu_samples.json").write_text(json.dumps(dict(inputs="".join(f"{x:08x}" for x in samples), outputs={k: "".join(f"{x:08x}" for x in v) for k, v in sampled.items()})) + "\n")
    finally: driver.close()
    if a.limit_chunks is not None: print(json.dumps(results, indent=1))


if __name__ == "__main__": main()
