"""MUFU.<op>.F16 on the SM89 GPU: which half of Rb is read, what lands in Rd, and how the result compares with the
correctly rounded float16 value, over every 16-bit input.
usage: PYTHONPATH=. uv run --with numpy python research/mufu/fp16.py"""
import numpy as np
from research.mufu.probe import Runner
from tests.harness.gpu import Driver, sm89

OPS = ("COS", "SIN", "EX2", "LG2", "RCP", "RSQ", "SQRT", "TANH")


def word(op, extract, **flags):
    return sm89("mufu_fp16__RR", Rd=11, Rb=2, mufuop=op, width="F16", extract=extract, **flags)


def reference(op, x16):
    """Correctly rounded float16 result (SIN/COS in turns, as the f32 forms)."""
    x = x16.view(np.float16).astype(np.float64)
    with np.errstate(all="ignore"):
        y = {"RCP": lambda: 1 / x, "RSQ": lambda: 1 / np.sqrt(x), "SQRT": lambda: np.sqrt(x), "EX2": lambda: np.exp2(x), "LG2": lambda: np.log2(x),
             "TANH": lambda: np.tanh(x), "SIN": lambda: np.sin(2 * np.pi * (x - np.rint(2 * x) / 2)) * np.where(np.fmod(np.rint(2 * x), 2) == 0, 1, -1),
             "COS": lambda: np.sin(2 * np.pi * (0.25 - np.abs(x - np.rint(2 * x) / 2))) * np.where(np.fmod(np.rint(2 * x), 2) == 0, 1, -1)}[op]()
    return y.astype(np.float16).view(np.uint16)


def ordered16(u):
    m = (u & 0x7fff).astype(np.int64)
    return np.where(u >> 15, -m, m)


CLASSES = ("zero", "subnormal", "normal", "inf", "nan")


def classify16(u):
    e, f = (u >> 10) & 31, u & 1023
    return np.select([e == 31, e == 0], [np.where(f == 0, 3, 4), np.where(f == 0, 0, 1)], 2)


def sweep(d, op, extract, other=0x3c00):
    """(inputs, hardware low halves, hardware high halves) over every 16-bit input."""
    x = np.arange(1 << 16, dtype=np.uint32)
    r = Runner(d, op, 1 << 16, word=word(op, extract))
    out = r(((x | other << 16) if extract == "H0" else (other | x << 16)).astype(np.uint32)); r.close()
    return x.astype(np.uint16), (out & 0xffff).astype(np.uint16), (out >> 16).astype(np.uint16)


if __name__ == "__main__":
    d = Driver()
    try:
        for op in OPS:
            x, lo, hi = sweep(d, op, "H0"); want = reference(op, x)
            ci, cw, cg = classify16(x), classify16(want), classify16(lo)
            dist = np.abs(ordered16(lo) - ordered16(want)); fin = (cw >= 0) & (cw != 4) & (cg != 4)
            nan_in = cw == 4
            print(f"{op:4} high half always zero: {(hi == 0).all()}; NaN results: hw NaN on {np.mean(cg[nan_in] == 4):.1%} of {nan_in.sum()}, patterns {sorted({f'{v:04x}' for v in lo[nan_in & (cg == 4)]})[:4]}")
            for i in range(5):
                for w in range(4):
                    m = (ci == i) & (cw == w)
                    if not m.any(): continue
                    both = m & (cg != 4); h = np.bincount(np.minimum(dist[both], 9), minlength=10)
                    print(f"      {CLASSES[i]:9}->{CLASSES[w]:9} n={int(m.sum()):6} exact {np.mean(lo[m] == want[m]):6.1%} max {int(dist[both].max()) if both.any() else '-':>5} hist(0..8,9+) {h.tolist()}")
    finally: d.close()
