from fractions import Fraction

import z3


class LinearBounds:
    def __init__(self, bounds):
        features = sorted({m for row, _ in bounds for m, _ in row if m}, key=repr)
        self.symbols = {m: z3.Real(f'f{i}') for i, m in enumerate(features)}
        self.solver = z3.Optimize()
        self.solver.add(*(self.expression(dict(row)) <= z3.RealVal(str(upper)) for row, upper in bounds))

    def expression(self, row):
        return z3.Sum([z3.RealVal(str(c)) * (self.symbols[m] if m else 1) for m, c in row.items()])

    def feasible(self):
        result = self.solver.check()
        if result == z3.unknown: raise RuntimeError("linear feasibility solver returned unknown")
        return result == z3.sat

    def upper(self, row):
        if any(m and m not in self.symbols for m in row): return None
        self.solver.push()
        try:
            objective = self.solver.maximize(self.expression(row))
            if not self.feasible(): raise RuntimeError("infeasible constraint state reached bound projection")
            bound = objective.upper()
            return Fraction(str(bound)) if z3.is_rational_value(bound) or z3.is_int_value(bound) else None
        finally: self.solver.pop()
