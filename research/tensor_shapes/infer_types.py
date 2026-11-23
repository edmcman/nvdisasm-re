import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sass.decode import cubin
from sass.semantics import bitvec, lifter
from type_system import ADDRESS, NUMERIC, InferenceError, Printer, Solver, Type, dimension, floating, integer, pointer, predicate, shape
import call_graph


FLOAT_UNARY = frozenset(f"{op}.f{width}" for op in ("neg", "abs", "sat") for width in (16, 32, 64))
STABLE_SPECIAL = frozenset((33, 34, 35, 37, 38, 39, 136))


def lower(e): return bitvec.lowered(e, preserve=FLOAT_UNARY)


@dataclass(eq=False)
class Value:
    index: int
    op: str
    width: int
    args: tuple
    origin: str


class Graph:
    def __init__(self):
        self.values, self.inputs, self.accesses, self.unsupported = [], {}, [], []
        self.memo = {}
        self.load_guards = {}
        self.control = []
        self.selections = {}
        self.blocks, self.phi_blocks, self.phi_predecessors = {}, {}, {}
        self.phi_locations = {}
        self.phi_components = {}
        self.source_blocks = {}
        self.return_checks, self.verified_returns, self.call_contexts = {}, [], {}
        self.numeric_operations = {}

    def node(self, op, width, *args, origin=""):
        key = op, width, args, origin if op == "load" else ""
        if op not in ("const", "phi") and key in self.memo: return self.memo[key]
        v = Value(len(self.values), op, width, args, origin)
        self.values.append(v)
        if op not in ("const", "phi"): self.memo[key] = v
        return v

    def input(self, name, width):
        return self.inputs.setdefault((name, width), self.node("input", width, name, origin=name))

    def extract(self, v, hi, lo, origin=""):
        if lo == 0 and hi + 1 == v.width: return v
        if v.op == "extract":
            source, _, offset = v.args
            return self.extract(source, hi + offset, lo + offset, origin)
        if v.op == "concat":
            offset = 0
            for part in reversed(v.args):
                if offset <= lo <= hi < offset + part.width: return self.extract(part, hi - offset, lo - offset, origin)
                offset += part.width
        return self.node("extract", hi - lo + 1, v, hi, lo, origin=origin)

    def read_phi(self, value, read):
        if value in self.phi_locations: return read(*self.phi_locations[value])
        result = 0
        for parent, hi, lo in self.phi_components[value]:
            width = hi - lo + 1
            result = (result << width) | ((self.read_phi(parent, read) >> lo) & ((1 << width) - 1))
        return result

    def concat(self, parts, origin=""):
        if len(parts) == 1: return parts[0]
        width = sum(p.width for p in parts)
        if all(p.op == "extract" for p in parts):
            source = parts[0].args[0]
            cursor = width
            exact = width == source.width
            for p in parts:
                exact &= p.args[0] is source and p.args[1] == cursor - 1 and p.args[2] == cursor - p.width
                cursor -= p.width
            if exact: return source
        if len(parts) == 2 and all(p.width == 32 for p in parts):
            h, l = parts
            if h.op == "add" and l.op == "extract" and l.args[1:] == (31, 0):
                total = l.args[0]
                for high, carry in (h.args, h.args[::-1]):
                    if carry.op != "zext" or carry.args[0].op != "ule": continue
                    threshold, summed = carry.args[0].args
                    if threshold.op != "const" or threshold.args[0] != 1 << 32 or summed is not total or total.op != "add": continue
                    for low, offset in (total.args, total.args[::-1]):
                        if low.op != "zext" or low.args[0].width != 32: continue
                        bounded = (offset.op == "zext" and offset.args[0].width <= 32) or (offset.op == "const" and 0 <= offset.args[0] < 1 << 32)
                        if not bounded: continue
                        base = self.concat((high, low.args[0]), origin)
                        return self.node("add", 64, base, offset, origin=origin)
        return self.node("concat", width, *parts, origin=origin)

    def under(self, v, guard):
        key = v, guard
        if key in self.selections: return self.selections[key]
        result = v
        match v.op:
            case "ite":
                condition, yes, no = v.args
                if condition is guard: result = self.under(yes, guard)
                elif (condition.op == "not" and condition.args[0] is guard) or (guard.op == "not" and guard.args[0] is condition):
                    result = self.under(no, guard)
            case "extract": result = self.extract(self.under(v.args[0], guard), *v.args[1:], v.origin)
            case "concat": result = self.concat(tuple(self.under(a, guard) for a in v.args), v.origin)
        self.selections[key] = result
        return result


