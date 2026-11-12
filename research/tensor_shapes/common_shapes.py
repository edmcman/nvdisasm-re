from dataclasses import dataclass
from fractions import Fraction

from access_relations import Atom, execution_dependent
from common_constraints import CommonConstraints, freeze, show, thaw
from constraint_domain import Equalities, add, multiply, order
from constraint_state import ConstraintState
from type_system import Printer, Type


def uniform(expression):
    return all(a.operation not in ("variable", "nondeterministic", "iteration") and
               not (a.operation == "word" and execution_dependent(a.arguments[0])) for m in expression for a in m)


def substitute(expression, atom, replacement):
    result = {}
    for factors, coefficient in expression.items():
        term = {(): coefficient}
        for factor in factors: term = multiply(term, replacement if factor == atom else {(factor,): 1})
        result = add(result, term)
    return result


def coefficient(expression, atom):
    result = {}
    for factors, c in expression.items():
        if factors.count(atom) > 1: return None
        if atom in factors: result[tuple(a for a in factors if a != atom)] = c
    return result


def normalize_coefficients(state, expression):
    groups = {}
    for feature, value in expression.items():
        fixed = tuple(a for a in feature if uniform({(a,): 1}))
        moving = tuple(a for a in feature if not uniform({(a,): 1}))
        groups[moving] = add(groups.get(moving, {}), {fixed: value})
    result = {}
    for moving, values in groups.items(): result = add(result, multiply(state.reduce(values), {moving: 1}))
    return result


def divide(expression, radix):
    if len(radix) != 1: return None
    (factors, denominator), = radix.items()
    denominator = Fraction(denominator)
    if denominator.denominator != 1 or denominator <= 0: return None
    quotient, remainder = {}, {}
    for monomial, c in expression.items():
        c = Fraction(c)
        remaining = list(monomial)
        for a in factors:
            if a not in remaining: break
            remaining.remove(a)
        else:
            if not factors:
                if c.denominator != 1: return None
                q, r = divmod(int(c), int(denominator))
            elif (c / denominator).denominator == 1: q, r = c / denominator, 0
            else: q, r = 0, c
            if q: quotient[tuple(remaining)] = q
            if r: remainder[monomial] = r
            continue
        remainder[monomial] = c
    return quotient, remainder


@dataclass
class Access:
    record: dict
    state: object
    index: dict
    coordinates: list
    original: dict
    unresolved: str | None = None

    def expand(self, expression):
        result = expression
        for atom, definition in reversed(self.coordinates): result = substitute(result, atom, definition)
        return result

    def upper(self, expression): return self.state.query(self.expand(expression))


def decompose(access, expression, extent):
    divided = divide(expression, extent)
    if divided is None: return None
    quotient, remainder = divided
    upper = access.upper(remainder)
    lower = access.upper(add({m: -c for m, c in remainder.items()}, extent, -1))
    if upper is not None and upper <= -1 and lower is not None and lower <= 0:
        return add(quotient, {(): -1}), add(remainder, extent)
    return quotient, remainder


