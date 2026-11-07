import ctypes
import json
from statistics import median
from types import SimpleNamespace
from sass import paths
from sass.emulate import build, cxx
from sass.semantics import bitvec, lifter, ordered


BASELINE = """SASS_HD __attribute__((noinline)) static void baseline(sass::Ctx &c) {
  auto &t = c.t;
  const sass::u64 v0 = sass::u64(t.r[0]);
  const sass::u64 v1 = ((v0 + UINT64_C(0x1)) & UINT64_C(0xffffffff));
  const sass::u64 next = UINT64_C(0x10);
  t.r[0] = uint32_t((v1 >> 0) & 0xffffffff);
  t.r[1] = uint32_t((v0 >> 0) & 0xffffffff);
  t.pc = next;
}
"""


def source():
    r0, r1 = (bitvec.reg("R", index, 1) for index in range(2))
    builder = ordered.Builder()
    builder.assign_from_inputs([(r0.args, r0 + 1), (r1.args, r0)])
    sem = lifter.Semantics("SM89", "gate", "gate", 0, "native", body=builder.block())
    candidate = cxx.instruction(sem, "candidate", SimpleNamespace())
    candidate = candidate.replace("static void candidate", "__attribute__((noinline)) static void candidate")
    return '#include "sass/machine.hpp"\n#include <chrono>\n' + BASELINE + "\n" + candidate + """
extern "C" double benchmark(unsigned mode, unsigned count, uint64_t *checksum) {
  sass::Thread t{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment env{};
  sass::Ctx c{t, memory, local, warp, env};
  const auto start = std::chrono::steady_clock::now();
  if (mode) for (unsigned i = 0; i < count; ++i) candidate(c);
  else for (unsigned i = 0; i < count; ++i) baseline(c);
  const auto end = std::chrono::steady_clock::now();
  *checksum = (uint64_t(t.r[0]) << 32) | t.r[1];
  return std::chrono::duration<double>(end - start).count();
}
"""


def main():
    text = source()
    lib = build.library(text, "ordered-perf")
    lib.benchmark.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint64)]
    lib.benchmark.restype = ctypes.c_double
    samples = [[], []]
    count = 100_000_000
    checksum = ctypes.c_uint64()
    for iteration in range(9):
        for mode in ((0, 1) if iteration % 2 == 0 else (1, 0)):
            duration = lib.benchmark(mode, count, ctypes.byref(checksum))
            assert checksum.value == count << 32 | count - 1
            samples[mode].append(duration)
    medians = list(map(median, samples))
    result = dict(iterations=count, samples=samples, medians=medians, ratio=medians[1] / medians[0], compiler=build.compiler())
    directory = paths.CACHE / "ordered-gate"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "performance.cpp").write_text(text)
    (directory / "performance.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__": main()
