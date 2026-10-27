"""Four-way native kernel benchmark.

nice uv run python research/native_backend_benchmark.py --out /tmp/native-benchmark.json

Cold libraries use a fresh temporary cache for every sample. Generation starts after
warming MD/lifting discovery. Warm execution includes Kernel.run's Python launch and
output handling, uses one worker, and is checked in full against all configurations.
"""
import argparse
import ctypes
import hashlib
import json
import os
import platform
import random
import statistics
import struct
import tempfile
import time
from pathlib import Path

from sass.assemble import assemble_instruction
from sass.emulate import build, codegen, kernel, machine, reference


ARCH, BASE, INPUT, OUTPUT = "SM89", 0x100000, 0x200000, 0x400000
CONFIGS = [(backend, calls) for backend in ("host", "softfloat") for calls in (False, True)]
WORKLOADS = {
    "fp16": ("HADD2 R0, R0, R2", "HMUL2 R0, R0, R4", "HFMA2 R0, R0, R4, R6"),
    "fp32": ("FADD.FTZ R0, |R0|, R2", "FMUL.RM R0, R0, R4", "FFMA.RP R0, R0, R4, R6"),
    "fp64": ("DADD R0, |R0|, R2", "DMUL.RM R0, R0, R4", "DFMA.RP R0, R0, R4, R6"),
    "convert": ("F2F.F64.F32 R8, R0", "F2F.F32.F64 R0, R8",
                "F2F.F16.F32 R9, R0", "F2F.F32.F16 R0, R9",
                "I2F.F32.U32 R10, R2", "F2I.U32.F32.TRUNC R2, R10"),
    "integer": ("PRMT R0, R0, 0x5410, R2", "IMAD R0, R0, R4, R6",
                "IADD3 R2, R0, R2, R6"),
    "cooperative": ("SHFL.IDX PT, R0, R0, 0x1, 0x1f",
                    "PRMT R0, R0, 0x5410, R2", "WARPSYNC 0xffffffff", "BAR.SYNC 0x0"),
}


def normal(rng, width):
    mantissa, bias = {16: (10, 15), 32: (23, 127), 64: (52, 1023)}[width]
    return rng.getrandbits(1) << (width - 1) | rng.randrange(bias - 3, bias + 1) << mantissa | rng.getrandbits(mantissa)


def workload(name, threads, cycles):
    rng = random.Random(710)
    data = []
    for _ in range(threads):
        if name == "fp16":
            data.extend(normal(rng, 16) | normal(rng, 16) << 16 for _ in range(8))
        elif name == "fp64":
            for _ in range(4):
                value = normal(rng, 64)
                data.extend((value & 0xffffffff, value >> 32))
        elif name in ("fp32", "convert"):
            data.extend(normal(rng, 32) for _ in range(8))
        else:
            data.extend(rng.getrandbits(32) for _ in range(8))
    texts = ["S2R R30, SR_TID.X", "S2R R31, SR_CTAID.X", "IMAD R30, R31, 0x20, R30",
             "IMAD.WIDE.U32 R40, R30, R32, R42", "IMAD.WIDE.U32 R44, R30, R33, R46",
             "LDG.128 R0, [R40]", "LDG.128 R4, [R40+0x10]"]
    texts.extend(WORKLOADS[name] * cycles)
    texts.extend(("STG.128 [R44], R0", "EXIT"))
    code = b"".join(assemble_instruction(ARCH, text) for text in texts)
    request = {"kernel": {"name": name, "code": code.hex(), "address": BASE,
                          "threads": threads, "block_size": 32, "cooperative": name == "cooperative",
                          "registers": {"R32": 32, "R33": 16, "R42": INPUT, "R46": OUTPUT}},
               "memory": [{"space": "ram", "address": INPUT,
                           "hex": struct.pack("<" + "I" * len(data), *data).hex()}],
               "context": {"synchronization": {"bar": True, "warpsync": True}},
               "output": {"address": OUTPUT, "size": 16, "stride": 16}}
    return code, request


