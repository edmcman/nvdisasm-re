"""Export lifted SASS to K. Exporting needs no K installation."""
from dataclasses import dataclass
import json
from pathlib import Path

from sass.decode import pydecode
from sass.semantics import bitvec, lifter

SOURCES = tuple(Path(__file__).with_name(name) for name in ("bits.md", "float.md", "sass.md"))
DEFINITION = SOURCES[-1]
VERSION = "7.1.337"
IMAGE = "nvdisasm-re-k:7.1.337"

# Keep this explicit: a new executable IR operator requires a K rule, not an opaque fallback.
OPERATORS = frozenset("""
add sub mul and or xor not neg shl shr sar eq ne ult ule slt sle
zext sext extract concat fadd fmul ffma nanresult flt fle feq fnan fconv
const param reg load ite primitive
""".split())
ELEMENT = {"R": 32, "UR": 32, "P": 1, "UP": 1, "SR": 32}


class ExportError(ValueError):
    """An input cannot be represented by the executable K model."""


def string(value):
    return json.dumps(value, ensure_ascii=True)


def klist(items):
    return " ".join(f"ListItem({item})" for item in items) or ".List"


def kmap(items):
    return " ".join(f"({key} |-> {value})" for key, value in items) or ".Map"


@dataclass(frozen=True, order=True)
class Contract:
    name: str
    arguments: tuple[int, ...]
    result: int
    kind: str
    context: str


def context(name):
    if name.startswith("sass_prim_mufu_"): return "hardware approximation"
    if name in ("sass_memory_order", "sass_memory_descriptor"): return "validated memory controls"
    if name == "sass_prim_address_space": return "generic address ranges"
    if name.removeprefix("sass_prim_") in ("bar", "bssy", "bsync", "break", "warpsync", "bmov"):
        return "synchronization environment"
    if name.removeprefix("sass_prim_") in ("lane", "active", "waiting", "read_lane", "ballot", "match") or name.startswith("sass_prim_redux_"):
        return "explicit warp context"
    return "external runtime contract"


@dataclass(frozen=True)
class Export:
    source: str
    program: str
    contracts: tuple[Contract, ...]
    instructions: tuple[dict, ...]

    def manifest(self):
        """JSON-compatible provenance and runtime assumptions; no verification claim."""
        from dataclasses import asdict
        return dict(format=1, k_version=VERSION,
                    contracts=[asdict(c) for c in self.contracts],
                    instructions=list(self.instructions))


