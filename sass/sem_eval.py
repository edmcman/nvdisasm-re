"""Concrete evaluation of `sass.sem` expressions and lifted instructions on a machine state.

This is the definition of the semantics: `sass.sem.OPS` gives each operator's meaning and this
module walks expressions and applies an instruction's writes. Registers are stored per element
(R/UR 32-bit, P/UP 1-bit) and wide locations read and write their elements little-endian.
Widths are in bits; memory is byte-addressed.
"""
from dataclasses import dataclass, field
from sass import sem
from sass.sem import Expr, mask

ELEMENT = {"R": 32, "UR": 32, "P": 1, "UP": 1, "SR": 32}


@dataclass
class State:
    regs: dict = field(default_factory=dict)      # (file, index) -> element value
    memory: dict = field(default_factory=dict)    # space -> {address: byte}

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


def evaluate(e, state, params=None, memo=None):
    memo = {} if memo is None else memo
    if not isinstance(e, Expr): return e
    if e in memo: return memo[e]
    match e.op:
        case "const": v = e.args[0]
        case "param":
            if not params or e.args[0] not in params: raise Unknown(f"unbound {e!r}")
            v = params[e.args[0]] & mask(e.w)
        case "reg": v = state.read(*e.args)
        case "load": v = state.load(e.args[0], evaluate(e.args[1], state, params, memo), e.w)
        case "ite":
            c, a, b = e.args
            v = evaluate(a if evaluate(c, state, params, memo) else b, state, params, memo)
        case _: v = sem.OPS[e.op]([evaluate(a, state, params, memo) for a in e.args],
                                  [a.w if isinstance(a, Expr) else None for a in e.args], e.w)
    memo[e] = v
    return v


def execute(semantics, state, params=None):
    """The state after one lifted instruction (`sass.sem_lift.Semantics`): writes are evaluated
    against the state before any of them, then applied in order, if the guard holds."""
    memo = {}
    if semantics.guard is not None and not evaluate(semantics.guard, state, params, memo): return state.copy()
    values = [(loc, evaluate(v, state, params, memo)) for loc, v in semantics.writes]
    out = state.copy()
    for loc, v in values: out.write(*loc.args, v)
    return out
