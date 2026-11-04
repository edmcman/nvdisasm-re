"""Kernel request validation, decoded scheduling facts and shared diagnostics.
The scheduling loop lives in include/sass/run.hpp; callbacks adapts Python/Ghidra lanes.
"""
from typing import Protocol
from math import prod
from sass.launch import coordinates, geometry
from sass.decode import ir
from sass.md import expression
from sass.semantics.family import roles
from sass.sleigh.registers import ENUM_FILE, REG_FILES

# Instructions reading warp state (collectives, BRA.DIV/.CONV): the runtime warp-context key and the
# operand each lane contributes (None: only the active and waiting masks).
WARP_INPUTS = {'SHFL': ('values', 'Ra'), 'VOTE': ('predicates', 'Pp'), 'VOTEU': ('predicates', 'Pp'), 'MATCH': ('values', 'Ra'),
               'REDUX': ('values', 'Ra'), 'ELECT': ('values', None), 'BRA': ('values', None)}

WARP_GROUPS = {"LDSM": ("Ra", "Ra_URb"), "HMMA": ("Ra", "Rb", "Rc", "Re")}


class Fault(Exception):
    """An instruction failed; the message is the runtime's."""


class Lane(Protocol):
    def pc(self) -> int: ...                           # 0 once exited
    def read(self, file, index) -> int: ...            # one architectural register element
    def seed(self, name, value) -> None: ...           # a launch input register, by name
    def step(self, decoded, context) -> None: ...      # the instruction at pc(); raises Fault
    def writes(self, pc) -> dict: ...                  # {register: value} the instruction at pc writes


class Engine(Protocol):
    def group(self, threads, scope) -> list: ...   # Lanes at the entry, sharing one memory holding the code and request memory
    def output(self, lane, address, size) -> bytes: ...


# The errors of a kernel run, shared with the native engine.
def fault(name, thread, offset, message): return f'kernel {name} thread {thread} at {offset:#x}: {message}'
def limit(max_steps, pc, thread): return f'kernel instruction limit ({max_steps}) at {pc:#x}, thread {thread}'
def outside(name, thread, pc): return f'kernel {name} thread {thread}: PC outside kernel {pc:#x}'
NO_EXIT = 'kernel terminated without EXIT'
INVALID = 'invalid kernel launch or instruction text'


def launch(kernel):
    """(code, base, threads, block, max_steps) of a kernel request, validated."""
    code = bytes.fromhex(kernel['code']); base = int(kernel.get('address', 0x100000))
    _, dims, count = geometry(kernel.get('threads'), kernel.get('block', kernel.get('block_size', 32)), kernel.get('grid'))
    block = prod(dims); max_steps = int(kernel.get('max_steps', 10000))
    if 'block_size' in kernel and kernel['block_size'] != block: raise ValueError("block_size does not match block")
    if count <= 0 or block <= 0 or max_steps <= 0 or not code or len(code) % 16: raise ValueError(INVALID)
    return code, base, count, block, max_steps


def seeds(kernel, thread, block):
    """The registers a thread starts with; the rest start at zero (hardwired RZ/PT aside)."""
    grid, dims, _ = geometry(kernel.get('threads'), kernel.get('block', block), kernel.get('grid'))
    return {**kernel.get('registers', {}),
            **{f'SR_{name}.{axis}': v for name, rank, shape in (('TID', thread % block, dims), ('CTAID', thread // block, grid))
               for axis, v in zip('XYZ', coordinates(rank, shape))},
            'SR_LANEID': thread % block % 32}


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
    if 'ALL' in labels or 'all' in k.operand_types and 'all' not in d.env: return 'all',
    source = next(n for n in ('Ra', 'Rb', 'URb', 'Sb') if n in k.operand_types)
    return ('immediate', d.env[source] & 0xffffffff) if source == 'Sb' else ('operand', source)


def unmodelled(name, text): return f'kernel {name}: {text} is not modelled'
def omits(name, thread, text, mask): return f'kernel {name} thread {thread}: {text} mask {mask:#x} omits the lane'
def disagree(name, a, b): return f'kernel {name}: WARPSYNC masks {a:#x} and {b:#x} disagree'
def stuck(name): return f'kernel {name}: no thread can proceed'


def options(kernel):
    if "cooperative" in kernel: raise ValueError("cooperative was removed; use scope='thread', 'block', or 'launch'")
    scope, order = kernel.get("scope", "launch"), kernel.get("schedule", "round_robin")
    if scope not in ("thread", "block", "launch"): raise ValueError("invalid memory scope: " + str(scope))
    if order not in ("round_robin", "reverse", "seeded"): raise ValueError("invalid schedule: " + str(order))
    if scope != "launch" and order != "round_robin": raise ValueError("schedule requires launch scope")
    match order:
        case "round_robin": return scope, 0
        case "reverse": return scope, 1
        case "seeded":
            seed = int(kernel.get('schedule_seed', 0x5a55))
            if not 0 <= seed < (1 << 32) - 2: raise ValueError("invalid schedule seed")
            return scope, seed + 2


def run_kernel(request, engine, arch):
    from sass.emulate import callbacks
    return callbacks.run(request, engine, arch)
