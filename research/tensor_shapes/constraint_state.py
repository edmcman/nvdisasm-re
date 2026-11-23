from dataclasses import dataclass
from fractions import Fraction
from functools import cached_property

from constraint_domain import Equalities, add, clean, order
from linear_bounds import LinearBounds
from polynomial_bounds import PolynomialBounds


def freeze(expression): return tuple(sorted(clean(expression).items(), key=lambda item: order(item[0])))


def canonical(expression, equalities):
    expression = equalities.reduce(expression)
    constant = expression.pop((), 0)
    if not expression: return (), constant, Fraction(1)
    coefficient = abs(expression[min(expression, key=order)])
    return freeze({m: c / coefficient for m, c in expression.items()}), constant, coefficient


@dataclass(frozen=True)
class ConstraintState:
    equalities: Equalities = Equalities()
    bounds: tuple = ()

    @cached_property
    def cached_hash(self): return hash((self.equalities, self.bounds))

    def __hash__(self): return self.cached_hash

    @property
    def rows(self): return self.equalities.rows

    def reduce(self, expression): return self.equalities.reduce(expression)

    def query(self, expression):
        frozen, constant, scale = canonical(expression, self.equalities)
        if not frozen: return constant
        if frozen not in self.queries: self.queries[frozen] = self.bound(frozen)
        bound = self.queries[frozen]
        return None if bound is None else bound * scale + constant

    @cached_property
    def queries(self): return {}

    @cached_property
    def normalized_bounds(self):
        result = {}
        for row, upper in self.bounds:
            key, constant, scale = canonical(dict(row), self.equalities)
            if key: result[key] = min(result.get(key, (upper - constant) / scale), (upper - constant) / scale)
        return tuple(result.items())

    @cached_property
    def linear(self): return LinearBounds(self.normalized_bounds)

    @cached_property
    def polynomial(self): return PolynomialBounds(self.rows, self.bounds)

    def bound(self, frozen):
        row = dict(frozen)
        if not row or set(row) == {()}: return row.get((), Fraction(0))
        return self.linear.upper(row)

    def assume(self, equalities=(), inequalities=()):
        result = Equalities.make([dict(r) for r in self.rows] + list(equalities))
        if result.inconsistent: return None
        bounds = dict(self.bounds)
        for expression, upper in inequalities:
            key = freeze(expression)
            bounds[key] = min(bounds.get(key, upper), upper)
        state = ConstraintState(result, tuple(sorted(bounds.items(), key=repr)))
        if state == self: return self
        for row, upper in state.bounds:
            reduced = result.reduce(dict(row))
            if not reduced and upper < 0 or set(reduced) == {()} and reduced[()] > upper: return None
        return state if state.linear.feasible() else None

    def image(self, assignments, templates):
        result = self.equalities.image(assignments)
        bounds = []
        for template in templates:
            incoming = {}
            for feature, coefficient in template:
                incoming = add(incoming, assignments[feature], coefficient)
            upper = self.query(incoming)
            if upper is not None: bounds.append((template, upper))
        return ConstraintState(result, tuple(sorted(bounds, key=repr)))

    def join(self, other, templates, widening=False, thresholds=(-1, 0, 1)):
        result = self.equalities.join(other.equalities)
        bounds = []
        previous = dict(self.bounds)
        for template in templates:
            expression = dict(template)
            if widening:
                if template not in previous: continue
                right = other.query(expression)
                if right is None: continue
                upper = previous[template] if right <= previous[template] else next((t for t in thresholds if t >= right), None)
                if upper is not None: bounds.append((template, upper))
                continue
            left, right = self.query(expression), other.query(expression)
            if left is None or right is None: continue
            bounds.append((template, max(left, right)))
        return ConstraintState(result, tuple(sorted(bounds, key=repr)))

    def contains(self, other):
        if not self.equalities.contains(other.equalities): return False
        return all((actual := other.query(dict(row))) is not None and actual <= upper
                   or other.polynomial.entails(dict(row), upper) for row, upper in self.bounds)
