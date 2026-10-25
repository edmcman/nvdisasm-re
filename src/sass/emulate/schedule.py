"""Whole-kernel scheduling shared by the emulators, so that Ghidra's p-code lanes
(`sass.ghidra_bridge.ghidra_emulate`) and the lifted semantics' lanes (`sass.emulate.reference`) run
a kernel request (`sass.launch.kernel_request`) identically: the same seeding, interleaving,
synchronization, limits, errors and response. An engine supplies the lanes; this module only
decodes (it must stay importable from pyghidra without the generators).

Threads start at the kernel entry and run to EXIT, each with its own memory. With
kernel.cooperative, a block shares memory (private localmem); its warps take turns, each turn
stepping the live lanes at the warp's lowest PC together (they form the active mask; a warp
collective first gathers each lane's operand, WARP_INPUTS), and BAR.SYNC holds a thread until every
live thread of the block arrives (barrier ids and thread counts are not modelled). WARPSYNC holds a
lane until every live lane its mask names waits at a WARPSYNC with the same mask (CUDA's __syncwarp
contract; exited lanes are not waited for). Blocks run one after another."""
from typing import Protocol
from sass.decode import ir, pydecode
from sass.md import expression
from sass.semantics.family import roles
from sass.sleigh.registers import ENUM_FILE, REG_FILES

# Instructions reading warp state (collectives, BRA.DIV/.CONV): the runtime warp-context key and the
# operand each lane contributes (None: only the active and waiting masks).
WARP_INPUTS = {'SHFL': ('values', 'Ra'), 'VOTE': ('predicates', 'Pp'), 'VOTEU': ('predicates', 'Pp'), 'MATCH': ('values', 'Ra'),
               'REDUX': ('values', 'Ra'), 'ELECT': ('values', None), 'BRA': ('values', None)}


class Fault(Exception):
    """An instruction failed; the message is the runtime's."""


class Lane(Protocol):
    def pc(self) -> int: ...                           # 0 once exited
    def read(self, file, index) -> int: ...            # one architectural register element
    def seed(self, name, value) -> None: ...           # a launch input register, by name
    def step(self, decoded, context) -> None: ...      # the instruction at pc(); raises Fault
    def writes(self, pc) -> dict: ...                  # {register: value} the instruction at pc writes


class Engine(Protocol):
    def group(self, threads, cooperative) -> list: ...   # Lanes at the entry, sharing one memory holding the code and request memory
    def output(self, lane, address, size) -> bytes: ...


class Thread:
    """The scheduler's view of one lane."""
    __slots__ = ('lane', 'index', 'last', 'steps', 'waiting', 'released', 'sync')

    def __init__(self, lane, index, base):
        self.lane, self.index, self.last, self.steps, self.waiting, self.released, self.sync = lane, index, base, 0, False, False, None

    @property
    def pc(self): return self.lane.pc()


# The errors of a kernel run, shared with the native engine.
def fault(name, thread, offset, message): return f'kernel {name} thread {thread} at {offset:#x}: {message}'
def limit(max_steps, pc, thread): return f'kernel instruction limit ({max_steps}) at {pc:#x}, thread {thread}'
def outside(name, thread, pc): return f'kernel {name} thread {thread}: PC outside kernel {pc:#x}'
NO_EXIT = 'kernel terminated without EXIT'
INVALID = 'invalid kernel launch or instruction text'


def launch(kernel):
    """(code, base, threads, block, max_steps) of a kernel request, validated."""
    code = bytes.fromhex(kernel['code']); base = int(kernel.get('address', 0x100000))
    count = int(kernel['threads']); block = int(kernel.get('block_size', 32)); max_steps = int(kernel.get('max_steps', 10000))
    if count <= 0 or block <= 0 or max_steps <= 0 or not code or len(code) % 16: raise ValueError(INVALID)
    return code, base, count, block, max_steps


