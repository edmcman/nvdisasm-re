"""Whole kernels on the lifted semantics: `sass.emulate.schedule` over lanes running
`sass.semantics.lifter` + `sass.semantics.evaluate`, the Python counterpart of
`sass.ghidra_bridge.ghidra_emulate`'s Ghidra lanes (same requests, same responses)."""
import multiprocessing, re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from functools import lru_cache
from sass.decode import ir
from sass.emulate import schedule
from sass.semantics import evaluate, lifter, ordered, ordered_runtime
from sass.semantics.runtime import Runtime

NAME = re.compile(r"(UR|UP|R|P)(\d+)$")
SHARED = ("ram", "shared", "cbank")   # a block's common spaces; localmem is per thread
TRACED = ("R", "UR", "P", "UP")       # the architectural files a trace reports


@lru_cache(maxsize=1024)
def lifted(function, arch, word, pc, fp):
    # Include the implementation so replacing the lifter cannot reuse stale semantics.
    return function(arch, word, pc, fp)


class Kernel:
    """One request's engine: lifts each instruction once and makes lanes sharing memory per group."""

    def __init__(self, request, arch, fp="exact"):
        kernel = request["kernel"]; self.code = bytes.fromhex(kernel["code"]); self.base = int(kernel.get("address", 0x100000))
        self.block = schedule.launch(kernel)[3]
        self.arch, self.fp, self.runtime, self.lifted = arch, fp, Runtime(int(arch[2:])), {}
        self.global_memory = None
        self.memory = request.get("memory", []) + [dict(space="ram", address=self.base, hex=self.code.hex())]

    def lift(self, pc):
        if pc not in self.lifted:
            word = self.code[pc - self.base:pc - self.base + 16]
            self.lifted[pc] = replace(lifted(lifter.lift, self.arch, word, pc, self.fp))
        return self.lifted[pc]

    def group(self, threads, scope):
        shared = {space: {} for space in SHARED}
        if scope == "launch" and self.global_memory is not None: shared["ram"] = self.global_memory
        lanes = [Lane(self, evaluate.State(memory={**shared, "localmem": {}})) for _ in threads]
        runtime = ordered_runtime.Cooperative(provider=self.runtime)
        for index, lane in zip(threads, lanes): lane.ordered_runtime, lane.index = runtime, index
        for mem in self.memory:
            if scope == "launch" and self.global_memory is not None and mem["space"] == "ram": continue
            data = bytes.fromhex(mem["hex"])
            for lane in lanes if mem["space"] == "localmem" else lanes[:1]:
                lane.state.store(mem["space"], int(mem["address"]), 8 * len(data), int.from_bytes(data, "little"))
        if scope == "launch": self.global_memory = shared["ram"]
        return lanes

    def output(self, lane, address, size): return lane.state.load("ram", address, 8 * size).to_bytes(size, "little")


class Lane:
    def __init__(self, kernel, state):
        self.kernel, self.state = kernel, state
        self.frame, self.progressed = None, False
        state.pc = kernel.base

    def pc(self): return self.state.pc

    def read(self, file, index): return self.state.read(file, index, 1)

    def store(self, space, address, data):
        self.state.store(space, address, len(data) * 8, int.from_bytes(data, "little"))

    def seed(self, name, value):
        m = NAME.match(name)
        file, index = (m[1], int(m[2])) if m else ("SR", ir.load(self.kernel.arch).enums["SpecialRegister"][name])
        self.state.write(file, index, 1, value)

    def step(self, decoded, context):
        semantics = self.kernel.lift(self.state.pc)
        if semantics.status == "opaque": raise schedule.Fault(f"sass_opaque_{re.sub(r'\W', '_', decoded.klass.mnemonic)} ({semantics.reason})")
        runtime = self.kernel.runtime; runtime.context, runtime.events = context, []
        if self.frame is None:
            self.frame = ordered.Frame(semantics.body, semantics.addr + 16)
        frame = self.frame
        before = frame.position, frame.pending, frame.status
        local = self.index % self.kernel.block
        self.ordered_runtime.bind(frame, local % 32, (local // 32, semantics.addr),
                                  participant=self.index)
        status = frame.run(self.state, self.ordered_runtime)
        self.progressed = before != (frame.position, frame.pending, frame.status)
        if status == ordered.Status.FAULT: raise schedule.Fault(str(frame.error)) from frame.error
        if status == ordered.Status.COMPLETE: self.frame = None
        return status

    def writes(self, pc):
        return {f"{file}{index + k}": self.state.read(file, index + k, 1)
                for file, index, count in self.kernel.lift(pc).written_locations() if file in TRACED for k in range(count)}


def run_kernel(request, arch, fp="exact"):
    """`schedule.run_kernel` on lifted lanes; a failure is {error}, as from the Ghidra emulator."""
    try: return schedule.run_kernel(request, Kernel(request, arch, fp), arch)
    except Exception as error: return {"error": str(error)}


def run(job): return run_kernel(job[2], job[0], job[1])


def emulate_sharded(batches, workers=None):
    """`sass.ghidra_bridge.emulate_sharded`'s contract, {key: (arch, fp, requests)} -> {key: results},
    in worker processes."""
    jobs = [(key, (arch, fp, r)) for key, (arch, fp, requests) in batches.items() for r in requests]
    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("forkserver")) as pool:
        results = list(pool.map(run, [job for _, job in jobs]))
    out = {key: [] for key in batches}
    for (key, _), result in zip(jobs, results): out[key].append(result)
    return out