class Encoder:
    def __init__(self):
        self.nodes = {}
        self.lowering = {}
        self.contracts = set()
        self.checked = set()
        self.definitions = set()

    def check(self, expr):
        if not isinstance(expr, bitvec.Expr): raise ExportError("expected an expression")
        if expr in self.checked: return
        if expr.op not in OPERATORS and expr.op not in bitvec.DEFINED:
            raise ExportError(f"unexecutable IR operator {expr.op!r}")
        if type(expr.w) is not int or expr.w <= 0:
            raise ExportError("expression width must be positive")
        self.checked.add(expr)
        for arg in expr.args:
            if isinstance(arg, bitvec.Expr): self.check(arg)
        if expr.op in bitvec.DEFINED: return
        op, args, width = expr.op, expr.args, expr.w
        unary = {"not", "neg", "zext", "sext", "fnan"}
        binary = {"add", "sub", "mul", "and", "or", "xor", "eq", "ne", "ult", "ule", "slt", "sle",
                  "shl", "shr", "sar", "nanresult", "flt", "fle", "feq"}
        arity = {**dict.fromkeys(unary, 1), **dict.fromkeys(binary, 2),
                 "const": 1, "param": 1, "reg": 3, "load": 2, "ite": 3, "extract": 3,
                 "fadd": 3, "fmul": 4, "ffma": 4, "fconv": 2}
        if op in arity and len(args) != arity[op]:
            raise ExportError(f"{op}: invalid argument count")
        expressions = [a for a in args if isinstance(a, bitvec.Expr)]
        expected = (1 if op in unary else 2 if op in binary else
                    {"const": 0, "param": 0, "reg": 0, "load": 1, "ite": 3, "extract": 1,
                     "fadd": 2, "fmul": 2, "ffma": 3, "fconv": 1}.get(op))
        if expected is not None and len(expressions) != expected:
            raise ExportError(f"{op}: invalid expression arguments")
        if op == "const" and type(args[0]) is not int or op == "param" and not isinstance(args[0], str):
            raise ExportError(f"{op}: invalid leaf")
        if op == "const" and not 0 <= args[0] < 1 << width:
            raise ExportError("const: value outside its width")
        if op == "load" and (not isinstance(args[0], str) or width % 8):
            raise ExportError("invalid memory load")
        if op == "reg":
            file, index, count = args
            if file not in ELEMENT or type(index) is not int or type(count) is not int or index < 0 or count <= 0:
                raise ExportError("invalid register location")
            if width != count * ELEMENT[file]: raise ExportError("invalid register width")
        if op in binary - {"shl", "shr", "sar"} and args[0].w != args[1].w:
            raise ExportError(f"{op}: operand width mismatch")
        if op in {"eq", "ne", "ult", "ule", "slt", "sle", "flt", "fle", "feq", "fnan"} and width != 1:
            raise ExportError(f"{op}: result must be one bit")
        if op in {"add", "sub", "mul", "and", "or", "xor", "not", "neg", "shl", "shr", "sar", "nanresult"} and args[0].w != width:
            raise ExportError(f"{op}: result width mismatch")
        if op in {"zext", "sext"} and args[0].w > width:
            raise ExportError(f"{op}: cannot narrow")
        if op == "ite" and (args[0].w != 1 or args[1].w != width or args[2].w != width):
            raise ExportError("ite: invalid widths")
        if op == "extract":
            _, high, low = args
            if type(high) is not int or type(low) is not int or not 0 <= low <= high < args[0].w or high - low + 1 != width:
                raise ExportError("extract: invalid slice")
        if op == "concat" and (len(expressions) != len(args) or sum(a.w for a in expressions) != width):
            raise ExportError("concat: invalid widths")
        if op in {"fadd", "fmul", "ffma", "fconv"}:
            mode = args[3] if op == "ffma" else args[1] if op == "fconv" else args[2]
            if mode not in ("RN", "RZ", "RM", "RP") or width not in (16, 32, 64):
                raise ExportError(f"{op}: invalid IEEE format or rounding mode")
            if op != "fconv" and any(a.w != width for a in expressions):
                raise ExportError(f"{op}: operand width mismatch")
            if op == "fmul" and type(args[3]) is not int: raise ExportError("fmul: invalid scale")
        if op in {"fconv", "fnan", "flt", "fle", "feq"} and args[0].w not in (16, 32, 64):
            raise ExportError(f"{op}: invalid IEEE input format")
        if op == "nanresult" and width not in (16, 32, 64):
            raise ExportError("nanresult: invalid IEEE format")
        if op == "primitive" and (not args or not isinstance(args[0], str) or len(expressions) != len(args) - 1):
            raise ExportError("invalid primitive signature")

    def expr(self, expr):
        if expr is None: return "absent"
        self.check(expr)
        expr = bitvec.lowered(expr, self.lowering, lambda _, node: self.definitions.add(node.op))
        self.check(expr)
        if expr.op not in OPERATORS:
            raise ExportError(f"unexecutable IR operator {expr.op!r}")
        if expr.w <= 0:
            raise ExportError("expression width must be positive")
        if expr.op == "reg":
            file, index, count = expr.args
            if file not in ELEMENT or index < 0 or count <= 0 or expr.w != count * ELEMENT[file]:
                raise ExportError("invalid register location")
        if expr.op == "load" and (expr.w % 8 or not isinstance(expr.args[0], str)):
            raise ExportError("invalid memory load")
        if expr in self.nodes: return self.nodes[expr][1]
        ident = len(self.nodes)
        self.nodes[expr] = (ident, None)
        args = []
        for arg in expr.args:
            if isinstance(arg, bitvec.Expr): args.append(self.expr(arg))
            elif isinstance(arg, str): args.append(string(arg))
            elif type(arg) is int: args.append(str(arg))
            else: raise ExportError(f"unsupported argument of {expr.op}: {type(arg).__name__}")
        if expr.op == "primitive":
            name, *values = expr.args
            if not isinstance(name, str) or any(not isinstance(v, bitvec.Expr) for v in values):
                raise ExportError("invalid primitive signature")
            self.contracts.add(Contract(bitvec.PRIMITIVE_OP + name,
                                        tuple(v.w for v in values), expr.w, "value",
                                        context(bitvec.PRIMITIVE_OP + name)))
        result = f"node({ident}, {string(expr.op)}, {expr.w}, {klist(args)})"
        self.nodes[expr] = (ident, result)
        return result

    def instruction(self, sem):
        if sem.status not in ("native", "primitive", "opaque"):
            raise ExportError(f"unknown semantics status {sem.status!r}")
        guard = self.expr(sem.guard)
        if sem.status == "opaque":
            return f"unsupported({sem.addr}, {guard}, {string(sem.reason or 'opaque semantics')})"
        writes = []
        for loc, value in sem.writes:
            if loc.op != "reg" or loc.w != value.w: raise ExportError("invalid register write")
            self.expr(loc)  # validate its shape
            file, index, count = loc.args
            writes.append(f"write({string(file)}, {index}, {count}, {self.expr(value)})")
        stores = []
        for store in sem.stores:
            if store.value.w % 8: raise ExportError("store width must be a multiple of eight")
            stores.append(f"store({string(store.space)}, {self.expr(store.address)}, "
                          f"{self.expr(store.value)}, {self.expr(store.cond)})")
        effects = []
        for index, effect in enumerate(sem.effects):
            args = [self.expr(a) for a in effect.args]
            self.contracts.add(Contract(effect.name, tuple(a.w for a in effect.args), 0,
                                        "effect", context(effect.name)))
            # Negative ids distinguish effects from expression ids.
            effects.append(f"effect({-index - 1}, {string(effect.name)}, {klist(args)})")
        flow = "noFlow" if sem.flow is None else (
            f"flow({string(sem.flow.kind)}, {self.expr(sem.flow.target)}, {self.expr(sem.flow.cond)})")
        return (f"instruction({sem.addr}, {guard}, {klist(effects)}, "
                f"{klist(writes)}, {klist(stores)}, {flow})")


