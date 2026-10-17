"""Is MUFU.<op> on one binade piecewise quadratic? For every 23-bit mantissa of 2^e * (1 + m / 2^23) the raw output is
split into aligned segments of 2^k mantissas and fitted with polynomials of degree 1, 2 and 3; a quadratic followed by
rounding leaves a residual span of about 1 output unit and a cubic does not improve on it.
usage: PYTHONPATH=. uv run --with numpy python research/mufu/segment_fit.py OP [biased-exponent] [k]"""
import sys
import numpy as np
from research.mufu.probe import Runner
from tests.harness.gpu import Driver

op, e = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 127
k = int(sys.argv[3]) if len(sys.argv) > 3 else 16
n, size = 1 << 23, 1 << k
d = Driver()
try:
    r = Runner(d, op, n); out = r(((e << 23) | np.arange(n, dtype=np.uint32)).astype(np.uint32)).astype(np.int64); r.close()
finally: d.close()
t = np.arange(size, dtype=np.float64) / size
print(f"{op} e={e}, segments of 2^{k}: residual span of the best polynomial fit (output units) for degree 1 / 2 / 3")
for seg in sorted({0, 1, 2, 5, 17, 40, n // size // 2 - 1, n // size // 2, n // size - 1}):
    h = out[seg * size:(seg + 1) * size].astype(np.float64)
    spans = [np.ptp(h - np.polyval(np.polyfit(t, h, deg), t)) for deg in (1, 2, 3)]
    print(f"  segment {seg:4d}: {spans[0]:9.3f} {spans[1]:7.3f} {spans[2]:7.3f}")
