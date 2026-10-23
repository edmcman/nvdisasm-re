"""One lifted instruction (`sass.semantics.lifter.Semantics`) as a C++ function on the machine of
include/sass/machine.hpp, executing exactly as `sass.semantics.evaluate.apply`: the guard, then
the effects, then every write, store and branch evaluated against the state before any of them,
then the writes, the stores and the next PC. Pure nodes are computed once, eagerly; an `ite` whose
arms reach a primitive (which may fail, as SHFL's read of an invalid lane) keeps its laziness."""
import json
from sass.decode import ir
from sass.semantics import bitvec, primitive
from sass.semantics.bitvec import PRIMITIVE_OP, Expr
from sass.semantics.runtime import UnsupportedSemantics, memory_controls

FILES = {"R": ("t.r", 32), "UR": ("t.ur", 32), "SR": ("t.sr", 32), "P": ("t.p", 1), "UP": ("t.up", 1)}
SPACES = {"ram": "sass::Ram", "shared": "sass::Shared", "localmem": "sass::Localmem", "cbank": "sass::Cbank"}
ROUNDING = {r: f"sass::fp::{r}" for r in bitvec.ROUNDING}
INFIX = {"add": "+", "sub": "-", "mul": "*", "and": "&", "or": "|", "xor": "^"}
COMPARE = {"eq": "==", "ne": "!=", "ult": "<", "ule": "<="}
WRAPPED = {"add", "sub", "mul"}   # results that can carry past w
FP = {"fadd": "add", "fmul": "mul", "ffma": "fma"}
SYNC = {f"{PRIMITIVE_OP}{op}": f"sass::{op.capitalize()}" for op in primitive.SYNCHRONIZATION}


def ty(w): return "sass::u64" if w <= 64 else "sass::u128"


def literal(v, w):
    return f"UINT64_C({v:#x})" if w <= 64 else f"(sass::u128(UINT64_C({v >> 64:#x})) << 64 | UINT64_C({v & (1 << 64) - 1:#x}))"


def masked(expr, w): return expr if w in (64, 128) else f"({expr} & {literal(bitvec.mask(w), w)})"


def string(text): return json.dumps(text)


