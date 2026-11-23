from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from math import floor, lcm
import z3

from access_relations import Atom, Normal, Polynomial, PrecisionLimit, dominators, execution_dependent, invariant, render, signed
from constraint_domain import Equalities, add, multiply, order
from constraint_state import ConstraintState
from word_ranges import Interval, Ranges
from scalar_bounds import Infeasible, ScalarBounds
from types import SimpleNamespace
from infer_types import Value
from numeric_relations import NumericRelations


@dataclass(frozen=True)
class Comparison:
    left: tuple
    relation: str
    right: tuple
    width: int
    signed: bool
    origin: str


def freeze(row): return tuple(sorted(row.items(), key=lambda item: order(item[0])))
def thaw(row): return dict(row)


def direction(row):
    row = {m: c for m, c in row.items() if m and c}
    if not row: return ()
    scale = abs(row[min(row, key=order)])
    return freeze({m: Fraction(c) / scale for m, c in row.items()})


def show(row):
    parts = []
    for feature, coefficient in sorted(row.items(), key=lambda item: order(item[0])):
        factors = " * ".join(show_atom(a) for a in feature)
        parts.append(str(coefficient) if not feature else factors if coefficient == 1 else f"{coefficient} * {factors}")
    return " + ".join(parts) or "0"


def show_atom(atom):
    match atom:
        case Atom("variable", _, (index, hi, lo)): return f"v{index}[{hi}:{lo}]"
        case Atom("word", _, (source,)): return render(source)
        case Atom("nondeterministic", _, (source,)): return f"unknown({source})"
        case Atom("havoc", _, (source, target, _)): return f"havoc({source:#x}->{target:#x})"
        case _: return render(atom)


