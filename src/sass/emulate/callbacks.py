"""Python lane adapters for the common C++ kernel scheduler."""
import ctypes, re
from functools import cache
from sass.emulate import kernel, machine, metadata, schedule


class Program(kernel.Kernel):
    def __init__(self, arch, code, base):
        self.instructions = [metadata.Instruction(arch, code[i:i + 16]) for i in range(0, len(code), 16)]
        source = '#include "sass/run.hpp"\n#include "sass/abi.hpp"\n' + f"""
static void noop(sass::Ctx &) {{}}
static void (*const CODE[])(sass::Ctx &) = {{{', '.join('noop' for _ in self.instructions)}}};
static const sass::Info INFO[] = {{{', '.join(ins.info(arch, i) for i, ins in enumerate(self.instructions))}}};
static const sass::Program PROGRAM{{{base}, {len(self.instructions)}, CODE, INFO}};
extern "C" uint32_t sass_run(const sass::Launch *l, sass::Result *r) {{ return sass::run(PROGRAM, *l, *r); }}
"""
        self.initialize(source, arch, code, base)


@cache
def compiled(arch, code, base): return Program(arch, code, base)


def run(request, engine, arch):
    spec = request['kernel']
    scope, _ = schedule.options(spec)
    _, base, count, block, _ = schedule.launch(spec)
    lanes = []
    for first in range(0, count, block if scope != 'thread' else 1):
        indices = range(first, min(first + (block if scope != 'thread' else 1), count))
        group = engine.group(indices, scope)
        for index, lane in zip(indices, group):
            for name, value in schedule.seeds(spec, index, block).items(): lane.seed(name, value)
        lanes.extend(group)
    program = compiled(arch, bytes.fromhex(spec['code']), base)
    trace = []

    @kernel.Step
    def step(index, thread, warp, message, size):
        try:
            lane = lanes[index]
            w = warp.contents
            context = dict(request.get('context', {}))
            if scope != 'thread':
                context['synchronization'] = dict(context.get('synchronization', {}), bssy=True, bsync=True, bar=True, warpsync=True)
            if w.present:
                context['warp'] = dict(lane=w.lane, active=w.active, waiting=w.waiting,
                                       values=list(w.values), predicates=list(w.predicates))
            decoded = program.instructions[(thread.contents.pc - base) // 16].decoded
            pc = thread.contents.pc
            taken = "Pg" not in decoded.klass.operand_types or schedule.operand(arch, lane, decoded, "Pg") ^ decoded.env.get(("Pg", "not"), 0)
            lane.step(decoded, context)
            t = thread.contents
            t.pc = lane.pc()
            writes = lane.writes(pc) if taken else {}
            if spec.get("trace") and index < int(spec.get("trace_threads", 1)):
                trace.append(dict(thread=index, pc=pc - base, text=decoded.text, writes=writes))
            for name, value in writes.items():
                file, i = re.fullmatch(r"(UR|UP|R|P)(\d+)", name).groups()
                getattr(t, machine.FILES[file])[int(i)] = value
            return 0
        except Exception as error:
            data = str(error).encode()[:size - 1] + b'\0'
            ctypes.memmove(message, data, len(data))
            return 1

    response = program.run(dict(request, kernel=dict(spec, trace=False)), step_lane=step)
    if spec.get('trace'): response['trace'] = trace
    out = request['output']
    response['outputs'] = [engine.output(lane, int(out['address']) + i * int(out.get('stride', 4)), int(out.get('size', 4))).hex()
                           for i, lane in enumerate(lanes)]
    return response
