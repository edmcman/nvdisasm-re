from fractions import Fraction
from functools import reduce
from math import gcd, lcm


def nullspace(rows, columns):
    matrix = [[Fraction(x) for x in row] for row in rows if any(row)]
    pivots, rank = [], 0
    for column in range(columns):
        position = next((i for i in range(rank, len(matrix)) if matrix[i][column]), None)
        if position is None: continue
        matrix[rank], matrix[position] = matrix[position], matrix[rank]
        coefficient = matrix[rank][column]
        matrix[rank] = [x / coefficient for x in matrix[rank]]
        for i in range(len(matrix)):
            if i == rank or not matrix[i][column]: continue
            factor = matrix[i][column]
            matrix[i] = [a - factor * b for a, b in zip(matrix[i], matrix[rank])]
        pivots.append(column)
        rank += 1
    result = []
    for free in sorted(set(range(columns)) - set(pivots)):
        vector = [Fraction(0)] * columns
        vector[free] = Fraction(1)
        for row, pivot in enumerate(pivots): vector[pivot] = -matrix[row][free]
        denominator = lcm(*(v.denominator for v in vector))
        integers = [int(v * denominator) for v in vector]
        divisor = reduce(gcd, integers)
        result.append([v // divisor for v in integers])
    return result