class CommonConstraints:
    def __init__(self, inference, ranges=None, degree=4, feature_limit=256):
        self.inference, self.graph = inference, inference.graph
        self.normal = Normal(self.graph)
        self.degree, self.feature_limit = degree, feature_limit
        self.features = {b: {()} for b in self.graph.blocks}
        self.dominators = dominators(self.graph.blocks)
        self.loop_counts = {target: Atom('iteration', 64, (target,))
                            for source, record in self.graph.blocks.items() for target in record['successors']
                            if target in self.dominators[source]}
        for block, features in self.features.items():
            features.update((count,) for header, count in self.loop_counts.items() if header in self.dominators[block])
        self.assignments, self.states, self.edges = {}, {}, {}
        self.casts, self.residuals, self.variables = {}, [], {}
        self.masks, self.quotients = {}, {}
        self.selects, self.unsigned, self.pure_cache = {}, {}, {}
        self.select_cache = {}
        self.numeric_relation_cache = {}
        self.approximate_cache = {}
        self.counter_reductions = {}
        self.numeric_relation_blocks = {self.graph.source_blocks.get(value.origin)
                                       for value, (op, _) in self.graph.numeric_operations.items()
                                       if op.startswith('cvt.rzi')}
        self.memo, self.iterations = {}, 0
        self.range_context = Ranges(ranges)
        self.environment = self.launch_constraints()
        self.guards = self.edge_guards()
        self.local_guards = {}
        self.accesses = []
        for access in inference.accesses:
            row = self.value(self.graph.values[access["address"]])
            block = self.graph.source_blocks[access["origin"]]
            self.observe(block, row)
            self.accesses.append({**access, "expression": row, "block": block})
            if access["guard"] is not None:
                self.local_guards[access["origin"]] = self.predicates(self.graph.values[access["guard"]], True, access["origin"])
        self.conditions = list(self.guards.values()) + list(self.local_guards.values())
        for (source, _), alternatives in self.guards.items():
            for conjunction in alternatives:
                for comparison in conjunction:
                    self.observe(source, thaw(comparison.left))
                    self.observe(source, thaw(comparison.right))
        for access in self.accesses:
            for conjunction in self.local_guards.get(access["origin"], []):
                for comparison in conjunction:
                    self.observe(access["block"], thaw(comparison.left))
                    self.observe(access["block"], thaw(comparison.right))
        self.uniform_bounds = {frozen for alternatives in self.conditions for conjunction in alternatives for comparison in conjunction
                               for frozen in (comparison.left, comparison.right)
                if not any(a.operation in ("variable", "nondeterministic") or a.operation == "word" and not invariant(a.arguments[0]) for m, _ in frozen for a in m)}
        self.uniform_bounds.update(c.right for c in self.environment)
        for block in self.features:
            for bound in self.uniform_bounds: self.observe(block, thaw(bound))
            for constraint in self.environment:
                self.observe(block, thaw(constraint.left))
        self.close_features()
        for _, condition, _, _ in list(self.selects.values()):
            self.conditions.extend([self.predicates(condition, truth, condition.origin) for truth in (False, True)])
        self.make_templates()
        try:
            self.numeric = ScalarBounds(SimpleNamespace(ranges=self.range_context), self).solve()
            if not self.numeric.verify(): self.numeric = None
        except (RuntimeError, Infeasible): self.numeric = None

        self.numeric_facts = {}
        if self.numeric is not None:
            for block, features in self.features.items():
                if block not in self.numeric.states: continue
                inequalities = []
                for feature in features:
                    if len(feature) != 1 or feature[0] not in self.variables: continue
                    value, hi, lo = self.variables[feature[0]]
                    if hi - lo >= 32 or self.graph.phi_blocks[value] not in self.dominators[block]: continue
                    term = self.normal.slice(value, hi, lo)
                    interval = self.numeric.word(term, self.numeric.states[block]).interval
                    if term.width > 1: interval = interval.signed(term.width)
                    inequalities.extend([({feature: 1}, interval.upper), ({feature: -1}, -interval.lower)])
                self.numeric_facts[block] = inequalities

    def numeric_reduce(self, state, block):
        for _ in range(16):
            reduced = self.reduce_words(state, block)
            if reduced is not None: reduced = self.refine_iterations(reduced, block)
            if reduced is None or reduced == state: return reduced
            state = reduced
        raise PrecisionLimit('word-domain reduction exceeded 16 iterations')

    def refine_iterations(self, state, block):
        if block not in self.loop_counts: return state
        key = block, state
        if key in self.counter_reductions: return self.counter_reductions[key]
        inequalities = []
        for frozen in self.templates[block]:
            if len(frozen) != 2 or not all(len(m) == 1 and abs(c) == 1 for m, c in frozen): continue
            if not any(m[0].operation == 'iteration' for m, _ in frozen): continue
            if not any(m[0] in self.quotients.values() for m, _ in frozen): continue
            row = thaw(frozen)
            bound = state.query(row)
            if bound is not None and bound <= -1: continue
            positive = [m for m in self.parameter_factors if (u := state.query({m: -1})) is not None and u < 0]
            cancelled = any((u := state.query(multiply(row, {factor: 1}, self.degree))) is not None and u < 0
                            for factor in positive if len(factor) < self.degree)
            if bound is not None and bound < 0 or cancelled:
                inequalities.append((row, -1))
        result = state.assume(inequalities=self.lift_inequalities(state, inequalities, self.features[block])) or state
        self.counter_reductions[key] = result
        return result

    def reduce_words(self, state, block):
        inequalities = list(self.numeric_facts.get(block, ()))
        inequalities.extend(({(count,): -1}, 0) for count in self.loop_counts.values() if (count,) in self.features[block])
        equalities = []
        for atom, width in self.unsigned.items():
            if (atom,) in self.features[block]: inequalities.extend([({(atom,): -1}, 0), ({(atom,): 1}, (1 << width) - 1)])
        for atom, (source, mask) in self.masks.items():
            if (atom,) not in self.features[block]: continue
            inequalities.extend([({(atom,): -1}, 0), ({(atom,): 1}, mask)])
            lower = state.query({m: -c for m, c in source.items()})
            if lower is not None and lower <= 0: inequalities.append((add({(atom,): 1}, source, -1), 0))
            upper = state.query(source)
            if lower is not None and upper is not None and lower <= 0 and floor(-lower / (mask + 1)) == floor(upper / (mask + 1)):
                equalities.append(add(add({(atom,): 1}, source, -1), {(): floor(upper / (mask + 1)) * (mask + 1)}))
            quotient = self.quotients[atom]
            if lower is not None and lower <= 0:
                equalities.append(add(add(source, {(atom,): 1}, -1), {(quotient,): mask + 1}, -1))
                inequalities.append(({(quotient,): -1}, 0))
        state = state.assume(self.lift_equalities(equalities, self.features[block]), inequalities)
        if state is None: return None
        rounded = []
        for quotient in self.quotients.values():
            if (quotient,) not in self.features[block]: continue
            upper, lower = state.query({(quotient,): 1}), state.query({(quotient,): -1})
            if upper is not None: rounded.append(({(quotient,): 1}, floor(upper)))
            if lower is not None: rounded.append(({(quotient,): -1}, floor(lower)))
        return state.assume(inequalities=self.lift_inequalities(state, inequalities + rounded, self.features[block]))

    def projection(self, atom):
        match atom:
            case Atom("phi", width, (index,)): return self.graph.values[index], width - 1, 0
            case Atom("extract", _, (Polynomial(_, (((Atom("phi", _, (index,)),), 1),)), hi, lo)):
                return self.graph.values[index], hi, lo
            case _: return None


    def launch_constraints(self):
        if getattr(self.graph, "arch", None) != "SM90": return []
        constraints = []
        for register, offset in ((33, 0), (34, 4), (35, 8), (37, 12), (38, 16), (39, 20)):
            if (f"SR{register}", 32) not in self.graph.inputs: continue
            index = self.value(self.graph.inputs[f"SR{register}", 32])
            size = self.expression(Polynomial.atom(Atom("input", 32, (f"cb{offset:x}",))))
            origin = "SM90 launch ABI"
            constraints.extend([Comparison(freeze(index), "<", freeze(size), 32, False, origin),
                                Comparison(freeze(index), ">=", (), 32, False, origin),
                                Comparison(freeze(size), ">", (), 32, False, origin)])
        return constraints

    def variable(self, value, hi, lo):
        atom = Atom("variable", 64, (value.index, hi, lo))
        self.variables[atom] = value, hi, lo
        return {(atom,): 1}

    def value(self, value): return self.expression(self.normal(value))

    def expression(self, polynomial):
        if polynomial in self.memo: return self.memo[polynomial]
        result = {}
        for factors, coefficient in polynomial.terms:
            term = {(): coefficient if polynomial.width == 1 else signed(coefficient, polynomial.width)}
            for atom in factors: term = multiply(term, self.atom(atom), self.degree)
            result = add(result, term)
        self.memo[polynomial] = result
        return result

    def atom(self, atom):
        match atom:
            case Atom("opaque", 1, (index,)) if self.graph.values[index].op == "not":
                return add({(): 1}, self.value(self.graph.values[index].args[0]), -1)
            case Atom("opaque", _, (index,)) if (source := self.pure_value(self.graph.values[index])) is not None:
                return {(Atom("word", 64, (Polynomial.atom(source),)),): 1}
            case Atom("and", width, (left, right)) if left.constant_value() is not None or right.constant_value() is not None:
                source, mask = (right, left.constant_value()) if left.constant_value() is not None else (left, right.constant_value())
                if mask & (mask + 1): return {(Atom("nondeterministic", 64, (repr(atom),)),): 1}
                word = Atom("word", 64, (self.pure_term(Polynomial.atom(atom)),))
                self.masks[word] = self.expression(source), mask
                shifted = Atom("shr", width, (self.pure_term(source), Polynomial.constant(width, (mask + 1).bit_length() - 1)))
                self.quotients[word] = Atom("word", 64, (Polynomial.atom(shifted),))
                return {(word,): 1}
            case Atom("phi", width, (index,)): return self.variable(self.graph.values[index], width - 1, 0)
            case Atom("extract", _, (Polynomial(_, (((Atom("phi", _, (index,)),), 1),)), hi, lo)):
                return self.variable(self.graph.values[index], hi, lo)
            case Atom("extract", width, (Polynomial(_, (((Atom("input", _, (name,)),), 1),)), hi, lo)) if name.startswith("cb") and lo % 8 == 0:
                source = Polynomial.atom(Atom("input", width, (f"cb{int(name[2:], 16) + lo // 8:x}",)))
                return {(Atom("word", 64, (source,)),): 1}
            case Atom("sext" | "zext", width, (source,)):
                self.casts[atom] = {"source": render(source), "source_width": source.width, "width": width, "interpretation": atom.operation}
                result = self.expression(source)
                return {m: -c for m, c in result.items()} if atom.operation == "sext" and source.width == 1 else result
            case Atom("input", 64): return {(atom,): 1}
            case _ if invariant(source := self.pure_term(Polynomial.atom(atom))):
                word = Atom("word", 64, (source,))
                if atom.operation == "extract": self.unsigned[word] = atom.width
                return {(word,): 1}
            case _: return {(Atom("nondeterministic", 64, (repr(atom),)),): 1}

    def pure_inputs(self, value):
        value = self.normal.aliases.get(value, value)
        if value in self.pure_cache: return self.pure_cache[value]
        match value.op:
            case "input": result = frozenset([Atom("input", value.width, value.args)])
            case "const": result = frozenset()
            case "primitive" if not value.args[0].startswith("mufu_"): result = None
            case "phi" | "load" | "special": result = None
            case "add" | "sub" | "neg" | "mul" | "and" | "or" | "xor" | "not" | "concat" | "extract" | "shl" | "shr" | "sar" | "sext" | "zext" | "eq" | "ne" | "slt" | "sle" | "ult" | "ule" | "lzcount" | "ite" | "primitive":
                children = [self.pure_inputs(a) for a in value.args if isinstance(a, Value)]
                result = None if any(c is None for c in children) else frozenset().union(*children)
            case _: result = None
        self.pure_cache[value] = result
        return result

    def pure_value(self, value):
        dependencies = self.pure_inputs(value)
        if dependencies is None: return None
        atom = Atom("pure", value.width, (value.index, *sorted(dependencies, key=repr)))
        word = Atom("word", 64, (Polynomial.atom(atom),))
        if value.op == "ite" and word not in self.selects:
            condition, yes, no = value.args
            self.selects[word] = self.graph.source_blocks.get(value.origin), condition, self.value(yes), self.value(no)
        if value.op == "shr" or value.width == 1: self.unsigned[word] = value.width
        return atom

    def pure_term(self, term):
        match term:
            case Polynomial(width, terms):
                return Polynomial.make(width, [(tuple(self.pure_term(a) for a in m), c) for m, c in terms])
            case Atom("opaque", _, (index,)):
                return self.pure_value(self.graph.values[index]) or term
            case Atom(op, width, args):
                return Atom(op, width, tuple(self.pure_term(a) if isinstance(a, (Atom, Polynomial)) else a for a in args))
            case _: return term

    def observe(self, block, row):
        for feature in row:
            if any(a.operation == "havoc" for a in feature): continue
            self.features[block].add(feature)
            self.features[block].update((a,) for a in feature)
            for atom in feature:
                if atom in self.masks:
                    self.observe(block, self.masks[atom][0])
                    self.features[block].add((self.quotients[atom],))

    def predicates(self, value, truth, origin):
        value = self.normal.aliases.get(value, value)
        match value.op:
            case "not": return self.predicates(value.args[0], not truth, origin)
            case "and" | "or":
                a, b = (self.predicates(argument, truth, origin) for argument in value.args)
                return [left + right for left, right in product(a, b)] if (value.op == "and") == truth else a + b
            case "eq" | "ne" | "slt" | "sle" | "ult" | "ule":
                left, right = value.args
                relation = {"eq": "=", "ne": "!=", "slt": "<", "ult": "<", "sle": "<=", "ule": "<="}[value.op]
                if not truth: relation = {"=": "!=", "!=": "=", "<": ">=", "<=": ">"}[relation]
                unsigned = value.op in ("ult", "ule")
                def operand(v): return {(): v.args[0] % (1 << v.width)} if v.op == "const" and (unsigned or v.width < 32) else self.value(v)
                return [[Comparison(freeze(operand(left)), relation, freeze(operand(right)), left.width, value.op.startswith("s"), origin)]]
            case "const": return [[]] if bool(value.args[0]) == truth else []
            case _: return [[Comparison(freeze(self.value(value)), "=", (((), int(truth)),), 1, False, origin)]]

    def edge_guards(self):
        result = {}
        controls = {c["block"]: c for c in self.graph.control}
        for block, record in self.graph.blocks.items():
            control = controls.get(block)
            for target in record["successors"]:
                if control is None or control["target"] == control["fallthrough"]:
                    result[block, target] = [[]]
                    continue
                truth = control["kind"] == "goto" and target == control["target"]
                predicates = [self.graph.values[control[k]] for k in ("guard", "condition") if control[k] is not None]
                alternatives = [[]] if truth else []
                for predicate in predicates:
                    incoming = self.predicates(predicate, truth, control["origin"])
                    alternatives = [a + b for a, b in product(alternatives, incoming)] if truth else alternatives + incoming
                result[block, target] = alternatives or ([[]] if not predicates else [])
        return result

    def substitute_edge(self, feature, source, target):
        result = {(): 1}
        for atom in feature:
            if atom.operation == 'iteration':
                header, = atom.arguments
                match target == header, header in self.dominators[source], header in self.dominators[target]:
                    case True, True, _: incoming = {(atom,): 1, (): 1}
                    case True, False, _: incoming = {}
                    case False, _, False: incoming = {(Atom('havoc', 64, (source, target, repr(atom))),): 1}
                    case _: incoming = {(atom,): 1}
            elif atom in self.variables and self.graph.phi_blocks[self.variables[atom][0]] == target:
                value, hi, lo = self.variables[atom]
                position = self.graph.phi_predecessors[value].index(source)
                incoming = self.expression(self.normal.slice(value.args[position], hi, lo))
            elif atom in self.variables and self.graph.phi_blocks[self.variables[atom][0]] not in self.dominators[target]:
                incoming = {(Atom("havoc", 64, (source, target, repr(atom))),): 1}
            elif atom.operation == "nondeterministic" or atom.operation == "word" and self.word_changes(atom.arguments[0], target):
                incoming = {(Atom("havoc", 64, (source, target, repr(atom))),): 1}
            else: incoming = {(atom,): 1}
            result = multiply(result, incoming, self.degree)
        return result

    def word_changes(self, term, target):
        match term:
            case Polynomial(_, terms): return any(self.word_changes(a, target) for factors, _ in terms for a in factors)
            case Atom("phi", _, (index,)): return self.graph.phi_blocks[self.graph.values[index]] == target or self.graph.phi_blocks[self.graph.values[index]] not in self.dominators[target]
            case Atom("opaque", _, (index,)): return self.pure_inputs(self.graph.values[index]) is None
            case Atom(): return any(self.word_changes(a, target) for a in term.arguments if isinstance(a, (Atom, Polynomial)))
            case _: return False

    def close_features(self):
        self.parameter_factors = {m for fs in self.features.values() for m in fs if m and all(self.parameter(a) for a in m)}
        changed = True
        while changed:
            before = sum(map(len, self.features.values()))
            self.parameter_factors.update((a,) for features in self.features.values() for m in features for a in m if self.parameter(a))
            immutable = {feature for features in self.features.values() for feature in features
                         if all(a.operation == "input" or a.operation == "word" and invariant(a.arguments[0]) for a in feature)}
            for features in self.features.values(): features.update(immutable)
            for block, features in self.features.items():
                scalars = {a for feature in features for a in feature if a.operation == 'iteration' or a in self.variables and self.variables[a][1] - self.variables[a][2] + 1 == 32 or a.operation == "word" and a.arguments[0].width == 32 and execution_dependent(a.arguments[0])}
                features.update(tuple(sorted((a,) + m, key=repr)) for a in scalars for m in self.parameter_factors if len(m) < self.degree)
            for word, (block, condition, yes, no) in list(self.selects.items()):
                if block is None: continue
                for conjunction in self.predicates(condition, True, condition.origin) + self.predicates(condition, False, condition.origin):
                    for c in conjunction:
                        self.observe(block, thaw(c.left))
                        self.observe(block, thaw(c.right))
                self.observe(block, {(word,): 1})
                for branch in (yes, no): self.observe(block, branch)
            for source, record in self.graph.blocks.items():
                for target in record["successors"]:
                    for feature in tuple(self.features[target]):
                        try: self.observe(source, self.substitute_edge(feature, source, target))
                        except ValueError: pass
            for block, features in self.features.items():
                if len(features) > self.feature_limit: raise PrecisionLimit(f"common feature basis exceeds {self.feature_limit} at block {block:#x}")
            changed = sum(map(len, self.features.values())) != before
        for source, record in self.graph.blocks.items():
            for target in record["successors"]:
                assignments = {}
                for feature in sorted(self.features[target], key=order):
                    try: assignments[feature] = self.substitute_edge(feature, source, target)
                    except ValueError:
                        assignments[feature] = {(Atom("havoc", 64, (source, target, repr(feature))),): 1}
                self.assignments[source, target] = assignments

    def parameter(self, atom):
        return atom.operation == 'word' and atom.arguments[0].width == 32 and not execution_dependent(atom.arguments[0]) and self.program_word(atom.arguments[0])

    def program_word(self, term):
        match term:
            case Polynomial(_, terms): return all(self.program_word(a) for m, _ in terms for a in m)
            case Atom('input', width, (name,)):
                if (name, width) in self.graph.inputs: return True
                if not name.startswith('cb'): return False
                address = int(name[2:], 16)
                return any(base.startswith('cb') and int(base[2:], 16) <= address and address * 8 + width <= int(base[2:], 16) * 8 + size for base, size in self.graph.inputs)
            case Atom(): return all(self.program_word(a) for a in term.arguments if isinstance(a, (Atom, Polynomial)))
            case _: return True

    def replace_atom(self, row, atom, replacement):
        result = {}
        for monomial, coefficient in row.items():
            term = {(): coefficient}
            for a in monomial: term = multiply(term, replacement if a == atom else {(a,): 1}, self.degree)
            result = add(result, term)
        return result

    def make_templates(self):
        extents = [thaw(frozen) for frozen in self.uniform_bounds]
        extents.extend({(q,): 1} for q in self.quotients.values() if invariant(q.arguments[0]))
        self.templates = {}
        for block, features in self.features.items():
            templates = set()
            scalars = [m for m in features if len(m) == 1 and (m[0].operation == 'iteration' or m[0] in self.variables and self.variables[m[0]][1] - self.variables[m[0]][2] + 1 == 32)]
            for scalar in scalars:
                for extent in extents + [{}]:
                    row = add({scalar: 1}, extent, -1)
                    if all(m in features for m in row):
                        templates.add(freeze(row))
                        templates.add(freeze({m: -c for m, c in row.items()}))
            for feature in features:
                if feature and all(a.operation == "word" for a in feature):
                    templates.add(freeze({feature: 1}))
                    templates.add(freeze({feature: -1}))
            for constraint in self.environment:
                row = add(thaw(constraint.left), thaw(constraint.right), -1)
                templates.add(freeze(row))
                templates.add(freeze({m: -v for m, v in row.items()}))
            for alternatives in self.conditions:
                for conjunction in alternatives:
                    for c in conjunction:
                        row = add(thaw(c.left), thaw(c.right), -1)
                        if all(m in features for m in row):
                            templates.add(freeze(row))
                            templates.add(freeze({m: -v for m, v in row.items()}))
                        if not c.signed and c.width > 1 and c.relation in ('<', '<=', '>', '>='):
                            for side in (c.left, c.right):
                                if all(m in features for m, _ in side):
                                    templates.add(side)
                                    templates.add(freeze({m: -v for m, v in side}))
            for access in self.accesses:
                if access['block'] != block: continue
                bases = [m for m, c in access['expression'].items() if len(m) == 1 and m[0].operation == 'input' and m[0].width == 64 and c == 1]
                indices = []
                if len(bases) == 1:
                    index = {m: c / access['bytes'] for m, c in access['expression'].items() if m != bases[0]}
                    changing = {a for m in index for a in m if a.operation == 'variable' or a.operation == 'word' and execution_dependent(a.arguments[0])}
                    if len(changing) == 1: indices.append(index)
                for index in indices:
                    for extent in extents:
                        row = add(index, extent, -1)
                        if all(m in features for m in row):
                            templates.add(freeze(row))
                            templates.add(freeze({m: -c for m, c in row.items()}))
            self.templates[block] = sorted({direction(thaw(row)) for row in templates} - {()}, key=repr)
        for block, features in self.features.items():
            templates = set(self.templates[block])
            for word, (owner, _, yes, no) in self.selects.items():
                if owner != block: continue
                for branch in (yes, no):
                    for monomial, coefficient in branch.items():
                        if len(monomial) != 1 or abs(coefficient) != 1: continue
                        source = monomial[0]
                        replacement = {m: c / coefficient for m, c in add({(word,): 1}, add(branch, {(source,): coefficient}, -1), -1).items()}
                        for frozen in tuple(templates):
                            try: row = self.replace_atom(thaw(frozen), source, replacement)
                            except ValueError: continue
                            if row and all(m in features for m in row): templates.add(freeze(row))
            self.templates[block] = sorted({direction(thaw(row)) for row in templates} - {()}, key=repr)
        dom = self.dominators
        changed = True
        while changed:
            changed = False
            immutable = {row for rows in self.templates.values() for row in rows
                         if all(a.operation == 'input' or a.operation == 'word' and invariant(a.arguments[0]) for m, _ in row for a in m)}
            for block, rows in self.templates.items():
                added = {row for row in immutable if all(m in self.features[block] for m, _ in row)} - set(rows)
                if added:
                    self.templates[block] = sorted(set(rows) | added, key=repr)
                    changed = True
            for (source, target), assignments in self.assignments.items():
                if target in dom[source]: continue
                templates = set(self.templates[source])
                for frozen in self.templates[target]:
                    row = {}
                    for feature, coefficient in frozen: row = add(row, assignments[feature], coefficient)
                    row.pop((), None)
                    if not row or any(m not in self.features[source] for m in row): continue
                    scale = abs(row[min(row, key=order)])
                    templates.add(freeze({m: c / scale for m, c in row.items()}))
                if len(templates) > 1024: raise PrecisionLimit("common guard-template basis exceeds 1024 expressions")
                if len(templates) != len(self.templates[source]):
                    self.templates[source] = sorted(templates, key=repr)
                    changed = True

    def lift_equalities(self, equalities, features):
        factors = [m for m in features if all(a.operation == "word" or a in self.variables and self.variables[a][1] - self.variables[a][2] < 32 for a in m)]
        result = list(equalities)
        for row in equalities:
            for factor in factors:
                try: lifted = multiply(row, {factor: 1}, self.degree)
                except ValueError: continue
                if all(m in features for m in lifted): result.append(lifted)
        return result

    def lift_inequalities(self, state, inequalities, features):
        result = list(inequalities)
        positive = [m for m in self.parameter_factors if (bound := state.query({m: -1})) is not None and bound <= 0]
        for row, upper in inequalities:
            for factor in positive:
                try: lifted = multiply(add(row, {(): -upper}), {factor: 1}, self.degree)
                except ValueError: continue
                if all(m in features for m in lifted): result.append((lifted, 0))
        return result

    def assume(self, state, conjunction, block=None):
        if not conjunction: return state
        equalities, inequalities = [], []
        for comparison in conjunction:
            if not comparison.signed and comparison.width > 1 and comparison.relation in ("<", "<=", ">", ">="):
                for side in (comparison.left, comparison.right):
                    inequalities.extend([(thaw(side), (1 << comparison.width) - 1), ({m: -c for m, c in side}, 0)])
            expression = add(thaw(comparison.left), thaw(comparison.right), -1)
            reduced = state.reduce(expression)
            if comparison.relation == "=": equalities.append(expression)
            elif comparison.relation == "!=" and not reduced: return None
            elif not reduced or set(reduced) == {()}:
                constant = reduced.get((), 0)
                valid = {"<": constant < 0, "<=": constant <= 0, ">": constant > 0, ">=": constant >= 0, "!=": constant != 0}[comparison.relation]
                if not valid: return None
            if comparison.relation in ("<", "<="): inequalities.append((expression, -int(comparison.relation == "<")))
            if comparison.relation in (">", ">="): inequalities.append(({m: -c for m, c in expression.items()}, -int(comparison.relation == ">")))
        features = self.features[block] if block is not None else set().union(*self.features.values())
        return state.assume(self.lift_equalities(equalities, features), inequalities)

    def refine_selects(self, state, block):
        state = self.refine_numeric_relations(state, block)
        if not any(owner == block for owner, *_ in self.selects.values()): return state
        key = block, state
        if key not in self.select_cache: self.select_cache[key] = self.compute_selects(state, block)
        return self.select_cache[key]

    def approximate_value(self, value):
        value = self.normal.aliases.get(value, value)
        if value in self.approximate_cache: return self.approximate_cache[value]
        if value.op == 'phi': return False
        result = value.op == 'primitive' and value.args[0] == 'mufu_rcp' or any(self.approximate_value(a) for a in value.args if isinstance(a, Value))
        self.approximate_cache[value] = result
        return result

    def approximate_term(self, term):
        match term:
            case Atom('pure' | 'opaque', _, (index, *_)): return self.approximate_value(self.graph.values[index])
            case Atom(): return any(self.approximate_term(a) for a in term.arguments if isinstance(a, (Atom, Polynomial)))
            case Polynomial(): return any(self.approximate_term(a) for m, _ in term.terms for a in m)
            case _: return False

    def numeric_context(self, state):
        def inputs_only(row):
            return all(len(m) <= 1 and all(a.operation == 'word' and all(b.operation == 'input' for p, _ in a.arguments[0].terms for b in p)
                                          for a in m) for m in row)
        directions = {freeze(self.value(value)) for value in self.graph.inputs.values() if value.width == 32}
        directions.update(freeze(add(thaw(c.left), thaw(c.right), -1))
                          for conjunctions in [self.environment, *(conjunction for alternatives in self.conditions for conjunction in alternatives)]
                          for c in conjunctions)
        inequalities = []
        for frozen in sorted(directions, key=repr):
            reduced = state.reduce(thaw(frozen))
            if not inputs_only(reduced): continue
            for sign in (-1, 1):
                expression = {m: c * sign for m, c in reduced.items()}
                if (upper := state.query(expression)) is not None: inequalities.append((expression, upper))
        return ConstraintState().assume([thaw(row) for row in state.rows if inputs_only(thaw(row))], inequalities)

    def refine_numeric_relations(self, state, block):
        if block not in self.numeric_relation_blocks: return state
        key = block, state
        if key in self.numeric_relation_cache: return self.numeric_relation_cache[key]
        rows = [thaw(row) for row in self.templates[block] if len(row) > 1
                and all(a.operation in ('word', 'input') for m, _ in row for a in m)
                and any(self.approximate_term(a) for m, _ in row for a in m)]
        result = state
        if rows:
            try:
                context = self.numeric_context(state)
                model = NumericRelations(self, context)
                expressions = [(row, model.row(row)) for row in rows]
                for atom in list(model.symbols): model.link(atom)
                model.reduce_moduli()
                if model.solver.check() == z3.sat:
                    model.solver.set(timeout=1000)
                    inequalities = []
                    for row, expression in expressions:
                        step = Fraction(1, lcm(*(c.denominator for c in row.values())))
                        if model.prove(expression < z3.RealVal(str(step))):
                            inequalities.append((row, -step if model.prove(expression < 0) else 0))
                    result = state.assume(inequalities=inequalities) or state
            except (NotImplementedError, KeyError): pass
        self.numeric_relation_cache[key] = result
        return result

    def compute_selects(self, state, block):
        groups = {}
        for word, (owner, condition, yes, no) in self.selects.items():
            if owner == block: groups.setdefault(condition, []).append((word, yes, no))
        states = [state]
        for condition, assignments in groups.items():
            incoming = []
            for previous in states:
                for truth in (False, True):
                    for conjunction in self.predicates(condition, truth, condition.origin):
                        guarded = self.assume(previous, conjunction, block)
                        if guarded is None: continue
                        equations = [add({(word,): 1}, yes if truth else no, -1) for word, yes, no in assignments]
                        guarded = guarded.assume(self.lift_equalities(equations, self.features[block]))
                        if guarded is not None: incoming.append(guarded)
            states = incoming
            if not states: return None
            if len(states) > 64: raise PrecisionLimit('local conditional value analysis exceeds 64 partitions')
        result = states[0]
        for other in states[1:]: result = result.join(other, self.templates[block])
        return result

    def transfer(self, source, target, state):
        incoming = None
        state = self.refine_selects(state, source)
        if state is None: return None
        for conjunction in self.guards[source, target]:
            guarded = self.assume(state, conjunction, source)
            if guarded is None: continue
            if self.numeric is not None and (source, target) in self.numeric.edges:
                try: context = self.numeric.guard(source, target, self.numeric.states[source])
                except Infeasible: continue
                if context is None: continue
                reductions, intervals = [], []
                for feature in self.features[source]:
                    if len(feature) != 1: continue
                    atom = feature[0]
                    if atom in self.variables:
                        value, hi, lo = self.variables[atom]
                        if hi - lo >= 32: continue
                        term = self.normal.slice(value, hi, lo)
                    elif atom.operation == "word": term = atom.arguments[0]
                    else: continue
                    try: interval = self.numeric.word(term, context).interval
                    except Infeasible: continue
                    if term.width > 1: interval = interval.signed(term.width)
                    intervals.extend([({feature: 1}, interval.upper), ({feature: -1}, -interval.lower)])
                    if interval.lower == interval.upper: reductions.append({feature: 1, (): -interval.lower})
                for feature in self.features[source] | self.features[target]:
                    for atom in feature:
                        if atom.operation != "word": continue
                        original = atom.arguments[0].single_atom()
                        if original is None or original.operation != "and": continue
                        for value, mask in (original.arguments, original.arguments[::-1]):
                            constant = mask.constant_value()
                            if constant is None or constant & (constant + 1): continue
                            try: bounded = self.numeric.word(value, context).interval.upper <= constant
                            except Infeasible: bounded = False
                            if bounded: reductions.append(add({(atom,): 1}, self.expression(value), -1))
                guarded = guarded.assume(self.lift_equalities(reductions, self.features[source]), intervals)
                if guarded is None: continue
            image = guarded.image(self.assignments[source, target], self.templates[target])
            image = self.numeric_reduce(image, target)
            if image is None: continue
            incoming = image if incoming is None else incoming.join(image, self.templates[target])
        return incoming

    def initial_state(self):
        entry = min(self.graph.blocks)
        initial = self.assume(ConstraintState(), self.environment, entry)
        equalities, inequalities = [], []
        for feature in self.features[entry]:
            if len(feature) != 1 or feature[0].operation != "word": continue
            source = feature[0].arguments[0]
            interval = self.range_context.integer(source, True)
            inequalities.extend([({feature: 1}, interval.upper), ({feature: -1}, -interval.lower)])
            if interval.lower == interval.upper:
                equalities.append({feature: 1, (): -interval.lower})
        initial = initial.assume(self.lift_equalities(equalities, self.features[entry]), inequalities)
        products = []
        for feature in self.features[entry]:
            if len(feature) < 2 or any(a.operation != "word" for a in feature): continue
            interval = Interval(1, 1)
            for atom in feature:
                upper, negative = initial.query({(atom,): 1}), initial.query({(atom,): -1})
                if upper is None or negative is None: break
                interval = interval * Interval(-negative, upper)
            else: products.extend([({feature: 1}, interval.upper), ({feature: -1}, -interval.lower)])
        return self.numeric_reduce(initial.assume(inequalities=products), entry)

    def solve(self, limit=512):
        entry = min(self.graph.blocks)
        self.states[entry] = self.initial_state()
        pending = deque([entry])
        visits = {}
        while pending:
            source = pending.popleft()
            self.iterations += 1
            if self.iterations > limit: raise PrecisionLimit("common equality fixed point exceeded its block-visit limit")
            for target in self.graph.blocks[source]["successors"]:
                incoming = self.transfer(source, target, self.states[source])
                if incoming is None: continue
                self.edges[source, target] = incoming
                previous = self.states.get(target)
                visits[target] = visits.get(target, 0) + 1
                backedge = target in self.dominators[source]
                merged = incoming if previous is None else previous.join(incoming, self.templates[target], backedge and visits[target] > 8)
                merged = self.numeric_reduce(merged, target)
                if merged is None: continue
                if merged != previous:
                    self.states[target] = merged
                    if target not in pending: pending.append(target)
        return self

    def verify(self):
        return all(self.states[target].contains(incoming)
                   for source, state in self.states.items() for target in self.graph.blocks[source]["successors"]
                   if (incoming := self.transfer(source, target, state)) is not None)

    def report(self):
        return {"domain": "bounded polynomial equalities over the conditional integer interpretation",
                "degree": self.degree, "iterations": self.iterations, "fixed_point_verified": self.verify(),
                "blocks": {f"{b:#x}": [show(dict(r)) + " = 0" for r in state.rows] for b, state in self.states.items()},
                "bounds": {f"{b:#x}": [show(dict(row)) + f" <= {bound}" for row, bound in state.bounds] for b, state in self.states.items()},
                "accesses": [{"origin": a["origin"], "address": show(self.states[a["block"]].reduce(a["expression"]))} for a in self.accesses if a["block"] in self.states],
                "integer_interpretation_proven": False, "cast_obligations": list(self.casts.values())}
