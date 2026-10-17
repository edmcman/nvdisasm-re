"""Does an op's error against the correctly rounded result repeat with the position inside aligned segments of 2^k
mantissas (as a table-driven quadratic interpolator's would)? Prints, per k, the fraction of the signed-error variance
explained by the offset alone, for inputs 2^e * (1 + m / 2^23), m over every 23-bit mantissa.
usage: PYTHONPATH=. uv run --with numpy python research/mufu/structure.py OP [biased-exponent]"""
import sys
import numpy as np
from research.mufu.probe import Runner
from research.mufu.survey import ordered, reference
from tests.harness.gpu import Driver

op, e = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 127
n = 1 << 23
u = ((e << 23) | np.arange(n, dtype=np.uint32)).astype(np.uint32)
d = Driver()
try:
    r = Runner(d, op, n); got = r(u); r.close()
finally: d.close()
want, _, _ = reference(op, u)
err = (ordered(got) - ordered(want)).astype(np.float64)
print(f"{op} e={e}: signed error in ulps: min {err.min():.0f} max {err.max():.0f} mean {err.mean():.4f} var {err.var():.4f}; exact {np.mean(err == 0):.1%}")
for k in range(4, 22):
    seg = err.reshape(-1, 1 << k); by_offset = seg.mean(axis=0)
    print(f"  k={k:2d} (segments of {1 << k:>8}, {n >> k:>7} of them): variance explained by offset {by_offset.var() / err.var():7.2%}")
np.save("/tmp/claude-1000/-home-ed-Projects-nvdisasm-re/ffb13ceb-4272-4b27-9bc7-8fd513930fd1/scratchpad/rcp_hw.npy", got)
