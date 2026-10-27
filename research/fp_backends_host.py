"""Normal-input CPU backend throughput; run with nice uv run python research/fp_backends_host.py."""
import ctypes
import random
import statistics

from sass.emulate import build

COUNT, REPEATS = 4096, 128
SPECS = [(w, op, scale) for w in (16, 32, 64) for op in ("add", "mul", "fma")
         for scale in ((0, 3) if op == "mul" else (0,))]
SPECS += [(64, "mul", scale) for scale in (1024, 1020)]


def name(backend, w, op, scale):
    return f"{backend}_{op}_{w}_{scale}"


def main():
    functions = []
    for w, op, scale in SPECS:
        for backend, namespace in (("soft", "softfloat"), ("host", "fp_host")):
            args = "a[i], b[i], " + ("c[i], " if op == "fma" else "") + "mode"
            if op == "mul":
                args += f", {scale}"
            functions.append(f'''
extern "C" double {name(backend, w, op, scale)}(
    const uint64_t *a, const uint64_t *b, const uint64_t *c, int rm, uint64_t *check) {{
  const auto mode = sass::fp_common::Rnd(rm);
  uint64_t checksum = 0;
  const auto start = std::chrono::steady_clock::now();
  for (int repeat = 0; repeat < {REPEATS}; ++repeat)
    for (int i = 0; i < {COUNT}; ++i)
      checksum += sass::{namespace}::{op}<{w}>({args});
  *check = checksum;
  return std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - start)
           .count() / ({REPEATS} * {COUNT});
}}''')
    source = '#include <chrono>\n#include "sass/softfloat.hpp"\n#include "sass/fp_host.hpp"\n'
    lib = build.library(source + "\n".join(functions), "fp-benchmark")
    rng = random.Random(95)
    for w, op, scale in SPECS:
        m, bias = {16: (10, 15), 32: (23, 127), 64: (52, 1023)}[w]
        columns = [(ctypes.c_uint64 * COUNT)(*((rng.randrange(bias - 2, bias + 3) << m)
                    | rng.getrandbits(m) for _ in range(COUNT))) for _ in range(3)]
        if scale in (1024, 1020):
            for column in columns[:2]:
                for i in range(COUNT):
                    column[i] = (column[i] & ((1 << m) - 1)) | (1 << (m - 1))
        elif scale:
            for i in range(COUNT):
                columns[1][i] -= scale << m
        for mode in (0, 3):
            results, checks = [], []
            for backend in ("soft", "host"):
                fn = getattr(lib, name(backend, w, op, scale))
                fn.argtypes = [ctypes.POINTER(ctypes.c_uint64)] * 3 + [
                    ctypes.c_int, ctypes.POINTER(ctypes.c_uint64)]
                fn.restype = ctypes.c_double
                timings = []
                for _ in range(3):
                    checksum = ctypes.c_uint64()
                    timings.append(fn(*columns, mode, ctypes.byref(checksum)))
                checks.append(checksum.value)
                results.append(statistics.median(timings))
            assert checks[0] == checks[1], (w, op, scale, mode)
            soft, host = results
            print(f'{w} {op} {("RN", "RZ", "RP", "RM")[mode]} scale={scale}: '
                  f"soft={soft:.2f} ns/op host={host:.2f} ns/op speedup={soft / host:.2f}x",
                  flush=True)


if __name__ == "__main__":
    main()
