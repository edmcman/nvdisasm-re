"""Where a MUFU op's error lives: exact fraction and worst ulp distance per exponent of |x| (random mantissas).
usage: PYTHONPATH=. uv run --with numpy python research/mufu/by_exponent.py OP [samples-per-exponent]"""
import sys
import numpy as np
from research.mufu.probe import Runner
from research.mufu.survey import ordered, reference
from tests.harness.gpu import Driver

op, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1 << 16
rng = np.random.default_rng(1)
d = Driver()
try:
    r = Runner(d, op, n)
    print(f"{op}: exponent  exact%  max-ulp  (sign + for x>0)")
    for e in range(0, 255, 8 if len(sys.argv) < 4 else 1):
        u = ((e << 23) | rng.integers(0, 1 << 23, n, dtype=np.uint32)).astype(np.uint32)
        got = r(u); want, _, _ = reference(op, u)
        dist = np.abs(ordered(got) - ordered(want))
        print(f"  e={e:3d} (2^{e - 127:4d}): exact {np.mean(dist == 0):6.1%}  max {int(dist.max()):>9}  median {int(np.median(dist)):>6}")
    r.close()
finally: d.close()
