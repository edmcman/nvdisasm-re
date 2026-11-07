"""Concrete evaluation of `sass.semantics.bitvec` expressions and lifted instructions on a machine state.

This is the definition of the semantics: `sass.semantics.bitvec.OPS` gives each operator's meaning and this
module walks expressions and applies an instruction's writes. Registers are stored per element
(R/UR 32-bit, P/UP 1-bit) and wide locations read and write their elements little-endian.
Widths are in bits; memory is byte-addressed.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from sass.semantics import bitvec
from sass.semantics.bitvec import Expr, mask

ELEMENT = {"R": 32, "UR": 32, "P": 1, "UP": 1, "SR": 32}


@dataclass
class State:
    regs: dict = field(default_factory=dict)      # (file, index) -> element value
    memory: dict = field(default_factory=dict)    # space -> {address: byte}
    pc: int | None = None                         # after execute: the next instruction's address

    def copy(self): return State(dict(self.regs), {s: dict(m) for s, m in self.memory.items()})

    def read(self, file, index, count):
        e = ELEMENT[file]
        return sum((self.regs.get((file, index + j), 0) & mask(e)) << (e * j) for j in range(count))

    def write(self, file, index, count, value):
        e = ELEMENT[file]
        for j in range(count): self.regs[file, index + j] = value >> (e * j) & mask(e)

    def load(self, space, address, width):
        m = self.memory.get(space, {})
        return sum(m.get(address + j, 0) << 8 * j for j in range(width // 8))

    def store(self, space, address, width, value):
        m = self.memory.setdefault(space, {})
        for j in range(width // 8): m[address + j] = value >> 8 * j & 0xff


class Unknown(Exception):
    """The expression needs a value the state can't provide (an unbound parameter)."""


expanded = lru_cache(maxsize=128)(bitvec.lowered)


def evaluate(e, state, params=None, memo=None, runtime=None, *, temporaries=None):
    """e's value (its defined operators by their expansions); a primitive is computed by `runtime`
    (`sass.semantics.runtime.Runtime`)."""
    memo = {} if memo is None else memo
    e = expanded(e)
    def ev(e):
        if not isinstance(e, Expr): return e
        if e in memo: return memo[e]
        match e.op:
            case "const": v = e.args[0]
            case "param":
                if not params or e.args[0] not in params: raise Unknown(f"unbound {e!r}")
                v = params[e.args[0]] & mask(e.w)
            case "temp":
                if temporaries is None or e.args[0] not in temporaries: raise Unknown(f"unbound {e!r}")
                v = temporaries[e.args[0]] & mask(e.w)
            case "reg": v = state.read(*e.args)
            case "load": v = state.load(e.args[0], ev(e.args[1]), e.w)
            case "ite":
                c, a, b = e.args
                v = ev(a if ev(c) else b)
            case "primitive":
                if runtime is None: raise Unknown(f"primitive {e.args[0]} without a runtime")
                v = runtime.call(bitvec.PRIMITIVE_OP + e.args[0], [ev(a) for a in e.args[1:]], (e.w + 7) // 8) & mask(e.w)
            case _: v = bitvec.OPS[e.op]([ev(a) for a in e.args], [a.w if isinstance(a, Expr) else None for a in e.args], e.w)
        memo[e] = v
        return v
    return ev(e)


def apply(semantics, state, params=None, runtime=None):
    """Execute the ordered body in place; suspension requires retaining a Frame."""
    from sass.semantics import ordered, ordered_runtime
    if semantics.body is None: raise Unknown(semantics.reason or "instruction has no ordered body")
    parameters = {name: bitvec.const(params[name], width)
                  for name, width in ordered.parameter_types(semantics.body).items() if name in params} if params else {}
    frame = ordered.Frame(semantics.body, semantics.addr + 16, parameters)
    runtime = runtime if isinstance(runtime, ordered_runtime.Runtime) else ordered_runtime.Explicit(provider=runtime)
    status = frame.run(state, runtime)
    if status == ordered.Status.FAULT: raise frame.error
    if status == ordered.Status.SUSPENDED: raise Unknown("suspended instruction requires its retained frame")
    return state


def execute(semantics, state, params=None, runtime=None):
    """The state after one lifted instruction; `state` is unchanged (see `apply`)."""
    return apply(semantics, state.copy(), params, runtime)
