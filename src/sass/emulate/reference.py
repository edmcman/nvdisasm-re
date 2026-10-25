"""Whole kernels on the lifted semantics: `sass.emulate.schedule` over lanes running
`sass.semantics.lifter` + `sass.semantics.evaluate`, the Python counterpart of
`sass.ghidra_bridge.ghidra_emulate`'s Ghidra lanes (same requests, same responses)."""
import multiprocessing, re
from concurrent.futures import ProcessPoolExecutor
from sass.decode import ir
from sass.emulate import schedule
from sass.semantics import evaluate, lifter
from sass.semantics.runtime import Runtime, UnsupportedSemantics

NAME = re.compile(r"(UR|UP|R|P)(\d+)$")
SHARED = ("ram", "shared", "cbank")   # a cooperative block's common spaces; localmem is per thread
TRACED = ("R", "UR", "P", "UP")       # the architectural files a trace reports


class Kernel:
    """One request's engine: lifts each instruction once and makes lanes sharing memory per group."""

    def __init__(self, request, arch, fp="exact"):
        kernel = request["kernel"]; self.code = bytes.fromhex(kernel["code"]); self.base = int(kernel.get("address", 0x100000))
        self.arch, self.fp, self.runtime, self.lifted = arch, fp, Runtime(int(arch[2:])), {}
        self.memory = request.get("memory", []) + [dict(space="ram", address=self.base, hex=self.code.hex())]

    def lift(self, pc):
        if pc not in self.lifted: self.lifted[pc] = lifter.lift(self.arch, self.code[pc - self.base:pc - self.base + 16], pc, self.fp)
        return self.lifted[pc]

    def group(self, threads, cooperative):
        shared = {space: {} for space in SHARED}
        lanes = [Lane(self, evaluate.State(memory={**shared, "localmem": {}})) for _ in threads]
        for mem in self.memory:
            data = bytes.fromhex(mem["hex"])
            lanes[0].state.store(mem["space"], int(mem["address"]), 8 * len(data), int.from_bytes(data, "little"))
        return lanes

    def output(self, lane, address, size): return lane.state.load("ram", address, 8 * size).to_bytes(size, "little")


class Lane:
    def __init__(self, kernel, state):
        self.kernel, self.state = kernel, state
        state.pc = kernel.base

    def pc(self): return self.state.pc

    def read(self, file, index): return self.state.read(file, index, 1)

    def seed(self, name, value):
        m = NAME.match(name)
        file, index = (m[1], int(m[2])) if m else ("SR", ir.load(self.kernel.arch).enums["SpecialRegister"][name])
        self.state.write(file, index, 1, value)

    def step(self, decoded, context):
        semantics = self.kernel.lift(self.state.pc)
        if semantics.status == "opaque": raise schedule.Fault(f"sass_opaque_{re.sub(r'\W', '_', decoded.klass.mnemonic)} ({semantics.reason})")
        runtime = self.kernel.runtime; runtime.context, runtime.events = context, []
        try: evaluate.apply(semantics, self.state, runtime=runtime)
        except (UnsupportedSemantics, evaluate.Unknown) as error: raise schedule.Fault(str(error)) from error

    def writes(self, pc):
        return {f"{file}{index + k}": self.state.read(file, index + k, 1)
                for location, _ in self.kernel.lift(pc).writes for file, index, count in [location.args] if file in TRACED for k in range(count)}


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
