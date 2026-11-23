from dataclasses import dataclass
from fractions import Fraction

from linear_algebra import nullspace


def order(feature):
    dynamic = any(a.operation in ("variable", "nondeterministic") for a in feature)
    wide = any(a.operation == "variable" and len(a.arguments) == 3 and a.arguments[1] - a.arguments[2] == 63 for a in feature)
    iteration = any(a.operation == "iteration" for a in feature)
    return (0 if wide else 1 if dynamic else 2 if iteration else 3 if feature else 4), -len(feature), repr(feature)


def clean(expression): return {m: Fraction(c) for m, c in expression.items() if c}


def add(left, right, scale=1):
    result = dict(left)
    for feature, coefficient in right.items(): result[feature] = result.get(feature, 0) + scale * coefficient
    return clean(result)


def multiply(left, right, degree=4):
    result = {}
    for a, x in left.items():
        for b, y in right.items():
            feature = tuple(sorted(a + b, key=repr))
            if len(feature) > degree: raise ValueError("polynomial feature exceeds the domain degree")
            result[feature] = result.get(feature, 0) + x * y
    return clean(result)


def rref(rows, key=order):
    basis = {}
    for expression in rows:
        row = clean(expression)
        for pivot in sorted(basis, key=key):
            if pivot in row: row = add(row, basis[pivot], -row[pivot])
        if not row: continue
        pivot = min(row, key=key)
        coefficient = row[pivot]
        row = {m: c / coefficient for m, c in row.items()}
        for old in list(basis):
            if pivot in basis[old]: basis[old] = add(basis[old], row, -basis[old][pivot])
        basis[pivot] = row
    return tuple(tuple(sorted(basis[p].items(), key=lambda x: key(x[0]))) for p in sorted(basis, key=key))


@dataclass(frozen=True)
class Equalities:
    rows: tuple = ()

    @staticmethod
    def make(rows, key=order): return Equalities(rref(rows, key))

    def reduce(self, expression):
        result = clean(expression)
        for frozen in self.rows:
            row = dict(frozen)
            pivot = frozen[0][0]
            if pivot in result: result = add(result, row, -result[pivot])
        return result

    def assume(self, expression): return Equalities.make([dict(r) for r in self.rows] + [expression])

    @property
    def inconsistent(self): return any(len(row) == 1 and row[0][0] == () for row in self.rows)

    def image(self, assignments):
        columns = list(assignments)
        reduced = [self.reduce(assignments[c]) for c in columns]
        features = sorted(set().union(*(r.keys() for r in reduced)), key=order)
        matrix = [[r.get(f, 0) for r in reduced] for f in features]
        return Equalities.make([{c: v for c, v in zip(columns, vector) if v} for vector in nullspace(matrix, len(columns))])

    def join(self, other):
        if self == other: return self
        if not self.rows or not other.rows: return Equalities()
        left, right = [dict(r) for r in self.rows], [dict(r) for r in other.rows]
        features = sorted(set().union(*(r.keys() for r in left + right)), key=order)
        matrix = [[r.get(f, 0) for r in left] + [-r.get(f, 0) for r in right] for f in features]
        result = []
        for vector in nullspace(matrix, len(left) + len(right)):
            row = {}
            for coefficient, source in zip(vector, left): row = add(row, source, coefficient)
            result.append(row)
        return Equalities.make(result)

    def contains(self, other): return all(not other.reduce(dict(row)) for row in self.rows)