def phi_aliases(values):
    phis = [v for v in values if v.op == "phi"]
    leaves = {v: {a for a in v.args if a.op != "phi"} for v in phis}
    changed = True
    while changed:
        changed = False
        for v in phis:
            incoming = set().union(*(leaves[a] for a in v.args if a.op == "phi")) if any(a.op == "phi" for a in v.args) else set()
            before = len(leaves[v])
            leaves[v].update(incoming)
            changed |= before != len(leaves[v])
    def depends(source, target):
        pending, seen = [source], set()
        while pending:
            v = pending.pop()
            if v is target: return True
            if v in seen: continue
            seen.add(v)
            pending.extend(a for a in v.args if isinstance(a, Value))
        return False
    return {v: next(iter(sources)) for v, sources in leaves.items() if len(sources) == 1 and not depends(next(iter(sources)), v)}


def pack_joins(graph):
    original = list(graph.values)
    replacements = phi_aliases(original)
    packed = []
    def projection(value):
        hi, lo = value.width - 1, 0
        while True:
            if value in replacements:
                value = replacements[value]
                continue
            if value.op == "extract":
                parent, _, offset = value.args
                value, hi, lo = parent, hi + offset, lo + offset
                continue
            return (value, hi, lo) if value.op == "phi" else None
    for value in original:
        if value.op != "concat" or len(value.args) != 2 or any(a.width != 32 for a in value.args): continue
        components = tuple(projection(a) for a in value.args)
        if None in components: continue
        phis = [c[0] for c in components]
        if len({graph.phi_blocks[p] for p in phis}) != 1 or len({graph.phi_predecessors[p] for p in phis}) != 1: continue
        whole = graph.node("phi", 64, origin=value.origin)
        graph.phi_blocks[whole] = graph.phi_blocks[phis[0]]
        graph.phi_predecessors[whole] = graph.phi_predecessors[phis[0]]
        graph.phi_components[whole] = components
        replacements[value] = whole
        packed.append(whole)
    if not packed: return graph
    memo = {}
    def rewrite(value):
        if not isinstance(value, Value): return value
        if value in memo: return memo[value]
        if value in replacements:
            result = rewrite(replacements[value])
        elif value.op in ("phi", "load", "const", "input"):
            result = value
        elif value.op == "extract":
            parent = rewrite(value.args[0])
            result = graph.extract(parent, *value.args[1:], value.origin)
            if result in replacements: result = rewrite(replacements[result])
        else:
            args = tuple(rewrite(a) for a in value.args)
            match value.op:
                case "concat": result = graph.concat(args, value.origin)
                case _ if all(a is b for a, b in zip(args, value.args)): result = value
                case _: result = graph.node(value.op, value.width, *args, origin=value.origin)
            if result in replacements: result = rewrite(replacements[result])
        memo[value] = result
        return result
    for whole in packed:
        sources = []
        for position in range(len(graph.phi_predecessors[whole])):
            parts = [rewrite(graph.extract(parent.args[position], hi, lo, whole.origin)) for parent, hi, lo in graph.phi_components[whole]]
            sources.append(rewrite(graph.concat(tuple(parts), whole.origin)))
        whole.args = tuple(sources)
    for value in list(graph.values):
        if value not in replacements: value.args = tuple(rewrite(a) for a in value.args)
    graph.accesses = [(kind, space, rewrite(address), rewrite(value), rewrite(guard), origin) for kind, space, address, value, guard, origin in graph.accesses]
    graph.load_guards = {v: rewrite(g) for v, g in graph.load_guards.items()}
    graph.numeric_operations = {rewrite(v): (op, tuple(rewrite(a) for a in args))
                                for v, (op, args) in graph.numeric_operations.items()}
    for control in graph.control:
        for name in ("guard", "condition"):
            if control[name] is not None: control[name] = rewrite(graph.values[control[name]]).index
    graph.memo.clear()
    return graph


