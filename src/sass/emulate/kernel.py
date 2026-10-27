"""Whole kernels on the native emulator: a kernel request (`sass.launch.kernel_request`) answered
like `sass.emulate.schedule.run_kernel`, from C++ generated per kernel (`codegen.kernel`, run by
include/sass/run.hpp): model 1 for non-cooperative launches (threads in parallel chunks), model 2 for
cooperative ones (blocks in parallel chunks)."""
import ctypes, os
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from sass.decode import ir
from sass.emulate import codegen, machine, schedule

DONE, FAULTED, LIMITED, OUTSIDE, NO_EXIT, UNDECODABLE, UNMODELLED, OMITS, DISAGREE, STUCK = range(10)
NAMES = {v: k for k, v in codegen.FILE_IDS.items()}


class Seed(ctypes.Structure):
    _fields_ = [("file", ctypes.c_uint32), ("index", ctypes.c_uint32), ("value", ctypes.c_uint64)]


class Launch(ctypes.Structure):
    _fields_ = [*((n, ctypes.c_uint64) for n in ("first", "last", "block", "max_steps", "traced")),
                ("image", ctypes.c_void_p), ("seeds", ctypes.POINTER(Seed)),
                *((n, ctypes.c_uint32) for n in ("nseeds", "tid_x", "ctaid_x", "cooperative")),
                ("env", ctypes.POINTER(machine.Environment)),
                *((n, ctypes.c_uint64) for n in ("count", "out_address", "out_size", "out_stride")),
                ("outputs", ctypes.POINTER(ctypes.c_uint8)), ("visited", ctypes.POINTER(ctypes.c_uint8)), ("trace", ctypes.c_void_p)]


class Result(ctypes.Structure):
    _fields_ = [("outcome", ctypes.c_uint32), ("pad", ctypes.c_uint32),
                *((n, ctypes.c_uint64) for n in ("group", "thread", "pc", "steps", "a", "b")), ("message", ctypes.c_char * 512)]


def location(arch, name):
    """(file id, index) of a register named R4, UR2, P0, UP1 or SR_*."""
    for file in ("UR", "UP", "R", "P"):
        if name.startswith(file) and name[len(file):].isdigit(): return codegen.FILE_IDS[file], int(name[len(file):])
    return codegen.FILE_IDS["SR"], ir.load(arch).enums["SpecialRegister"][name]


