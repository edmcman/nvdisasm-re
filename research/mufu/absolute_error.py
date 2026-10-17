"""Maximum absolute error (against the float64 reference) of a MUFU op, per exponent of |x| (random mantissas, both signs).
usage: PYTHONPATH=. uv run --with numpy python research/mufu/absolute_error.py OP [samples-per-exponent]"""
import sys
import numpy as np
from research.mufu.probe import Runner
from research.mufu.survey import reference
from tests.harness.gpu import Driver

op, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1 << 16
rng = np.random.default_rng(2)
d = Driver()
try:
    r = Runner(d, op, n)
    worst = 0
    for e in range(96, 160):
        u = ((e << 23) | rng.integers(0, 1 << 23, n, dtype=np.uint32) | (rng.integers(0, 2, n, dtype=np.uint32) << 31)).astype(np.uint32)
        got, (want, _, _) = r(u).view(np.float32).astype(np.float64), reference(op, u)
        err = np.abs(got - want.view(np.float32).astype(np.float64)); ok = np.isfinite(err)
        m = float(err[ok].max()) if ok.any() else 0; worst = max(worst, m)
        if e % 4 == 0: print(f"  e={e:3d} (2^{e - 127:4d}): max abs err {m:.3e} = 2^{np.log2(m) if m else float('-inf'):.2f}")
    print(f"{op}: worst max abs err over e in [96,160): {worst:.3e} = 2^{np.log2(worst):.2f}")
    r.close()
finally: d.close()