def _export(instructions, base):
    terms, contracts, metadata = [], set(), []
    for sem in instructions:
        if type(sem.addr) is not int or not 0 < sem.addr < 1 << 64 or sem.addr % 16:
            raise ExportError("instruction address must be aligned and nonzero; PC zero terminates execution")
        encoder = Encoder()
        terms.append((str(sem.addr), encoder.instruction(sem)))
        contracts.update(encoder.contracts)
        metadata.append(dict(arch=sem.arch, address=sem.addr, cls=sem.cls,
                             text=sem.text, status=sem.status, reason=sem.reason,
                             definitions=sorted(encoder.definitions),
                             runtime_sites=[
                                 dict(id=ident, name=bitvec.PRIMITIVE_OP + node.args[0],
                                      kind="value", width=node.w,
                                      arguments=[encoder.expr(a) for a in node.args[1:]])
                                 for node, (ident, _) in list(encoder.nodes.items())
                                 if node.op == "primitive"
                             ] + [
                                 dict(id=-index - 1, name=effect.name, kind="effect", width=0,
                                      arguments=[encoder.expr(a) for a in effect.args])
                                 for index, effect in enumerate(sem.effects)
                             ]))
    if not terms: raise ExportError("program is empty")
    program = f"program({kmap(terms)}, {base})"
    comments = "\n".join("// " + string(m["text"]) for m in metadata)
    source = (f'requires "sass.md"\n\nmodule SASS-EXPORTED\n  imports SASS\n'
              f'  syntax Program ::= exportedProgram() [function]\n'
              f'  {comments}\n  rule exportedProgram() => {program}\nendmodule\n')
    return Export(source, program, tuple(sorted(contracts)), tuple(metadata))


def export_instruction(semantics):
    """Export one already-lifted instruction, retaining its original address."""
    return _export([semantics], semantics.addr)


def export_program(arch, code, base=0x100000, fp="exact"):
    """Lift and export a nonempty sequence of 16-byte instruction words.

    PC zero is reserved for termination; base must be positive and 16-byte aligned.
    Unmodelled decoded instructions remain explicit unsupported transitions.
    """
    if not isinstance(code, bytes) or not code or len(code) % 16:
        raise ExportError("code must be nonempty bytes with a multiple-of-16 length")
    if type(base) is not int or base <= 0 or base % 16 or base + len(code) > 1 << 64:
        raise ExportError("base must be an aligned, nonzero 64-bit address")
    if fp not in ("exact", "readable"): raise ExportError("invalid floating-point mode")
    instructions = []
    for offset in range(0, len(code), 16):
        try:
            instructions.append(lifter.lift(arch, code[offset:offset + 16], base + offset, fp=fp))
        except pydecode.NoMatch as error:
            raise ExportError(f"{arch} at {base + offset:#x}: {error}") from error
    return _export(instructions, base)


def input_term(export, state=None, *, params=None, responses=(), environment=None, limit=10000):
    """K input for a program and an explicit environment response transcript.

    A response has step, pc, id, name, arguments, width, value, events, before and environment.
    Events and environments are K terms, defaulting to .List and the initial environment.
    Argument/pre-environment equality and result bounds are checked by K.
    """
    if type(limit) is not int or limit <= 0: raise ExportError("limit must be positive")
    regs = kmap((f"location({string(f)}, {i})", str(v))
                for (f, i), v in sorted((state.regs if state else {}).items()))
    memory = kmap((f"byte({string(s)}, {a})", str(v))
                  for s, values in sorted((state.memory if state else {}).items())
                  for a, v in sorted(values.items()))
    bindings = kmap((string(k), str(v)) for k, v in sorted((params or {}).items()))
    replies, keys = [], set()
    for response in responses:
        key = response["step"], response["pc"], response["id"]
        if key in keys: raise ExportError("duplicate runtime response")
        keys.add(key)
        if response["width"] < 0 or not 0 <= response["value"] < 1 << response["width"]:
            raise ExportError("runtime response outside its result width")
        replies.append((f"request({key[0]}, {key[1]}, {key[2]})",
                        f"response({string(response['name'])}, "
                        f"{klist(map(str, response['arguments']))}, {response['width']}, "
                        f"{response['value']}, {response.get('events', '.List')}, "
                        f"{response.get('before', environment or '.Map')}, "
                        f"{response.get('environment', environment or '.Map')})"))
    return (f"run({export.program}, {regs}, {memory}, {bindings}, {kmap(replies)}, "
            f"{environment or '.Map'}, {limit})")