class CommonShapes:
    def __init__(self, inference, ranges=None):
        self.inference, self.ranges = inference, ranges or {}
        self.access_states = {}
        self.analysis = CommonConstraints(inference, ranges).solve()
        if not self.analysis.verify(): raise RuntimeError("common constraint fixed point did not verify")
        self.printer = Printer(inference.solver)
        self.roots = {}
        for (name, width), value in inference.graph.inputs.items():
            if isinstance(resolved := inference.solver.resolve(inference.types[value]), Type) and resolved.tag == "pointer":
                expression = self.analysis.value(value)
                if len(expression) == 1:
                    (m, c), = expression.items()
                    if len(m) == 1 and c == 1: self.roots[m[0]] = name
        program = set()
        for alternatives in self.analysis.conditions:
            for conjunction in alternatives:
                for c in conjunction:
                    left, right = thaw(c.left), thaw(c.right)
                    for side in (left, right):
                        if uniform(side): program.add(freeze(side))
                    upper = add(right, {(): int(c.relation == '<=')}) if c.relation in ('<', '<=') and not uniform(left) and uniform(right) else add(left, {(): int(c.relation == '>=')}) if c.relation in ('>', '>=') and uniform(left) and not uniform(right) else None
                    if upper is not None: program.add(freeze(upper))
        self.program_extents = program
        candidates = program | {c.right for c in self.analysis.environment}
        self.extents = [thaw(row) for row in candidates if uniform(thaw(row)) and thaw(row) and
                        (set(thaw(row)) != {()} or thaw(row)[()] > 0)]

    def coordinates(self, state, index, block):
        index = normalize_coefficients(state, index)
        expressions = []
        for alternatives in self.analysis.conditions:
            for conjunction in alternatives:
                for c in conjunction:
                    if c.width != 32: continue
                    for expression in (thaw(c.left), thaw(c.right)):
                        if expression and not uniform(expression): expressions.append(expression)
        expressions.extend({(a,): 1} for m in self.analysis.features[block] for a in m if a in self.analysis.variables and self.analysis.variables[a][1] - self.analysis.variables[a][2] == 31)
        result, substitutions = [], []
        changed = True
        while changed:
            changed, seen = False, set()
            for raw in expressions:
                expression = state.reduce(raw)
                if freeze(expression) in seen: continue
                seen.add(freeze(expression))
                if not any(state.query(add(expression, extent, -1)) is not None and state.query(add(expression, extent, -1)) <= -1 for extent in self.extents): continue
                for atom, replacement in substitutions: expression = substitute(expression, atom, replacement)
                possible = []
                for m, c in expression.items():
                    if len(m) != 1: continue
                    atom = m[0]
                    changing = atom.operation == "variable" or atom.operation == "word" and execution_dependent(atom.arguments[0])
                    if not changing or any(atom == a for a, _ in substitutions): continue
                    stride = coefficient(index, atom)
                    if stride is None: continue
                    stride = {f: v / c for f, v in stride.items()}
                    if stride and uniform(stride) and all(v > 0 for v in stride.values()): possible.append((atom, c))
                if not possible: continue
                atom, c = min(possible, key=lambda x: order((x[0],)))
                coordinate = Atom("coordinate", 64, (f"c{len(result)}",))
                replacement = {m: v / c for m, v in add({(coordinate,): 1}, add(expression, {(atom,): c}, -1), -1).items()}
                index = normalize_coefficients(state, substitute(index, atom, replacement))
                substitutions.append((atom, replacement))
                result.append((coordinate, raw))
                changed = True
        return index, result

    def access(self, record):
        if record["block"] not in self.analysis.states: return None
        temporaries = frozenset(a for m in record["expression"] for a in m if a.operation == "variable")
        context = record["block"], record["guard"], temporaries
        state = self.access_states.get(context)
        if state is None:
            state = self.analysis.states[record["block"]]
            alternatives = [[]]
            if record["guard"] is not None:
                alternatives = self.analysis.predicates(self.inference.graph.values[record["guard"]], True, record["origin"])
            guarded = [s for conjunction in alternatives if (s := self.analysis.assume(state, conjunction, record["block"])) is not None]
            if not guarded: return None
            state = guarded[0]
            for other in guarded[1:]: state = state.join(other, self.analysis.templates[record["block"]])
            state = self.analysis.refine_selects(state, record['block'])
            if state is None: return None
            state = self.analysis.numeric_reduce(state, record['block'])
            if state is None: return None
            key = lambda m: (not any(a in temporaries for a in m), order(m))
            state = ConstraintState(Equalities.make([dict(r) for r in state.rows], key), state.bounds)
            self.access_states[context] = state
        expression = state.reduce(record["expression"])
        bases = [(a, c) for m, c in expression.items() if len(m) == 1 for a in m if a in self.roots]
        if len(bases) != 1 or bases[0][1] != 1: return Access(record, state, {}, [], {}, "address base not determined by common constraints")
        root = bases[0][0]
        offset = add(expression, {(root,): 1}, -1)
        index = {m: c / record["bytes"] for m, c in offset.items()}
        rewritten, coordinates = self.coordinates(state, index, record["block"])
        record = {**record, "buffer": self.roots[root]}
        return Access(record, state, rewritten, coordinates, index)

    def options(self, access, expression):
        return {freeze(extent) for extent in self.extents
                if (upper := access.upper(add(expression, extent, -1))) is not None and upper <= -1}

    def dimension(self, accesses, expressions):
        choices, strides = [], set()
        for access, expression in zip(accesses, expressions):
            for atom, _ in access.coordinates:
                stride = coefficient(expression, atom)
                if stride == {(): 1}: choices.append(self.options(access, {(atom,): 1}))
                elif stride and uniform(stride): strides.add(freeze(stride))
        constants = [e.get((), 0) for e in expressions if not e or set(e) == {()}]
        if len(constants) == len(expressions) and all(Fraction(c).denominator == 1 for c in constants): return {(): Fraction(max(constants, default=0) + 1)}
        common = set.intersection(*choices) if choices else set()
        structural = common & strides
        if len(structural) == 1: return thaw(next(iter(structural)))
        whole = [self.options(a, e) for a, e in zip(accesses, expressions)]
        bounded = set.intersection(*whole) if whole else set()
        common = bounded or common
        if not common: return None
        minimal = []
        for candidate in common:
            dominated = any(other != candidate and all((u := a.state.query(add(thaw(other), thaw(candidate), -1))) is not None and u <= 0 for a in accesses)
                            and (repr(other) < repr(candidate) or not all((u := a.state.query(add(thaw(candidate), thaw(other), -1))) is not None and u <= 0 for a in accesses)) for other in common)
            if not dominated: minimal.append(candidate)
        preferred = [candidate for candidate in minimal if candidate in self.program_extents]
        minimal = preferred or minimal
        return thaw(minimal[0]) if len(minimal) == 1 else None

    def report(self):
        accesses, residuals = [], []
        for record in self.analysis.accesses:
            access = self.access(record)
            if access is None: continue
            if access.unresolved: residuals.append({"origin": record["origin"], "reason": access.unresolved})
            else: accesses.append(access)
        tensors = []
        for root in sorted({a.record["buffer"] for a in accesses}):
            group = [a for a in accesses if a.record["buffer"] == root]
            expressions = [a.index for a in group]
            dimensions, checks = [], []
            for _ in range(4):
                extent = self.dimension(group, expressions)
                if extent is None: break
                divided = [decompose(access, expression, extent) for access, expression in zip(group, expressions)]
                if any(value is None for value in divided): break
                dimensions.append(extent)
                for access, (_, remainder) in zip(group, divided):
                    upper = access.upper(add(remainder, extent, -1))
                    lower = access.upper({m: -c for m, c in remainder.items()})
                    checks.append({"origin": access.record["origin"], "dimension": len(dimensions) - 1,
                                   "upper_bound_proven": upper is not None and upper <= -1,
                                   "nonnegative_proven": lower is not None and lower <= 0})
                expressions = [value[0] for value in divided]
                if not any(expressions): break
            complete = bool(dimensions) and not any(expressions) and all(c["upper_bound_proven"] and c["nonnegative_proven"] for c in checks)
            tensors.append({"buffer": root, "dimensions": [show(d) for d in dimensions], "dimensions_recovered": complete,
                            "accesses": len(group), "coordinate_checks": checks,
                            "remaining_index": [show(e) for e in expressions if e],
                            "integer_interpretation_proven": False})
        return {"analysis": "common equality and inequality fixed point", "fixed_point_verified": True,
                "tensors": tensors, "residuals": residuals, "iterations": self.analysis.iterations,
                "numerical_assumptions": ["MUFU.RCP has at most one ulp error for positive normal operands and results"] if self.analysis.numeric_relation_blocks else [],
                "source_rank_inferred": False, "integer_interpretation_proven": False}
