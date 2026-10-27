"""Backend selection, floating-point environment, and the host FP16 odd intermediate."""
import os, subprocess, sys

import pytest
from sass.emulate import build, native
from sass.semantics.fp import softfloat

pytestmark = pytest.mark.native


def isolated(script, cxxflags=""):
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(build.HERE.parent.parent), "SASS_CXXFLAGS": cxxflags})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("backend", ("host", "softfloat"))
def test_selection(backend):
    isolated(f'''
from sass.emulate import build
source = """
#include "sass/fp.hpp"
#include "sass/fp_host.hpp"
static_assert(sass::fp::RN == 0 && sass::fp::RZ == 1 && sass::fp::RP == 2 && sass::fp::RM == 3);
extern "C" int selected() {{
  return &sass::fp::add<32> == &sass::{"fp_host" if backend == "host" else "softfloat"}::add<32>;
}}
"""
assert build.library(source, "fp-selection").selected() == 1
assert ("-DSASS_FP_SOFT" in build.flags()) == {backend == "softfloat"}
for flag in ("-ffp-contract=off", "-fno-fast-math", "-frounding-math", "-msse2", "-mfpmath=sse"):
    assert flag in build.flags()
''', cxxflags="-DSASS_FP_SOFT" if backend == "softfloat" else "")


def test_backend_cache_paths_are_distinct():
    isolated('''
import os
from sass.emulate import build
source = '#include "sass/fp.hpp"'
host = build.path(source, "selection")
os.environ["SASS_CXXFLAGS"] = "-DSASS_FP_SOFT"
soft = build.path(source, "selection")
assert host != soft
assert host.parent == soft.parent
''')


@pytest.mark.parametrize("mask", (0x8000, 0x40, 0x8040))
def test_mxcsr_rejection(mask):
    isolated(f'''
import ctypes
from sass.emulate import build
source = """
#include <xmmintrin.h>
extern "C" void set_mxcsr(unsigned x) {{ _mm_setcsr(x); }}
"""
lib = build.library(source, "fp-environment")
lib.set_mxcsr.argtypes = [ctypes.c_uint32]
saved = lib.sass_runtime_mxcsr()
lib.set_mxcsr(saved | {mask})
try:
    # Even a cached .so is checked after every CDLL load.
    build.library(source, "fp-environment")
except RuntimeError as error:
    assert "FTZ and DAZ" in str(error)
else:
    raise AssertionError("flushing environment accepted")
assert lib.sass_runtime_mxcsr() == saved | {mask}
lib.set_mxcsr(saved)
''')


def test_caller_rounding_independence_and_restoration():
    isolated('''
from sass.emulate import build, native
control = build.library("""
#include <cfenv>
extern "C" int set_round(int i) {
  const int modes[] = {FE_TONEAREST, FE_TOWARDZERO, FE_UPWARD, FE_DOWNWARD};
  return std::fesetround(modes[i]);
}
extern "C" int get_round() { return std::fegetround(); }
""", "fp-rounding")
native.library()
for caller in range(4):
    assert control.set_round(caller) == 0
    saved = control.get_round()
    for mode in native.ROUNDING:
        for w, a, b, c in ((16, 1, 1, 0x7800),
                           (32, 0x3f800001, 0x33800000, 0x80000001),
                           (64, 0x3ff0000000000001, 0x3ca0000000000000, 1)):
            for op in ("add", "mul", "fma", "lt", "le", "eq", "nan"):
                for scale in ((-512, 0, 512, 513) if op == "mul" else (0,)):
                    args = dict(mode=mode, scale=scale)
                    assert native.fp_host(op, w, a, b, c, **args) == native.fp(op, w, a, b, c, **args)
                    assert control.get_round() == saved, (caller, mode, w, op)
            for dst in (16, 32, 64):
                assert native.fp_host("convert", w, a, dst=dst, mode=mode) == native.fp("convert", w, a, dst=dst, mode=mode)
                assert control.get_round() == saved
assert control.set_round(0) == 0
''')


def test_rounding_only_sets_changed_modes():
    isolated('''
from sass.emulate import build
original_flags = build.flags
build.flags = lambda: [*original_flags(), "-Wl,--wrap=fesetround"]
lib = build.library("""
#include "sass/fp_host.hpp"
extern "C" int __real_fesetround(int);
static int calls;
extern "C" int __wrap_fesetround(int mode) {
  ++calls;
  return __real_fesetround(mode);
}
extern "C" int scopes(int caller, int outer, int inner) {
  const int modes[] = {FE_TONEAREST, FE_TOWARDZERO, FE_UPWARD, FE_DOWNWARD};
  if (__real_fesetround(modes[caller])) return -1;
  calls = 0;
  {
    sass::fp_host::Rounding first{sass::fp_host::Rnd(outer)};
    if (std::fegetround() != modes[outer]) return -1;
    {
      sass::fp_host::Rounding second{sass::fp_host::Rnd(inner)};
      if (std::fegetround() != modes[inner]) return -1;
    }
    if (std::fegetround() != modes[outer]) return -1;
  }
  const bool restored = std::fegetround() == modes[caller];
  __real_fesetround(FE_TONEAREST);
  return restored ? calls : -1;
}
""", "fp-rounding-transitions")
for caller in range(4):
    for outer in range(4):
        for inner in range(4):
            # Matched scopes perform zero setters; each change sets and restores once.
            expected = 2 * (caller != outer) + 2 * (outer != inner)
            assert lib.scopes(caller, outer, inner) == expected, (caller, outer, inner)
''')