def expressions(e):
    if isinstance(e, bitvec.Expr):
        yield e
        for a in e.args: yield from expressions(a)


def build(sems, return_checks=None):
    graph = Graph()
    graph.arch = sems[0].arch
    by_address = {s.addr: s for s in sems}
    leaders = {sems[0].addr}
    for s in sems:
        if s.flow:
            leaders.add(s.addr + 16)
            if s.flow.kind == "goto":
                if not bitvec.is_const(s.flow.target): raise InferenceError(f"indirect control flow at {s.addr:#x}")
                leaders.add(bitvec.value(s.flow.target))
    starts = sorted(leaders & by_address.keys())
    blocks = {a: [s for s in sems if a <= s.addr < b] for a, b in zip(starts, starts[1:] + [sems[-1].addr + 16])}
    successors = {}
    for a, block in blocks.items():
        last = block[-1]
        targets = []
        if last.flow and last.flow.kind == "goto": targets.append(bitvec.value(last.flow.target))
        if not last.flow or last.guard is not None or last.flow.cond is not None: targets.append(last.addr + 16)
        successors[a] = [t for t in targets if t in blocks]
    reachable, todo = set(), [starts[0]]
    while todo:
        a = todo.pop()
        if a in reachable: continue
        reachable.add(a)
        todo.extend(successors[a])
    predecessors = defaultdict(list)
    for a in sorted(reachable):
        for b in successors[a]: predecessors[b].append(a)
    graph.blocks = {a: {"successors": successors[a], "predecessors": predecessors[a]} for a in sorted(reachable)}

    spans = set()
    for a in reachable:
        for s in blocks[a]:
            roots = [v for _, v in s.writes] + [r for r, _ in s.writes]
            roots += [t.address for t in s.stores] + [t.value for t in s.stores]
            for root in roots:
                for e in expressions(root):
                    if e.op == "reg" and e.args[0] in ("R", "UR") and e.args[2] == 2:
                        file, index, _ = e.args
                        spans.add((file, index))
    owners = defaultdict(set)
    for file, i in spans:
        for j in (i, i + 1): owners[file, j].add((file, i))
    pairs = {p for p in spans if all(len(owners[p[0], j]) == 1 for j in (p[1], p[1] + 1))}
    entries, outgoing, pending = {}, {}, []

    def entry(a, file, index, count=1):
        if count == 1 and (file, index) in owners:
            unique = next(iter(owners[file, index]))
            if unique in pairs:
                parent = entry(a, *unique, 2)
                lo = (index - unique[1]) * 32
                return graph.extract(parent, lo + 31, lo, f"block {a:#x}")
        key = a, file, index, count
        if key not in entries:
            width = count * (1 if file in ("P", "UP") else 32)
            v = graph.node("phi", width, origin=f"block {a:#x} {file}{index}:{count}")
            entries[key] = v
            graph.phi_blocks[v] = a
            graph.phi_locations[v] = file, index, count
            graph.phi_predecessors[v] = tuple(predecessors[a])
            pending.append(key)
        return entries[key]

    def read(state, a, file, index, count):
        width = 1 if file in ("P", "UP") else 32
        parts = [state[file, i] if (file, i) in state else entry(a, file, i) for i in reversed(range(index, index + count))]
        return graph.concat(parts, f"block {a:#x} {file}{index}:{count}")

    for a in sorted(reachable):
        state = {}
        for s in blocks[a]:
            origin = f"{s.addr:#x}: {s.text}"
            graph.source_blocks[origin] = a
            if s.flow and s.flow.kind not in ("goto", "return"): raise InferenceError(f"unsupported {s.flow.kind} at {s.addr:#x}")
            if s.status == "opaque": raise InferenceError(f"opaque semantics at {origin}: {s.reason}")
            graph.unsupported.extend({"effect": e.name, "origin": origin} for e in s.effects)
            cache = {}
            def translate(e):
                if not isinstance(e, bitvec.Expr): return e
                if e.op != "const" and e in cache: return cache[e]
                match e.op:
                    case "reg":
                        file, index, count = e.args
                        match file:
                            case "SR" if index in STABLE_SPECIAL: v = graph.input(f"SR{index}", e.w)
                            case "SR": v = graph.node("special", e.w, index, origin, origin=origin)
                            case _: v = read(state, a, file, index, count)
                        if guard is not None: v = graph.under(v, guard)
                    case "load" if e.args[0] == "cbank" and bitvec.is_const(e.args[1]):
                        v = graph.input(f"cb{bitvec.value(e.args[1]):x}", e.w)
                    case "param": v = graph.input(e.args[0], e.w)
                    case "extract": v = graph.extract(translate(e.args[0]), *e.args[1:], origin)
                    case "concat": v = graph.concat(tuple(translate(x) for x in e.args), origin)
                    case _: v = graph.node(e.op, e.w, *(translate(x) for x in e.args), origin=origin)
                if e.op == "load" and e.args[0] != "cbank": graph.load_guards[v] = guard
                if e.op != "const": cache[e] = v
                return v
            def translate_root(expression):
                conversions = []
                def observe(expansion, node):
                    if node.op.startswith('cvt.'): conversions.append((expansion, node))
                expanded = bitvec.lowered(expression, origin=observe, preserve=FLOAT_UNARY)
                result = translate(expanded)
                for expansion, node in conversions:
                    graph.numeric_operations[translate(expansion)] = node.op, tuple(translate(lower(x)) for x in node.args)
                return result
            guard = None
            guard = translate_root(s.guard) if s.guard is not None else None
            if return_checks and s.addr in return_checks:
                expression, expected = return_checks[s.addr]
                graph.return_checks[origin] = translate_root(expression), expected
            writes = []
            for location, expression in s.writes:
                file, index, count = location.args
                v = translate_root(expression)
                width = 1 if file in ("P", "UP") else 32
                for k in range(count):
                    part = graph.extract(v, (k + 1) * width - 1, k * width, origin)
                    if guard is not None:
                        old = read(state, a, file, index + k, 1)
                        part = graph.node("ite", width, guard, part, old, origin=origin)
                    writes.append(((file, index + k), part))
            for t in s.stores:
                address, value = translate_root(t.address), translate_root(t.value)
                condition = translate_root(t.cond) if t.cond is not None else None
                if guard is not None: condition = guard if condition is None else graph.node("and", 1, guard, condition, origin=origin)
                graph.accesses.append(("write", t.space, address, value, condition, origin))
            if guard is not None: graph.node("guard", 1, guard, origin=origin)
            if s.flow:
                condition = translate_root(s.flow.cond) if s.flow.cond is not None else None
                if condition is not None: graph.node("guard", 1, condition, origin=origin)
                graph.control.append({"block": a, "address": s.addr, "kind": s.flow.kind, "target": bitvec.value(s.flow.target) if s.flow.kind == "goto" else None, "fallthrough": s.addr + 16, "successors": successors[a], "guard": None if guard is None else guard.index, "condition": None if condition is None else condition.index, "origin": origin})
            state.update(writes)
        outgoing[a] = state
    cursor = 0
    while cursor < len(pending):
        key = pending[cursor]
        cursor += 1
        a, file, index, count = key
        sources = [read(outgoing[p], p, file, index, count) for p in predecessors[a]]
        if a == starts[0] or not sources: sources.append(graph.input(f"entry.{file}{index}:{count}", entries[key].width))
        entries[key].args = tuple(sources)
    graph = pack_joins(graph)
    if graph.return_checks: call_graph.verify_returns(graph)
    return graph


