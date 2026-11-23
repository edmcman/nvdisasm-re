from dataclasses import dataclass
from math import gcd

from access_relations import Atom, Polynomial, signed
from word_ranges import Interval, input_view


class Infeasible(Exception):
    pass


@dataclass(frozen=True)
class WordRange:
    interval: Interval
    modulus: int = 1
    residue: int = 0

    @staticmethod
    def full(width): return WordRange(Interval(0, (1 << width) - 1))

    @staticmethod
    def constant(value, width):
        value %= 1 << width
        return WordRange(Interval(value, value), 0, value)

    def join(self, other):
        return WordRange(Interval(min(self.interval.lower, other.interval.lower), max(self.interval.upper, other.interval.upper)),
                         gcd(self.modulus, other.modulus, self.residue - other.residue), self.residue)

    def restrict(self, interval):
        lower, upper = max(self.interval.lower, interval.lower), min(self.interval.upper, interval.upper)
        if self.modulus:
            lower += (self.residue - lower) % self.modulus
            upper -= (upper - self.residue) % self.modulus
        elif not lower <= self.residue <= upper: return None
        if lower > upper: return None
        return WordRange(Interval(lower, upper), self.modulus, self.residue)

    def add(self, other, width):
        modulus = gcd(self.modulus, other.modulus, 1 << width)
        return WordRange((self.interval + other.interval).modulo(width), modulus, (self.residue + other.residue) % (modulus or (1 << width)))

    def multiply(self, other, width):
        modulus = gcd(self.modulus * other.modulus, self.modulus * other.residue, other.modulus * self.residue, 1 << width)
        return WordRange((self.interval * other.interval).modulo(width), modulus, (self.residue * other.residue) % (modulus or (1 << width)))

    def scale(self, coefficient, width):
        modulus = gcd(abs(coefficient) * self.modulus, 1 << width)
        return WordRange((self.interval * Interval(coefficient, coefficient)).modulo(width), modulus,
                         (self.residue * coefficient) % (modulus or (1 << width)))


