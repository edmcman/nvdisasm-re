"""Constant-memory C++ comparisons over complete input domains."""
import ctypes
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sass import workers
from sass.emulate import build, definitions
from sass.semantics import bitvec

CHUNK = 1 << 24


@dataclass(frozen=True)
class Enumeration:
    operation: str
    widths: tuple[int, ...]
    soft: str
    host: str

    @property
    def total(self): return 1 << sum(self.widths)

    def inputs(self, index):
        """Little-endian mixed radix: operand zero varies fastest."""
        values = []
        for width in self.widths:
            values.append(index & bitvec.mask(width))
            index >>= width
        return tuple(values)


def arguments(widths):
    offset, args = 0, []
    for width in widths:
        args.append(f"sass::Bits<{width}>((index >> {offset}) & {bitvec.mask(width)}ULL)")
        offset += width
    return ", ".join(args)


def floating(op, mode, src=16, dst=None):
    widths = (src,) if op == "convert" else (src, src)
    args = arguments(widths)
    name = f"{op}.{mode.lower()}.f{src}" + (f".f{dst}" if dst else "")
    template = f"{src}, {dst}" if dst else str(src)
    scale = ", 0" if op == "mul" else ""
    soft = f"sass::softfloat::{op}<{template}>({args}, sass::fp_common::{mode}{scale})"
    host = f"sass::fp_host::{op}<{template}>({args}, sass::fp_common::{mode}{scale})"
    return Enumeration(name, widths, soft, host)


def defined(use):
    label, widths = use
    name = definitions.definition_name(label, widths)
    args = arguments(widths)
    return Enumeration(f"{label}{widths}", widths,
                       f"sass::def_soft::{name}({args})", f"sass::def_host::{name}({args})")


def source(enumerations, uses=()):
    header = definitions.definitions_header(uses).replace("#pragma once\n", "")
    # Same generated bodies, with explicit backend names in separate namespaces.
    # This permits inlined comparisons without arrays or calls through Python.
    soft = header.replace("namespace sass::def {", "namespace sass::def_soft {").replace("sass::fp::", "sass::softfloat::")
    host = header.replace("namespace sass::def {", "namespace sass::def_host {").replace("sass::fp::", "sass::fp_host::")
    lines = ['#include "sass/softfloat.hpp"', '#include "sass/fp_host.hpp"', soft, host,
             'extern "C" sass::u64 run(unsigned which, sass::u64 start, sass::u64 count, sass::u64 *out) {',
             '  switch (which) {']
    for which, spec in enumerate(enumerations):
        lines.extend([
            f"    case {which}: {{",
            f"      if (start > {spec.total}ULL || count > {spec.total}ULL - start || count > {CHUNK}ULL) return ~sass::u64(0);",
            "      for (sass::u64 n = 0; n < count; ++n) {",
            "        const sass::u64 index = start + n;",
            # Volatile results keep every comparison observable even when both
            # integer bodies are identical and the compiler can prove equality.
            f"        const volatile auto soft = {spec.soft};",
            f"        const volatile auto host = {spec.host};",
            "        if (soft != host) {",
            "          out[0] = index;",
            "          out[1] = sass::u64(host);",
            "          out[2] = sass::u64(sass::u128(host) >> 64);",
            "          out[3] = sass::u64(soft);",
            "          out[4] = sass::u64(sass::u128(soft) >> 64);",
            "          return n;",
            "        }",
            "      }",
            "      return count;",
            "    }",
        ])
    lines.extend(["  }", "  return ~sass::u64(0);", "}"])
    return "\n".join(lines)


def compiled(enumerations, uses=()):
    fn = build.library(source(enumerations, uses), "native-full").run
    fn.argtypes = [ctypes.c_uint, ctypes.c_uint64, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)]
    fn.restype = ctypes.c_uint64
    return fn


def chunk(fn, which, spec, start, count):
    output = (ctypes.c_uint64 * 5)()
    checked = fn(which, start, count, output)
    if checked != count:
        index, low, high, slow, shigh = output
        assert checked < count, (spec.operation, "invalid chunk", start, count, checked)
        assert index == start + checked
        raise AssertionError(
            f"{spec.operation}, signature {spec.widths}, index {index:#x}, "
            f"inputs {tuple(hex(v) for v in spec.inputs(index))}: "
            f"host {low | high << 64:#x}, softfloat {slow | shigh << 64:#x}"
        )
    return checked


def complete(fn, which, spec):
    """Benchmark representative chunks first, then compare the whole domain."""
    assert sum(spec.widths) <= 32
    assert spec.inputs(0) == (0,) * len(spec.widths)
    assert spec.inputs(spec.total - 1) == tuple(bitvec.mask(w) for w in spec.widths)
    size = min(CHUNK, spec.total)
    chunks = spec.total // size
    parallel = min(chunks, workers.fitting(1, 32, os.cpu_count() or 1))
    # Benchmark at the actual concurrency, spread across both signs and every exponent
    # region. Dividing single-thread throughput by logical CPU count overstated scaling
    # on the 24-core/32-thread hybrid desktop used for the initial measurements.
    starts = [0] if parallel == 1 else [
        (k * (chunks - 1) // (parallel - 1)) * size for k in range(parallel)]
    remaining = [start for start in range(0, spec.total, size) if start not in starts]
    begin = time.monotonic()
    with ThreadPoolExecutor(parallel) as pool:
        checked = sum(pool.map(lambda start: chunk(fn, which, spec, start, size), starts))
        seconds = time.monotonic() - begin
        estimate = seconds * spec.total / checked
        print(f"\n{spec.operation} {spec.widths}: benchmark {checked:,} in {seconds:.3f}s; "
              f"{spec.total:,} total, {parallel} workers, estimated {estimate:.1f}s", flush=True)
        # Ordered results report the first mismatch in the submitted chunk order.
        checked += sum(pool.map(lambda start: chunk(fn, which, spec, start, min(size, spec.total - start)), remaining))
    elapsed = time.monotonic() - begin
    assert checked == spec.total, (spec.operation, checked, spec.total)
    print(f"{spec.operation}: compared {checked:,} in {elapsed:.3f}s", flush=True)
    return checked, elapsed
