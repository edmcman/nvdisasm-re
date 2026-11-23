import z3


class PolynomialBounds:
    def __init__(self, equalities, bounds):
        atoms = sorted({a for row in [*equalities, *(r for r, _ in bounds)] for m, _ in row for a in m}, key=repr)
        self.symbols = {a: z3.Real(f'a{i}') for i, a in enumerate(atoms)}
        self.solver = z3.SolverFor('QF_NRA')
        self.solver.set(timeout=2000)
        self.solver.add(*(self.expression(dict(row)) == 0 for row in equalities))
        self.solver.add(*(self.expression(dict(row)) <= z3.RealVal(str(upper)) for row, upper in bounds))

    def expression(self, row):
        terms = []
        for factors, coefficient in row.items():
            value = z3.RealVal(str(coefficient))
            for atom in factors:
                if atom not in self.symbols: self.symbols[atom] = z3.Real(f'a{len(self.symbols)}')
                value *= self.symbols[atom]
            terms.append(value)
        return z3.Sum(terms)

    def entails(self, row, upper, strict=False):
        self.solver.push()
        try:
            expression, limit = self.expression(row), z3.RealVal(str(upper))
            self.solver.add(expression >= limit if strict else expression > limit)
            return self.solver.check() == z3.unsat
        finally:
            self.solver.pop()
