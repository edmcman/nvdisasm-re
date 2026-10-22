"""Concrete evaluation of `sass.semantics.bitvec` expressions and lifted instructions on a machine state.

This is the definition of the semantics: `sass.semantics.bitvec.OPS` gives each operator's meaning and this
module walks expressions and applies an instruction's writes. Registers are stored per element
(R/UR 32-bit, P/UP 1-bit) and wide locations read and write their elements little-endian.
Widths are in bits; memory is byte-addressed.
"""
from dataclasses import dataclass, field
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


def evaluate(e, state, params=None, memo=None, runtime=None):
    """e's value; a primitive is computed by `runtime` (`sass.semantics.runtime.Runtime`)."""
    memo = {} if memo is None else memo
    def ev(e):
        if not isinstance(e, Expr): return e
        if e in memo: return memo[e]
        match e.op:
            case "const": v = e.args[0]
            case "param":
                if not params or e.args[0] not in params: raise Unknown(f"unbound {e!r}")
                v = params[e.args[0]] & mask(e.w)
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


def execute(semantics, state, params=None, runtime=None):
    """The state after one lifted instruction (`sass.semantics.lifter.Semantics`): its effects
    (runtime calls) first, then writes evaluated against the state before any of them and applied
    in order, if the guard holds."""
    memo = {}
    value = lambda e: evaluate(e, state, params, memo, runtime)
    out = state.copy(); out.pc = semantics.addr + 16
    if semantics.guard is not None and not value(semantics.guard): return out
    for effect in semantics.effects:
        if runtime is None: raise Unknown(f"effect {effect.name} without a runtime")
        runtime.call(effect.name, [value(a) for a in effect.args], 0)
    values = [(loc, value(v)) for loc, v in semantics.writes]
    stores = [(s.space, value(s.address), s.value.w, value(s.value))
              for s in semantics.stores if s.cond is None or value(s.cond)]
    flow = semantics.flow
    if flow and (flow.cond is None or value(flow.cond)):
        out.pc = value(flow.target)
    for loc, v in values: out.write(*loc.args, v)
    for space, address, w, v in stores: out.store(space, address, w, v)
    return out