@pytest.fixture(scope="module")
def odd_sum():
    import ctypes
    lib = build.library('''
#include "sass/fp_host.hpp"
extern "C" uint64_t odd_sum(uint64_t a, uint64_t b, uint64_t c, uint64_t *residual) {
  sass::fp_host::Rounding rounding(sass::fp_host::RN);
  volatile double x = sass::fp_host::half(a), y = sass::fp_host::half(b), p = x * y;
  const auto result = sass::fp_host::round_odd_sum(p, sass::fp_host::half(c));
  *residual = sass::fp_common::encoding<64>(result.residual);
  return sass::fp_common::encoding<64>(result.odd);
}
''', "fp-odd-sum")
    lib.odd_sum.argtypes = [ctypes.c_uint64] * 3 + [ctypes.POINTER(ctypes.c_uint64)]
    lib.odd_sum.restype = ctypes.c_uint64
    def call(a, b, c):
        residual = ctypes.c_uint64()
        bits = lib.odd_sum(a, b, c, ctypes.byref(residual))
        return bits, residual.value
    return call


@pytest.mark.parametrize("a,b,c", (
    (1, 1, 0x7800), (0x8001, 1, 0x7800),
    (1, 1, 0xf800), (0x8001, 1, 0xf800),
    (0x3c01, 0x3c01, 0xbc02),  # near cancellation
    (0x3c00, 0x1000, 0x3c00),  # exact halfway
    (0x3c01, 0x1000, 0x3c00),  # just above halfway
    (0x7bff, 0x4000, 0x7bff),  # overflow
    (1, 0x3800, 0), (3, 0x3800, 0x8001),  # subnormal ties and cancellation
    (0, 0xbc00, 0x8000), (0x8000, 0xbc00, 0),
    (0x3c00, 0x3c00, 0xbc00),  # exact cancellation
    (0x7c00, 0, 0), (0x7c00, 0x3c00, 0xfc00),
    (0xfc01, 0x3c00, 0x3c00), (0x3c00, 0x3c00, 0xfc00),
))
def test_half_fma_regressions(a, b, c):
    for mode in native.ROUNDING:
        assert native.fp_host("fma", 16, a, b, c, mode=mode) == softfloat.arithmetic("fma", [a, b, c], 16, mode)


@pytest.mark.parametrize("a,c", ((1, 0x7800), (0x8001, 0x7800), (1, 0xf800), (0x8001, 0xf800)))
def test_half_fma_exercises_nonzero_residual(odd_sum, a, c):
    bits, residual = odd_sum(a, 1, c)
    assert bits & 1  # round-to-odd path ran
    assert residual & 0x7fffffffffffffff
    assert residual >> 63 == a >> 15
    # Directed half rounding must notice a term lost entirely by ordinary double RN.
    mode = "RP" if a == 1 else "RM"
    expected = softfloat.arithmetic("fma", [a, 1, c], 16, mode)
    assert expected != c
    assert native.fp_host("fma", 16, a, 1, c, mode=mode) == expected


@pytest.mark.parametrize("w", (16, 32, 64))
def test_scaled_multiply_rounds_once(w):
    e, m = softfloat.FORMATS[w]
    # An unscaled result overflows; the scaled exact product is finite.
    maximum = ((1 << e) - 1) << m
    one = ((1 << (e - 1)) - 1) << m
    a, b = maximum - 1, one + (1 << m)
    for mode in native.ROUNDING:
        assert native.fp_host("mul", w, a, b, scale=-1, mode=mode) == a
        # Underflow before scaling would likewise lose a representable result.
        assert native.fp_host("mul", w, 1, one - (1 << m), scale=1, mode=mode) == 1


def test_odd_sum_tie_neighbours(odd_sum):
    # Exercise exact rounding from odd intermediates at both sides of half ties.
    for a, b, c in ((0x3c01, 0x1000, 0x3c00), (0xbc01, 0x1000, 0x3c00)):
        bits, _ = odd_sum(a, b, c)
        for mode in native.ROUNDING:
            want = softfloat.arithmetic("fma", [a, b, c], 16, mode)
            assert softfloat.pack(softfloat.unpack(bits, 64)[2], 16, mode) == want