class MeasuredKernel(kernel.Kernel):
    def __init__(self, code, source, instructions):
        # Source generation is measured separately from compilation/loading.
        machine.Library.__init__(self, source, "benchmark")
        self.arch, self.code, self.base, self.instructions = ARCH, code, BASE, instructions
        self.lib.sass_run.argtypes = [ctypes.POINTER(kernel.Launch), ctypes.POINTER(kernel.Result)]
        self.lib.sass_run.restype = ctypes.c_uint32


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warm-repeats", type=int, default=5)
    parser.add_argument("--threads", type=int, default=8192)
    parser.add_argument("--cycles", type=int, default=16)
    args = parser.parse_args()
    if min(args.repeats, args.warm_repeats, args.cycles) < 1 or args.threads < 32 or args.threads % 32:
        parser.error("positive repeats/cycles and a positive whole number of 32-thread warps required")
    setup = time.perf_counter()
    workloads = {name: workload(name, args.threads, args.cycles) for name in WORKLOADS}
    # Discovery/MD caches must not bias the first generation sample.
    for code, _ in workloads.values():
        for _, calls in CONFIGS: codegen.kernel(ARCH, code, BASE, definition_calls=calls)
    print(f"Prepared inputs and generation caches in {time.perf_counter() - setup:.2f}s", flush=True)
    samples, expected = {}, {}
    for name, (_, request) in workloads.items():
        start = time.perf_counter()
        small = {**request, "kernel": {**request["kernel"], "threads": 32},
                 "memory": [{**region, "hex": region["hex"][:32 * 32 * 2]} for region in request["memory"]]}
        result = reference.run_kernel(small, ARCH)
        if "error" in result: raise RuntimeError(result["error"])
        expected[name] = result["outputs"]
        print(f"Checked {name} reference warp in {time.perf_counter() - start:.2f}s", flush=True)
    report = {"machine": platform.platform(), "cpu": next((line.split(":", 1)[1].strip()
               for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")), ""),
              "compiler": build.compiler().splitlines()[0], "flags": list(build.FLAGS),
              "arch": ARCH, "fp": "exact", "seed": 710,
              "cpu_affinity": sorted(os.sched_getaffinity(0)),
              "threads": args.threads, "cycles": args.cycles, "repeats": args.repeats,
              "warm_repeats": args.warm_repeats, "workers": 1, "results": []}
    original_cache, original_flags = build.CACHE, os.environ.get("SASS_CXXFLAGS")
    baselines = {}
    try:
        for repeat in range(args.repeats):
            order = list(CONFIGS)
            random.Random(710 + repeat).shuffle(order)
            for backend, calls in order:
                os.environ["SASS_CXXFLAGS"] = "-DSASS_FP_SOFT" if backend == "softfloat" else ""
                for name, (code, request) in workloads.items():
                    start = time.perf_counter()
                    source, instructions = codegen.kernel(ARCH, code, BASE, definition_calls=calls)
                    generation = time.perf_counter() - start
                    if "not compiled:" in source: raise RuntimeError(f"{name}: unsupported instruction")
                    with tempfile.TemporaryDirectory(prefix="sass-native-benchmark-") as directory:
                        build.CACHE = Path(directory)
                        start = time.perf_counter()
                        compiled = MeasuredKernel(code, source, instructions)
                        compilation = time.perf_counter() - start
                        result = compiled.run(request, workers=1)  # untimed warmup
                        assert result["outputs"][:32] == expected[name], (name, backend, calls, "reference")
                        if name not in baselines: baselines[name] = result
                        assert result == baselines[name], (name, backend, calls)
                        warm = []
                        for _ in range(args.warm_repeats):
                            start = time.perf_counter()
                            result = compiled.run(request, workers=1)
                            warm.append(time.perf_counter() - start)
                            assert result == baselines[name], (name, backend, calls)
                    key = name, backend, calls
                    samples.setdefault(key, []).append((generation, compilation, warm, len(source.encode())))
                    print(f"{repeat + 1}/{args.repeats} {name} {backend} {'calls' if calls else 'inline'}: "
                          f"generation={generation:.4f}s compile={compilation:.3f}s "
                          f"warm={statistics.median(warm):.4f}s", flush=True)
    finally:
        build.CACHE = original_cache
        if original_flags is None: os.environ.pop("SASS_CXXFLAGS", None)
        else: os.environ["SASS_CXXFLAGS"] = original_flags
    for (name, backend, calls), rows in sorted(samples.items()):
        report["results"].append({"workload": name, "backend": backend, "definition_calls": calls,
                                 "generation_s": statistics.median(row[0] for row in rows),
                                 "cold_compile_s": statistics.median(row[1] for row in rows),
                                 "warm_execution_s": statistics.median(t for row in rows for t in row[2]),
                                 "source_bytes": rows[0][3], "samples": rows,
                                 "cxxflags": "-DSASS_FP_SOFT" if backend == "softfloat" else "",
                                 "output_sha256": hashlib.sha256(bytes.fromhex("".join(baselines[name]["outputs"]))).hexdigest()})
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))
    print("workload backend lowering generation_ms cold_compile_ms warm_ms")
    for row in report["results"]:
        print(row["workload"], row["backend"], "calls" if row["definition_calls"] else "inline",
              *(f"{1000 * row[key]:.3f}" for key in ("generation_s", "cold_compile_s", "warm_execution_s")))


if __name__ == "__main__":
    main()