class ScalarBounds:
    def __init__(self, shapes, loops):
        self.shapes, self.loops, self.graph, self.normal = shapes, loops, loops.graph, loops.normal
        self.states, self.edges, self.iterations = {}, {}, 0
        self.updates = {}
        self.projections = {}
        for value, block in self.graph.phi_blocks.items():
            for hi, lo in ([(value.width - 1, 0)] if value.width <= 32 else [(31, 0), (63, 32)]):
                term = self.normal.slice(value, hi, lo)
                atom = term.single_atom()
                if atom is not None and loops.projection(atom) == (value, hi, lo): self.projections.setdefault(block, []).append((atom, value, hi, lo))

    def word(self, term, state):
        result = self.compute(term, state)
        key = input_view(term) if isinstance(term, Atom) else None
        if key in self.shapes.ranges.inputs:
            result = result.restrict(self.shapes.ranges.inputs[key])
            if result is None: raise Infeasible()
        return result

    def compute(self, term, state):
        if term in state:
            stored = state[term]
            if isinstance(term, Atom) and self.loops.projection(term) is not None: return stored
            computed = self.word(term, {key: value for key, value in state.items() if key != term})
            result = computed.restrict(stored.interval) if computed.modulus % (stored.modulus or 1) == 0 else stored.restrict(computed.interval)
            if result is None: raise Infeasible()
            return result
        width = term.width
        match term:
            case Polynomial(_, terms):
                for factors, coefficient in terms:
                    if len(factors) != 1 or signed(coefficient, width) != -1 or factors[0].operation != "and": continue
                    for source, mask in (factors[0].arguments, factors[0].arguments[::-1]):
                        constant = mask.constant_value()
                        if constant is None or constant & (constant + 1) or term != source - Polynomial.atom(factors[0]): continue
                        original = self.word(source, state)
                        interval = Interval(original.interval.lower & ~constant, original.interval.upper & ~constant)
                        return WordRange(interval, constant + 1, 0)
                result = WordRange.constant(0, width)
                interval = Interval(0, 0)
                for factors, coefficient in terms:
                    product = WordRange.constant(1, width)
                    term_interval = Interval(signed(coefficient, width), signed(coefficient, width))
                    for atom in factors:
                        atom_range = self.word(atom, state)
                        product = product.multiply(atom_range, width)
                        term_interval = term_interval * atom_range.interval
                    product = product.scale(signed(coefficient, width), width)
                    result = result.add(product, width)
                    interval = interval + term_interval
                return WordRange(interval.modulo(width), result.modulus, result.residue)
            case Atom("phi") | Atom("opaque"): return WordRange.full(width)
            case Atom("and", _, (left, right)):
                a, b = self.word(left, state), self.word(right, state)
                upper = min(a.interval.upper, b.interval.upper)
                return WordRange(Interval(0, upper))
            case Atom("extract", _, (source, _, lo)):
                parent = self.word(source, state)
                return WordRange(Interval(parent.interval.lower >> lo, parent.interval.upper >> lo).modulo(width))
            case Atom("zext", _, (source,)): return self.word(source, state)
            case Atom("sext", _, (source,)):
                return WordRange(self.word(source, state).interval.signed(source.width).modulo(width))
            case _: return WordRange(self.shapes.ranges.word(term))

    def expression(self, value, state):
        result = self.word(self.normal(value), state)
        if result.interval.lower == result.interval.upper: return WordRange.constant(result.interval.lower, value.width)
        return result


    def restrict(self, expression, interval, state, is_signed):
        constant = expression.constant_value()
        if constant is not None:
            value = signed(constant, expression.width) if is_signed else constant
            return state if interval.lower <= value <= interval.upper else None
        variables = [(m[0], signed(c, expression.width)) for m, c in expression.terms if len(m) == 1]
        allowed_result = interval.modulo(expression.width)
        narrowed_result = self.word(expression, state).restrict(allowed_result)
        if narrowed_result is None: return None
        state = {**state, expression: narrowed_result}
        if len(variables) != 1 or any(len(m) > 1 for m, _ in expression.terms): return state
        atom, coefficient = variables[0]
        if coefficient != 1: return state
        offset = sum(signed(c, expression.width) for m, c in expression.terms if not m)
        allowed = (narrowed_result.interval + Interval(-offset, -offset)).modulo(expression.width)
        narrowed = self.word(atom, state).restrict(allowed)
        if narrowed is None: return None
        return {**state, atom: narrowed}

    def comparison(self, value, truth, state):
        op, (left_value, right_value) = value.op, value.args
        left, right = self.normal(left_value), self.normal(right_value)
        is_signed = op.startswith("s")
        a, b = self.word(left, state).interval, self.word(right, state).interval
        if is_signed: a, b = a.signed(left.width), b.signed(right.width)
        width = left.width
        low, high = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if is_signed else (0, (1 << width) - 1)
        relation = {"slt": "<", "ult": "<", "sle": "<=", "ule": "<=", "eq": "=", "ne": "!="}[op]
        if not truth: relation = {"<": ">=", "<=": ">", "=": "!=", "!=": "="}[relation]
        if relation in (">", ">="):
            left, right, a, b = right, left, b, a
            relation = {">": "<", ">=": "<="}[relation]
        match relation:
            case "<" | "<=":
                adjustment = int(relation == "<")
                if a.lower > b.upper - adjustment: return None
                state = self.restrict(left, Interval(low, b.upper - adjustment), state, is_signed)
                if state is None: return None
                return self.restrict(right, Interval(a.lower + adjustment, high), state, is_signed)
            case "=":
                if max(a.lower, b.lower) > min(a.upper, b.upper): return None
                allowed = Interval(max(a.lower, b.lower), min(a.upper, b.upper))
                state = self.restrict(left, allowed, state, is_signed)
                return None if state is None else self.restrict(right, allowed, state, is_signed)
            case "!=":
                if a.lower == a.upper == b.lower == b.upper: return None
                if b.lower == b.upper == a.lower and a.lower < a.upper:
                    return self.restrict(left, Interval(a.lower + 1, a.upper), state, is_signed)
                if b.lower == b.upper == a.upper and a.lower < a.upper:
                    return self.restrict(left, Interval(a.lower, a.upper - 1), state, is_signed)
                return state

    def assume(self, value, truth, state):
        value = self.normal.aliases.get(value, value)
        match value.op:
            case "not": return self.assume(value.args[0], not truth, state)
            case "and" | "or":
                conjunctive = (value.op == "and") == truth
                if conjunctive:
                    first = self.assume(value.args[0], truth, state)
                    return None if first is None else self.assume(value.args[1], truth, first)
                return self.join_states(self.assume(value.args[0], truth, state), self.assume(value.args[1], truth, state))
            case "eq" | "ne" | "slt" | "sle" | "ult" | "ule": return self.comparison(value, truth, state)
            case _:
                result = self.expression(value, state)
                if result.interval.lower == result.interval.upper and bool(result.interval.lower) != truth: return None
                return self.restrict(self.normal(value), Interval(int(truth), int(truth)), state, False) if value.width == 1 else state

    @staticmethod
    def join_states(a, b):
        if a is None: return b
        if b is None: return a
        return {key: a.get(key, WordRange.full(key.width)).join(b.get(key, WordRange.full(key.width))) for key in a.keys() | b.keys()}

    def guard(self, source, target, state):
        control = next((c for c in self.graph.control if c["block"] == source), None)
        if control is not None and control["target"] != control["fallthrough"]:
            truth = control["kind"] == "goto" and target == control["target"]
            predicates = [self.graph.values[control[k]] for k in ("guard", "condition") if control[k] is not None]
            if truth:
                for predicate in predicates:
                    state = self.assume(predicate, True, state)
                    if state is None: return None
            elif len(predicates) == 1: state = self.assume(predicates[0], False, state)
        return state

    def edge(self, source, target, state):
        state = self.guard(source, target, state)
        if state is None: return None
        def survives(term):
            match term:
                case Polynomial(): return all(survives(a) for m, _ in term.terms for a in m)
                case Atom():
                    projection = self.loops.projection(term)
                    return (projection is None or self.graph.phi_blocks[projection[0]] != target) and all(survives(a) for a in term.arguments if isinstance(a, (Atom, Polynomial)))
        updated = {key: value for key, value in state.items() if survives(key)}
        for atom, value, hi, lo in self.projections.get(target, []):
            position = self.graph.phi_predecessors[value].index(source)
            expression = self.normal.slice(value.args[position], hi, lo)
            updated[atom] = self.word(expression, state)
        return updated

    def solve(self, limit=2048):
        entry = min(self.graph.blocks)
        partitions = {entry: {(): {}}}
        self.states[entry] = {}
        pending = [(entry, ())]
        while pending:
            source, signature = pending.pop(0)
            self.iterations += 1
            if self.iterations > limit: raise RuntimeError("scalar range fixed point exceeded 2048 block visits")
            state = partitions[source][signature]
            for target in self.graph.blocks[source]["successors"]:
                try: outgoing = self.edge(source, target, state)
                except Infeasible: continue
                if outgoing is None: continue
                self.edges[source, target] = self.join_states(self.edges.get((source, target)), outgoing)
                target_signature = self.signature(outgoing)
                target_partitions = partitions.setdefault(target, {})
                if len(target_partitions) >= 128 and target_signature not in target_partitions:
                    raise RuntimeError("scalar range analysis exceeded 128 predicate partitions")
                previous = target_partitions.get(target_signature)
                merged = self.join_states(previous, outgoing)
                update_key = target, target_signature
                self.updates[update_key] = self.updates.get(update_key, 0) + 1
                if previous is not None and self.updates[update_key] > 8:
                    for atom, current in list(merged.items()):
                        old = previous.get(atom, WordRange.full(atom.width))
                        interval = Interval(0 if current.interval.lower < old.interval.lower else current.interval.lower,
                                            (1 << atom.width) - 1 if current.interval.upper > old.interval.upper else current.interval.upper)
                        merged[atom] = WordRange(interval, current.modulus, current.residue)
                if merged != previous:
                    target_partitions[target_signature] = merged
                    item = target, target_signature
                    if item not in pending: pending.append(item)
        for block, items in partitions.items():
            state = None
            for item in items.values(): state = self.join_states(state, item)
            self.states[block] = state
        self.partitions = partitions
        return self

    @staticmethod
    def signature(state):
        return tuple(sorted((atom.arguments, value.interval.lower) for atom, value in state.items()
                            if isinstance(atom, Atom) and atom.width == 1 and value.interval.lower == value.interval.upper))

    def verify(self):
        for source, states in self.partitions.items():
            for state in states.values():
                for target in self.graph.blocks[source]["successors"]:
                    try: outgoing = self.edge(source, target, state)
                    except Infeasible: continue
                    if outgoing is None: continue
                    stored = self.partitions[target].get(self.signature(outgoing))
                    if stored is None: return False
                    for key, value in outgoing.items():
                        allowed = stored.get(key, WordRange.full(key.width))
                        if value.interval.lower < allowed.interval.lower or value.interval.upper > allowed.interval.upper: return False
                        if allowed.modulus and (value.modulus % allowed.modulus or (value.residue - allowed.residue) % allowed.modulus): return False
        return True