class Lower:
    def __init__(self, arch):
        self.arch, self.lines, self.scopes, self.count, self.primitive_below = arch, [], [{}], 0, {}

    def emit(self, line): self.lines.append(line)

    def temp(self, w, expr):
        name = f"v{self.count}"; self.count += 1
        self.emit(f"const {ty(w)} {name} = {expr};")
        return name

    def lazy(self, e):
        """Whether e reaches a primitive (memoized per node)."""
        if not isinstance(e, Expr): return False
        if id(e) not in self.primitive_below:
            self.primitive_below[id(e)] = e.op == "primitive" or any(self.lazy(a) for a in e.args)
        return self.primitive_below[id(e)]

    def value(self, e):
        """A C++ name or literal holding e."""
        if not isinstance(e, Expr): return str(e)
        for scope in reversed(self.scopes):
            if id(e) in scope: return scope[id(e)]
        out = self.compute(e)
        self.scopes[-1][id(e)] = out
        return out

    def arm(self, e):
        self.scopes.append({})
        try: return self.value(e)
        finally: self.scopes.pop()

    def compute(self, e):
        a, w = e.args, e.w
        match e.op:
            case "const": return literal(a[0], w)
            case "reg": return self.temp(w, self.read(*a))
            case "load": return self.temp(w, f"c.space({SPACES[a[0]]}).load<{w}>({SPACES[a[0]]}, {self.value(a[1])})")
            case "ite" if self.lazy(a[1]) or self.lazy(a[2]):
                c = self.value(a[0]); name = f"v{self.count}"; self.count += 1
                self.emit(f"{ty(w)} {name};"); self.emit(f"if ({c}) {{")
                self.emit(f"{name} = {self.arm(a[1])};"); self.emit("} else {")
                self.emit(f"{name} = {self.arm(a[2])};"); self.emit("}")
                return name
            case "ite": return self.temp(w, f"({self.value(a[0])} ? {self.value(a[1])} : {self.value(a[2])})")
            case "primitive": return self.temp(w, masked(self.primitive(a[0], [self.value(x) for x in a[1:]]), w))
            case op if op in INFIX:
                expr = f"{self.value(a[0])} {INFIX[op]} {self.value(a[1])}"
                return self.temp(w, masked(f"({expr})", w) if op in WRAPPED else f"({expr})")
            case op if op in COMPARE: return self.temp(w, f"sass::u64({self.value(a[0])} {COMPARE[op]} {self.value(a[1])})")
            case "slt" | "sle":
                x, y = (f"sass::sx<{v.w}>({self.value(v)})" for v in a)
                return self.temp(w, f"sass::u64({x} {'<' if e.op == 'slt' else '<='} {y})")
            case "not": return self.temp(w, masked(f"(~{self.value(a[0])})", w))
            case "neg": return self.temp(w, masked(f"({ty(w)}(0) - {self.value(a[0])})", w))
            case "shl" | "shr" | "sar": return self.temp(w, f"sass::{e.op}<{w}>({self.value(a[0])}, {self.value(a[1])})")
            case "zext": return self.temp(w, f"{ty(w)}({self.value(a[0])})")
            case "sext": return self.temp(w, f"sass::sext<{a[0].w}, {w}>({self.value(a[0])})")
            case "extract": return self.temp(w, f"{ty(w)}(({self.value(a[0])} >> {a[2]}) & {literal(bitvec.mask(w), a[0].w)})")
            case "concat":
                parts, shift = [], w
                for part in a:
                    shift -= part.w
                    parts.append(f"({ty(w)}({self.value(part)}) << {shift})" if shift else f"{ty(w)}({self.value(part)})")
                return self.temp(w, " | ".join(parts))
            case op if op in FP:
                args = ", ".join(self.value(x) for x in a[:3 if op == "ffma" else 2])
                extra = f", {a[3]}" if op == "fmul" else ""
                return self.temp(w, f"sass::fp::{FP[op]}<{w}>({args}, {ROUNDING[a[3 if op == 'ffma' else 2]]}{extra})")
            case "nanresult":
                r = self.value(a[0])
                return self.temp(w, f"(sass::fp::isnan<{w}>({r}) ? {self.value(a[1])} : {r})")
            case "fconv": return self.temp(w, f"sass::fp::convert<{a[0].w}, {w}>({self.value(a[0])}, {ROUNDING[a[1]]})")
            case "flt" | "fle" | "feq": return self.temp(w, f"sass::u64(sass::fp::{e.op[1:]}<{a[0].w}>({self.value(a[0])}, {self.value(a[1])}))")
            case "fnan": return self.temp(w, f"sass::u64(sass::fp::isnan<{a[0].w}>({self.value(a[0])}))")
        raise NotImplementedError(f"IR op {e.op}")

    def read(self, file, index, count):
        array, element = FILES[file]
        w = count * element
        return " | ".join(f"({ty(w)}({array}[{index + j}]) << {element * j})" if j else f"{ty(w)}({array}[{index}])" for j in range(count))

    def write(self, file, index, count, value):
        array, element = FILES[file]
        cast = "uint32_t" if element == 32 else "uint8_t"
        for j in range(count):
            self.emit(f"{array}[{index + j}] = {cast}(({value} >> {element * j}) & {(1 << element) - 1:#x});")

    def primitive(self, name, args):
        family, _, variant = name.partition("_")
        match name:
            case "lane" | "active" | "waiting" | "read_lane" | "ballot" | "match" | "address_space":
                return f"sass::lane::{name}({', '.join(['c', *args])})"
            case "bmov": return f"sass::lane::synchronize(c, sass::Bmov, {args[0]}, true)"
        if family == "redux" and variant in primitive.REDUCTIONS:
            return f"sass::lane::redux(c, sass::prim::Reduction({primitive.REDUCTIONS.index(variant)}), {args[0]})"
        if family == "mufu" and variant.upper() in primitive.MUFU:
            return f"sass::lane::mufu(c, sass::prim::Mufu({primitive.MUFU.index(variant.upper())}), {str(len(args) == 2).lower()}, {args[0]})"
        return f"(throw sass::Fault{{{string('primitive ' + name)}}}, sass::u64(0))"

    def store(self, s):
        """(store, condition, address, value): evaluated now, the address and value only if the condition holds."""
        if s.cond is None: return s, None, self.value(s.address), self.value(s.value)
        cond, address, value = self.value(s.cond), f"a{self.count}", f"s{self.count}"; self.count += 1
        self.emit(f"sass::u64 {address} = 0; {ty(s.value.w)} {value} = 0;")
        self.emit(f"if ({cond}) {{"); self.emit(f"{address} = {self.arm(s.address)};"); self.emit(f"{value} = {self.arm(s.value)};"); self.emit("}")
        return s, cond, address, value

    def effect(self, effect):
        name, args = effect.name, effect.args
        if name == "sass_memory_order":
            if not all(bitvec.is_const(x) for x in args): raise NotImplementedError("dynamic memory controls")
            ordinal, *controls = (x.args[0] for x in args)
            try: memory_controls(ir.load(self.arch), ordinal, controls)
            except UnsupportedSemantics as error: self.emit(f"throw sass::Fault{{{string(str(error))}}};")
        elif name == "sass_memory_descriptor": self.emit(f"sass::check(sass::prim::memory_descriptor({self.value(args[0])}));")
        elif name in SYNC: self.emit(f"sass::lane::synchronize(c, {SYNC[name]}, {self.value(args[0])}, false);")
        else: self.emit(f"throw sass::Fault{{{string(name)}}};")


def instruction(semantics, function, decoded):
    """C++ `static void function(sass::Ctx &c)` executing `semantics` (status opaque: its fault)."""
    head = f"static void {function}(sass::Ctx &c) {{  // {semantics.text}"
    if semantics.status == "opaque":
        stem = "sass_opaque_" + "".join(ch if ch.isalnum() else "_" for ch in decoded.klass.mnemonic)
        return "\n".join([head, f"  throw sass::Fault{{{string(f'{stem} ({semantics.reason})')}}};", "}"])
    low = Lower(semantics.arch)
    low.emit("auto &t = c.t;")
    if semantics.guard is not None:
        low.emit(f"if (!{low.value(semantics.guard)}) {{ t.pc = UINT64_C({semantics.addr + 16:#x}); return; }}")
    for effect in semantics.effects: low.effect(effect)
    values = [(location, low.value(v)) for location, v in semantics.writes]
    stores = [low.store(s) for s in semantics.stores]
    pc = f"UINT64_C({semantics.addr + 16:#x})"
    if flow := semantics.flow:
        target = low.value(flow.target)
        pc = target if flow.cond is None else f"({low.value(flow.cond)} ? {target} : {pc})"
    low.emit(f"const sass::u64 next = {pc};")
    for location, v in values: low.write(*location.args, v)
    for s, cond, address, value in stores:
        store = f"c.space({SPACES[s.space]}).store<{s.value.w}>({SPACES[s.space]}, {address}, {value});"
        low.emit(store if cond is None else f"if ({cond}) {store}")
    low.emit("t.pc = next;")
    return "\n".join([head, *("  " + line for line in low.lines), "}"])