class Inference:
    def __init__(self, graph):
        self.graph, self.solver = graph, Solver()
        self.types = {v: self.solver.fresh(width=v.width) for v in graph.values}
        self.roles = {v: self.solver.fresh("role") for v in graph.values}
        self.additions, self.joins, self.residuals, self.accesses = [], [], [], []
        self.integer_views = []
        self.demanded = set()
        self.projections = {}

    def projected_type(self, value, hi, lo):
        if hi == value.width - 1 and lo == 0: return self.types[value]
        key = value, hi, lo
        if key in self.projections: return self.projections[key]
        term = self.solver.fresh(width=hi - lo + 1)
        self.projections[key] = term
        match value.op:
            case "extract":
                source, _, offset = value.args
                self.solver.unify(term, self.projected_type(source, hi + offset, lo + offset), value.origin)
            case "concat":
                offset = 0
                for source in reversed(value.args):
                    if offset <= lo <= hi < offset + source.width:
                        self.solver.unify(term, self.projected_type(source, hi - offset, lo - offset), value.origin)
                    offset += source.width
            case "phi":
                for source in value.args: self.solver.unify(term, self.projected_type(source, hi, lo), value.origin)
        return term

    def numeric(self, values, origin):
        for v in values: self.solver.constrain(self.roles[v], NUMERIC, origin)

    def integers(self, values, origin):
        self.numeric(values, origin)
        self.integer_views.extend((v, origin) for v in values)

    def memory(self, kind, space, address, value, guard, origin):
        if kind == "write": self.demanded.add(value)
        if space not in ("ram", "generic"):
            self.residuals.append({"kind": "memory_cell", "space": space, "address": address.index, "value": value.index, "origin": origin})
            return
        layout = self.solver.fresh("shape")
        self.solver.constrain(self.roles[address], ADDRESS, origin)
        self.solver.constrain(self.types[address], pointer("global", address.width, self.types[value], layout), origin)
        self.accesses.append({"constraint": "access_within", "kind": kind, "address": address.index, "value": value.index, "bytes": (value.width + 7) // 8, "guard": None if guard is None else guard.index, "shape": layout, "origin": origin})

    def generate(self):
        for v in self.graph.values:
            args = [a for a in v.args if isinstance(a, Value)]
            origin = v.origin or f"value {v.index} ({v.op})"
            match v.op:
                case "input":
                    if v.args[0].startswith("SR"):
                        self.integers([v], origin)
                        self.solver.constrain(self.types[v], integer(v.width), origin)
                case "const": self.numeric([v], origin)
                case "phi": self.joins.append(v)
                case "ite":
                    self.solver.constrain(self.types[args[0]], predicate(), origin)
                    self.joins.append(v)
                case "guard": self.solver.constrain(self.types[args[0]], predicate(), origin)
                case op if op in FLOAT_UNARY or op in ("fadd", "fmul", "ffma", "fneg", "fabs", "nanresult"):
                    operands = args[:1] if v.op == "nanresult" else args
                    for x in [v, *operands]: self.solver.constrain(self.types[x], floating(v.width), origin)
                    self.numeric([v, *operands], origin)
                case "flt" | "fle" | "feq" | "fnan":
                    for x in args: self.solver.constrain(self.types[x], floating(x.width), origin)
                    self.solver.constrain(self.types[v], predicate(), origin)
                    self.numeric([v, *args], origin)
                case "eq" | "ne" | "ult" | "ule" | "slt" | "sle":
                    self.solver.constrain(self.types[v], predicate(), origin)
                    self.integers([v, *args], origin)
                case "add" | "sub": self.additions.append(v)
                case "mul" | "shl" | "shr" | "sar" | "zext" | "sext": self.integers([v, *args], origin)
                case "and" | "or" | "xor" | "not" if v.width == 1:
                    for x in [v, *args]: self.solver.constrain(self.types[x], predicate(), origin)
                    self.numeric([v, *args], origin)
                case "and" | "or" | "xor" | "not": self.integers([v, *args], origin)
                case "load": self.memory("read", v.args[0], v.args[1], v, self.graph.load_guards.get(v), origin)
                case _:
                    self.residuals.append({"kind": "representation", "operation": v.op, "value": v.index, "origin": origin})
        for access in self.graph.accesses: self.memory(*access)
        return self

    def solve(self):
        self.solver.solve()
        pending, joins = self.additions, self.joins
        extractions = [v for v in self.graph.values if v.op == "extract"]
        while pending or joins or extractions:
            remaining, before = [], (len(self.solver.bindings), len(self.demanded))
            waiting = []
            for value in extractions:
                if value not in self.demanded and not isinstance(self.solver.root(self.types[value]), Type):
                    waiting.append(value)
                    continue
                self.solver.unify(self.types[value], self.projected_type(*value.args), value.origin)
            extractions = waiting
            undemanded = []
            for v in joins:
                demanded = v in self.demanded or isinstance(self.solver.root(self.types[v]), Type) or isinstance(self.solver.root(self.roles[v]), Type)
                if not demanded:
                    undemanded.append(v)
                    continue
                arms = v.args[1:] if v.op == "ite" else v.args
                self.demanded.update(arms)
                for a in arms:
                    self.solver.unify(self.types[v], self.types[a], v.origin)
                    self.solver.unify(self.roles[v], self.roles[a], v.origin)
            joins = undemanded
            for v in pending:
                a, b = v.args
                origin = v.origin
                orientations = [(a, b), (b, a)] if v.op == "add" else [(a, b)]
                selected = next(((base, offset) for base, offset in orientations if self.solver.root(self.roles[offset]) == NUMERIC), None)
                if selected is None:
                    remaining.append(v)
                    continue
                base, offset = selected
                self.solver.unify(self.roles[v], self.roles[base], origin)
                match self.solver.root(self.roles[base]):
                    case role if role == ADDRESS:
                        self.solver.unify(self.types[v], self.types[base], origin)
                        self.integer_views.append((offset, origin))
                    case role if role == NUMERIC:
                        self.integer_views.extend((x, origin) for x in (v, a, b))
                    case _: remaining.append(v)
            if (len(self.solver.bindings), len(self.demanded)) == before:
                pending = remaining
                break
            pending = remaining
        self.residuals.extend({"kind": "arithmetic_overload", "value": v.index, "operation": v.op, "origin": v.origin} for v in pending)
        self.residuals.extend({"kind": "unresolved_join", "value": v.index, "origin": v.origin} for v in joins)
        for value, origin in self.integer_views:
            if value.op == "extract": self.solver.unify(self.types[value], self.projected_type(*value.args), value.origin)
            term = self.solver.root(self.types[value])
            if not isinstance(term, Type): self.solver.unify(self.types[value], integer(value.width), origin)
        return self

    def annotate(self, name, width, term):
        v = self.graph.inputs[name, width]
        self.solver.constrain(self.types[v], term, f"annotation {name}:{width}")
        if term.tag == "pointer": self.solver.constrain(self.roles[v], ADDRESS, f"annotation {name}:{width}")
        return self

    def report(self):
        printer = Printer(self.solver)
        return {
            "bounds_checked": False,
            "inputs": {f"{name}:{width}": printer(self.types[v]) for (name, width), v in sorted(self.graph.inputs.items())},
            "accesses": [{**a, "shape": printer(a["shape"]), "element": printer(self.types[self.graph.values[a["value"]]])} for a in self.accesses],
            "residuals": self.residuals,
            "unsupported": self.graph.unsupported,
            "control": self.graph.control,
            "verified_returns": self.graph.verified_returns,
            "call_contexts": self.graph.call_contexts,
            "integer_views": [{"value": value.index, "as": printer(integer(value.width)), "storage": printer(self.types[value]), "origin": origin}
                              for value, origin in dict.fromkeys(self.integer_views)],
            "values": [{"id": v.index, "operation": v.op, "width": v.width, "arguments": [a.index if isinstance(a, Value) else a for a in v.args], "type": printer(self.types[v]), "origin": v.origin} for v in self.graph.values],
        }


def kernel(path, name=None):
    data = Path(path).read_bytes()
    name = name or next(iter(cubin.text_sections(data))).removeprefix(".text.")
    image = cubin.kernel_image(data, name)
    sems = [lifter.lift(image["arch"], w, i * 16, fp="readable") for i, w in enumerate(cubin.words(image["code"]))]
    sems, checks, contexts = call_graph.expand(sems)
    graph = build(sems, checks)
    graph.call_contexts = contexts
    return graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cubin")
    parser.add_argument("--kernel")
    parser.add_argument("--annotations", type=Path)
    args = parser.parse_args()
    inference = Inference(kernel(args.cubin, args.kernel)).generate()
    if args.annotations:
        for name, annotation in json.loads(args.annotations.read_text()).items():
            input_name, width = name.split(":")
            element = floating(annotation["float"])
            layout = shape(*(dimension(d) for d in annotation["shape"]))
            inference.annotate(input_name, int(width), pointer("global", int(width), element, layout))
    print(json.dumps(inference.solve().report(), indent=2))


if __name__ == "__main__": main()