def seeds(kernel, thread, block):
    """The registers a thread starts with; the rest start at zero (hardwired RZ/PT aside)."""
    return {**kernel.get('registers', {}), 'SR_TID.X': thread % block, 'SR_TID.Y': 0, 'SR_TID.Z': 0,
            'SR_CTAID.X': thread // block, 'SR_CTAID.Y': 0, 'SR_CTAID.Z': 0}


def operand_location(arch, d, name):
    """Where a decoded register operand lives: (file, index, span) over its md span (MATCH.U64's
    pair), or ('const', value) for the hardwired zero registers (RZ, PT, ...)."""
    file = ENUM_FILE[d.klass.operand_types[name].type]; zero = REG_FILES[file][3]
    if d.env[name] == zero: return 'const', int(file in ('P', 'UP'))
    span = next((o['span'] for o in roles(arch)[d.klass.name]['operands'] if o['name'] == name), 1) if d.klass.name in roles(arch) else 1
    return file, d.env[name], int(expression.evaluate(span, ir.load(arch), d.env)) if isinstance(span, str) else span


def operand(arch, lane, d, name):
    """A decoded register operand's value in a lane."""
    match operand_location(arch, d, name):
        case 'const', value: return value
        case file, index, span: return sum(lane.read(file, index + k) << 8 * REG_FILES[file][1] * k for k in range(span))


def sync_form(arch, d):
    """A WARPSYNC's member mask: ('all',), ('immediate', mask), ('operand', name), or ('unmodelled',)
    for the EXCLUSIVE, COLLECTIVE and constant-bank forms."""
    k = d.klass; labels = {ir.load(arch).rev_enums[k.operand_types[n].type][d.env[n]] for n in ('ex', 'div', 'all') if n in d.env}
    if k.name not in ('warpsync__RIR', 'warpsync__RRR', 'warpsync__RUR') or labels - {'noexclusive', 'nodiv__exclusive', 'ALL'}: return 'unmodelled',
    if 'ALL' in labels: return 'all',
    source = next(n for n in ('Ra', 'Rb', 'URb', 'Sb') if n in k.operand_types)
    return ('immediate', d.env[source] & 0xffffffff) if source == 'Sb' else ('operand', source)


def unmodelled(name, text): return f'kernel {name}: {text} is not modelled'
def omits(name, thread, text, mask): return f'kernel {name} thread {thread}: {text} mask {mask:#x} omits the lane'
def disagree(name, a, b): return f'kernel {name}: WARPSYNC masks {a:#x} and {b:#x} disagree'
def stuck(name): return f'kernel {name}: no thread can proceed'
COOPERATIVE_WARPS = 'cooperative execution requires whole 32-thread warps'


def run_kernel(request, engine, arch):
    """The response to a whole-kernel request: {outputs, threads, steps, visited, exit[, trace]}."""
    kernel = request['kernel']; code, base, count, block, max_steps = launch(kernel); outputs = []; steps = 0; visited = set()
    name = kernel.get('name'); cooperative = bool(kernel.get('cooperative'))
    trace = [] if kernel.get('trace') else None; traced = int(kernel.get('trace_threads', 1))
    decoded = {}

    def decode(pc):
        if pc not in decoded: decoded[pc] = pydecode.decode(arch, code[pc - base:pc - base + 16])
        return decoded[pc]

    def holds(t, d, name): return operand(arch, t.lane, d, name) ^ d.env.get((name, 'not'), 0)

    def step(t, pc, context):
        if t.steps >= max_steps: raise RuntimeError(limit(max_steps, pc, t.index))
        t.last = pc
        traced_step = trace is not None and t.index < traced
        # A write is reported even when it leaves the value unchanged; a predicated-off instruction writes nothing.
        d = decode(pc); taken = traced_step and ('Pg' not in d.klass.operand_types or holds(t, d, 'Pg'))
        try: t.lane.step(d, context)
        except Fault as error: raise RuntimeError(fault(name, t.index, pc - base, error)) from error
        t.steps += 1
        if traced_step: trace.append(dict(thread=t.index, pc=pc - base, text=d.text, writes=t.lane.writes(pc) if taken else {}))

    def sync_mask(t, d):
        """The member mask of t's WARPSYNC."""
        match sync_form(arch, d):
            case 'unmodelled',: raise RuntimeError(unmodelled(name, d.text))
            case 'all',: return 0xffffffff
            case 'immediate', mask: pass
            case 'operand', source: mask = operand(arch, t.lane, d, source) & 0xffffffff
        if not mask >> t.index % 32 & 1: raise RuntimeError(omits(name, t.index, d.text, mask))
        return mask

    def release_warpsyncs(warp):
        for t in warp:
            if t.sync is None: continue
            members = [u for u in warp if t.sync >> u.index % 32 & 1 and u.pc != 0]
            if any(u.sync is None for u in members): continue
            if any(u.sync != t.sync for u in members):
                raise RuntimeError(disagree(name, t.sync, next(u.sync for u in members if u.sync != t.sync)))
            for u in members: u.waiting, u.released, u.sync = False, True, None

    def turn(warp):
        """One lockstep instruction of a warp; False if none of its lanes could move."""
        release_warpsyncs(warp)
        live = [t for t in warp if t.pc != 0 and not t.waiting]
        if not live: return False
        pc = min(t.pc for t in live); active = [t for t in live if t.pc == pc]
        if not base <= pc < base + len(code) or (pc - base) % 16:
            raise RuntimeError(outside(name, active[0].index, pc))
        inputs = None; visited.add(pc - base); d = decode(pc); context = dict(request.get('context', {}))
        guarded = cooperative and 'Pg' in d.klass.operand_types
        taking = [t for t in active if holds(t, d, 'Pg')] if guarded else active
        if cooperative and d.klass.name.startswith('bar__SYNC') and not all(t.released for t in taking):
            for t in taking: t.waiting = True
            active = [t for t in active if t not in taking]
        elif cooperative and d.klass.mnemonic == 'WARPSYNC':
            held = [t for t in taking if not t.released and holds(t, d, 'Pp')]
            for t in held: t.waiting, t.sync = True, sync_mask(t, d)
            active = [t for t in active if t not in held]
        elif inputs := WARP_INPUTS.get(d.klass.mnemonic) if cooperative else None:
            key, source = inputs; values = [0] * 32
            for t in taking: values[t.index % 32] = operand(arch, t.lane, d, source) if source else 0
            # waiting: lanes still alive that are not at this instruction (BRA.DIV/.CONV)
            context['warp'] = {'active': sum(1 << t.index % 32 for t in taking), key: values,
                               'waiting': sum(1 << t.index % 32 for t in warp if t.pc != 0 and t not in active)}
        for t in active:
            step(t, pc, dict(context, warp=dict(context['warp'], lane=t.index % 32)) if inputs else context)
            t.released = False
        return True

    if cooperative and block % 32: raise ValueError(COOPERATIVE_WARPS)
    for group in ([range(b, min(b + block, count)) for b in range(0, count, block)] if cooperative else [[t] for t in range(count)]):
        threads = [Thread(lane, index, base) for lane, index in zip(engine.group(group, cooperative), group)]
        for t in threads:
            for register, value in seeds(kernel, t.index, block).items(): t.lane.seed(register, value)
        warps = [threads[i:i + 32] for i in range(0, len(threads), 32)]
        while live := [t for t in threads if t.pc != 0]:
            if all(t.waiting and t.sync is None for t in live):
                for t in live: t.waiting, t.released = False, True
            if not any([turn(w) for w in warps]):
                raise RuntimeError(stuck(name))
        steps += sum(t.steps for t in threads)
        for t in threads:
            if decode(t.last).klass.mnemonic != 'EXIT': raise RuntimeError(NO_EXIT)
            out = request['output']; offset = int(out['address']) + t.index * int(out.get('stride', 4))
            outputs.append(engine.output(t.lane, offset, int(out.get('size', 4))).hex())
    return dict(outputs=outputs, threads=count, steps=steps, visited=sorted(visited), exit='EXIT', **({'trace': trace} if trace is not None else {}))
