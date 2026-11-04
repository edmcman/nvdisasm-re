def reduce(op, values):
    match op:
        case "POPC": return sum(bool(value) for value in values)
        case "AND": return 0xffffffff if all(values) else 0
        case "OR": return 0xffffffff if any(values) else 0
        case _: raise ValueError(op)


def warp(result):
    return sum((bool(result & (1 << bit)) << bit) for bit in range(11))


def pending(op, expected, arrived, values=None):
    if arrived == expected: return 0
    tag = {"SYNC": 8, "ARV": 8, "POPC": 9, "AND": 10, "OR": 11}[op]
    values = [True] * (arrived * 32) if values is None else values
    total = sum(not bool(value) if op == "AND" else bool(value) for value in values)
    return tag * 2 ** 28 + expected * 2 ** 20 + total * 2 ** 8 + arrived
