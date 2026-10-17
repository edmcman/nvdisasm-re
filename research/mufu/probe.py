"""MUFU on the SM89 GPU: run raw MUFU.<op> R11, R2 (patched into the carry kernel) over arbitrary 32-bit inputs.
usage: PYTHONPATH=. uv run --with numpy python research/mufu/probe.py"""
import ctypes as C
import numpy as np
from tests.harness.gpu import Driver, Fixture, sm89
from tests.harness.templates import variable_latency_fixture

OPS = ("COS", "SIN", "EX2", "LG2", "RCP", "RSQ", "RCP64H", "RSQ64H", "SQRT", "TANH")
NOP = sm89("nop_")


def mufu_fixture(op, inputs=(0, 0, 0, 0)):
    word = sm89("mufu__RRR_RR", Rd=11, Rb=2, mufuop=op)
    return variable_latency_fixture(f"mufu_{op}", word, [(x, 0, 0) for x in inputs], None)


class Runner:
    """One loaded MUFU kernel and reusable device buffers for chunks of `n` inputs."""

    def __init__(self, driver, op, n):
        f = mufu_fixture(op, [0] * 32)
        self.d, self.n = driver, n
        self.module, self.fn = C.c_void_p(), C.c_void_p()
        d = driver
        d.call("cuModuleLoadData", C.byref(self.module), C.c_char_p(f.image))
        d.call("cuModuleGetFunction", C.byref(self.fn), self.module, f.kernel.encode())
        self.a, self.b = C.c_uint64(), C.c_uint64()
        d.call("cuMemAlloc_v2", C.byref(self.a), C.c_size_t(n * 12)); d.call("cuMemAlloc_v2", C.byref(self.b), C.c_size_t(n * 4))
        self.host = np.zeros((n, 3), np.uint32)

    def __call__(self, values):
        n = len(values); assert n % 256 == 0 and n <= self.n
        d = self.d; self.host[:n, 0] = values
        d.call("cuMemcpyHtoD_v2", self.a, self.host.ctypes.data_as(C.c_void_p), C.c_size_t(n * 12))
        params = (C.c_void_p * 2)(*(C.cast(C.byref(v), C.c_void_p) for v in (self.a, self.b)))
        d.call("cuLaunchKernel", self.fn, C.c_uint(n // 256), C.c_uint(1), C.c_uint(1), C.c_uint(256), C.c_uint(1), C.c_uint(1), C.c_uint(0), C.c_void_p(), params, C.c_void_p())
        out = np.empty(n, np.uint32)
        d.call("cuCtxSynchronize"); d.call("cuMemcpyDtoH_v2", out.ctypes.data_as(C.c_void_p), self.b, C.c_size_t(n * 4))
        return out

    def close(self):
        self.d.call("cuMemFree_v2", self.a); self.d.call("cuMemFree_v2", self.b); self.d.call("cuModuleUnload", self.module)


if __name__ == "__main__":
    f32 = lambda x: np.array([x], np.float32).view(np.uint32)[0]
    specials = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc00000, 0x7f800001, 0x7fffffff,
                0x40000000, 0x3f000000, 0x7f7fffff, 0xff7fffff, f32(100.0), f32(-100.0), f32(0.25), f32(1e10)]
    specials += [0] * (-len(specials) % 256)
    d = Driver()
    try:
        for op in OPS:
            r = Runner(d, op, 256); out = r(specials)
            print(op, " ".join(f"{x:08x}>{y:08x}" for x, y in list(zip(specials, out))[:22]))
            r.close()
    finally: d.close()