class Kernel(machine.Library):
    def __init__(self, arch, code, base, fp="exact", definition_calls=True):
        source, self.instructions = codegen.kernel(arch, code, base, fp, definition_calls)
        super().__init__(source, "kernel")
        self.arch, self.code, self.base = arch, code, base
        lib = self.lib
        lib.sass_run.argtypes, lib.sass_run.restype = [ctypes.POINTER(Launch), ctypes.POINTER(Result)], ctypes.c_uint32
        lib.sass_trace_new.restype = ctypes.c_void_p
        lib.sass_trace_free.argtypes = lib.sass_trace_size.argtypes = [ctypes.c_void_p]
        lib.sass_trace_size.restype = ctypes.c_size_t
        lib.sass_trace_step.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint32),
                                        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.POINTER(ctypes.c_uint64)), ctypes.POINTER(ctypes.c_uint32)]

    def text(self, pc): return self.instructions[(pc - self.base) // 16].decoded.text

    def message(self, r, name, max_steps):
        text = lambda: self.text(r.pc)
        return {FAULTED: lambda: schedule.fault(name, r.thread, r.pc - self.base, r.message.decode()),
                LIMITED: lambda: schedule.limit(max_steps, r.pc, r.thread), OUTSIDE: lambda: schedule.outside(name, r.thread, r.pc),
                NO_EXIT: lambda: schedule.NO_EXIT, UNDECODABLE: lambda: r.message.decode(),
                UNMODELLED: lambda: schedule.unmodelled(name, text()), OMITS: lambda: schedule.omits(name, r.thread, text(), r.a),
                DISAGREE: lambda: schedule.disagree(name, r.a, r.b), STUCK: lambda: schedule.stuck(name)}[r.outcome]()

    def steps(self, trace):
        """The trace's steps as schedule records them."""
        out, thread, index, taken, values, count = [], ctypes.c_uint64(), ctypes.c_uint32(), ctypes.c_uint32(), ctypes.POINTER(ctypes.c_uint64)(), ctypes.c_uint32()
        for k in range(self.lib.sass_trace_size(trace)):
            self.lib.sass_trace_step(trace, k, thread, index, taken, values, count)
            ins = self.instructions[index.value]
            dests = codegen.destinations(ins.semantics) if taken.value else []
            out.append(dict(thread=thread.value, pc=16 * index.value, text=ins.decoded.text,
                            writes={f"{file}{n}": values[j] for j, (file, n) in enumerate(dests)}))
        return out

    def run(self, request, workers=None):
        """The response to a kernel request; raises with the scheduler's message on failure."""
        kernel = request["kernel"]; code, base, count, block, max_steps = schedule.launch(kernel)
        name, out, cooperative = kernel.get("name"), request["output"], bool(kernel.get("cooperative"))
        if cooperative and block % 32: raise ValueError(schedule.COOPERATIVE_WARPS)
        size, stride, address = int(out.get("size", 4)), int(out.get("stride", 4)), int(out["address"])
        traced = int(kernel.get("trace_threads", 1)) if kernel.get("trace") else 0
        sr = ir.load(self.arch).enums["SpecialRegister"]
        named = {k: v for k, v in schedule.seeds(kernel, 0, block).items() if k not in ("SR_TID.X", "SR_CTAID.X")}
        seeds = (Seed * max(len(named), 1))(*(Seed(*location(self.arch, k), v) for k, v in named.items()))
        regions = [(m["space"], int(m["address"]), bytes.fromhex(m["hex"])) for m in request.get("memory", [])] + [("ram", base, code)]
        image, env = self.memory(regions), machine.environment(request.get("context", {}))
        unit = block if cooperative else 1   # chunks hold whole blocks
        units = -(-count // unit); work = units * (block if cooperative else 1)
        chunks = max(1, min(units, work // 1024, 4 * (workers or os.cpu_count() or 1)))
        bounds = [(min(count, unit * (units * i // chunks)), min(count, unit * (units * (i + 1) // chunks))) for i in range(chunks)]

        def part(span):
            first, last = span
            outputs, visited, result = (ctypes.c_uint8 * ((last - first) * size or 1))(), (ctypes.c_uint8 * (len(code) // 16))(), Result()
            trace = self.lib.sass_trace_new() if traced else None
            launch = Launch(first, last, block, max_steps, traced, image, seeds, len(named), sr["SR_TID.X"], sr["SR_CTAID.X"], int(cooperative),
                            ctypes.pointer(env), count, address, size, stride, outputs, visited, trace)
            self.lib.sass_run(ctypes.byref(launch), ctypes.byref(result))
            return result, bytes(outputs)[:(last - first) * size], bytes(visited), trace

        parts = []
        try:
            with ThreadPoolExecutor(workers or os.cpu_count()) as pool: parts = list(pool.map(part, bounds))
            if failed := [r for r, _, _, _ in parts if r.outcome != DONE]:
                raise RuntimeError(self.message(min(failed, key=lambda r: r.group), name, max_steps))
            data = b"".join(o for _, o, _, _ in parts)
            visited = sorted(16 * i for i in range(len(code) // 16) if any(v[i] for _, _, v, _ in parts))
            response = dict(outputs=[data[i * size:(i + 1) * size].hex() for i in range(count)], threads=count,
                            steps=sum(r.steps for r, _, _, _ in parts), visited=visited, exit="EXIT")
            if traced: response["trace"] = [s for _, _, _, t in parts for s in self.steps(t)]
            return response
        finally:
            self.free(image)
            for *_, t in parts:
                if t: self.lib.sass_trace_free(t)


@cache
def compiled(arch, code, base, fp): return Kernel(arch, code, base, fp)


def key(request, arch, fp):
    kernel = request["kernel"]
    return arch, bytes.fromhex(kernel["code"]), int(kernel.get("address", 0x100000)), fp


def run_kernel(request, arch, fp="exact"):
    """`schedule.run_kernel`'s response from the native emulator; a failure is {error}."""
    try: return compiled(*key(request, arch, fp)).run(request)
    except Exception as error: return {"error": str(error)}


def emulate_sharded(batches, workers=None):
    """`sass.ghidra_bridge.emulate_sharded`'s contract: the distinct kernels compile in parallel (the
    compiler runs outside the GIL), then each request runs its threads in parallel."""
    kernels = {key(r, arch, fp) for arch, fp, requests in batches.values() for r in requests}
    with ThreadPoolExecutor(workers or os.cpu_count()) as pool: list(pool.map(lambda k: compiled(*k), kernels))
    return {name: [run_kernel(r, arch, fp) for r in requests] for name, (arch, fp, requests) in batches.items()}
