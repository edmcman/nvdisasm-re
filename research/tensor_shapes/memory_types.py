import argparse
import json
from itertools import product
from pathlib import Path

from access_relations import Atom, Normal, Polynomial, PrecisionLimit
from infer_types import Inference, kernel
from type_system import Printer


def inputs(term):
    match term:
        case Atom("input", width, (name,)): return {(name, width)}
        case Atom(): return set().union(*(inputs(a) for a in term.arguments))
        case Polynomial(): return set().union(*(inputs(a) for factors, _ in term.terms for a in factors))
        case _: return set()


def evaluate(term, environment):
    mask = (1 << term.width) - 1
    match term:
        case Polynomial(_, terms):
            result = 0
            for factors, coefficient in terms:
                value = coefficient
                for factor in factors: value *= evaluate(factor, environment)
                result += value
            return result & mask
        case Atom("input", width, (name,)): return environment[name, width] & mask
        case Atom("extract", _, (source, _, lo)): return (evaluate(source, environment) >> lo) & mask
        case Atom("zext", _, (source,)): return evaluate(source, environment)
        case Atom("sext", _, (source,)):
            value = evaluate(source, environment)
            return (value - (1 << source.width) if value >= 1 << (source.width - 1) else value) & mask
        case Atom("and", _, (a, b)): return evaluate(a, environment) & evaluate(b, environment)
        case Atom("shr", _, (a, b)): return evaluate(a, environment) >> evaluate(b, environment)
        case Atom("concat", _, parts):
            result = 0
            for part in parts: result = (result << part.width) | evaluate(part, environment)
            return result & mask
        case _: raise PrecisionLimit(f"unresolved shared address operator {term.operation}")


def footprint(term, ranges, limit=65536):
    variables = sorted(inputs(term))
    domains, combinations = [], 1
    for name, width in variables:
        key = f"{name}:{width}"
        if key not in ranges: raise PrecisionLimit(f"missing address input range {key}")
        lower, upper = ranges[key]
        if not 0 <= lower <= upper < 1 << width: raise ValueError(f"invalid address input range {key}")
        combinations *= upper - lower + 1
        if combinations > limit: raise PrecisionLimit(f"shared footprint exceeds {limit} address valuations")
        domains.append(range(lower, upper + 1))
    return {evaluate(term, dict(zip(variables, values))) for values in product(*domains)}


class MemoryTypes:
    def __init__(self, inference, ranges=None):
        self.inference, self.ranges = inference, ranges or {}
        self.normal = Normal(inference.graph)
        self.cells, self.evidence, self.residuals = {}, [], []

    def lane_type(self, value, offset):
        if value.width == 32: return self.inference.types[value]
        if value.op == "concat" and all(a.width == 32 for a in value.args):
            return self.inference.types[value.args[-1 - offset // 4]]
        for child in self.inference.graph.values:
            if child.width == 32 and child.op == "extract" and child.args == (value, offset * 8 + 31, offset * 8):
                return self.inference.types[child]
        return self.inference.solver.fresh(width=32)

    def generate(self):
        memory = []
        for value in self.inference.graph.values:
            if value.op == "load" and value.args[0] == "shared":
                memory.append(("read", "shared", value.args[1], value, self.inference.graph.load_guards.get(value), value.origin))
        memory.extend(a for a in self.inference.graph.accesses if a[1] == "shared")
        for kind, space, address, value, guard, origin in memory:
            try:
                if value.width % 32: raise PrecisionLimit("shared cell typing requires whole 32-bit words")
                addresses = footprint(self.normal(address), self.ranges)
                if any(a % 4 for a in addresses): raise PrecisionLimit("shared access is not word-aligned")
                for offset in range(0, value.width // 8, 4):
                    lane = self.lane_type(value, offset)
                    for base in addresses:
                        location = space, base + offset
                        if location not in self.cells: self.cells[location] = self.inference.solver.fresh(width=32)
                        cell = self.cells[location]
                        self.inference.solver.constrain(lane, cell, f"shared cell {base + offset:#x} at {origin}")
                self.evidence.append({"kind": kind, "origin": origin, "addresses": sorted(addresses), "width": value.width,
                                      "guard_overapproximated": guard is not None})
            except PrecisionLimit as e: self.residuals.append({"origin": origin, "reason": str(e)})
        return self

    def report(self):
        printer = Printer(self.inference.solver)
        return {"input_ranges": self.ranges, "cell_types": {f"{space}:{address:#x}": printer(term) for (space, address), term in sorted(self.cells.items())},
                "accesses": self.evidence, "residuals": self.residuals,
                "semantics": "monomorphic_word_cells", "initialization_proven": False, "synchronization_proven": False}


def infer(graph, ranges=None):
    inference = Inference(graph).generate()
    memory = MemoryTypes(inference, ranges).generate()
    inference.solve()
    return inference, memory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cubin")
    parser.add_argument("--kernel")
    parser.add_argument("--ranges", type=Path)
    args = parser.parse_args()
    ranges = json.loads(args.ranges.read_text()) if args.ranges else None
    inference, memory = infer(kernel(args.cubin, args.kernel), ranges)
    print(json.dumps({"types": inference.report(), "memory": memory.report()}, indent=2))


if __name__ == "__main__": main()
