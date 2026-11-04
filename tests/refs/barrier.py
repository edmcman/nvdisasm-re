def reduce(op, values):
    match op:
        case "POPC": return sum(bool(value) for value in values)
        case "AND": return 0xffffffff if all(values) else 0
        case "OR": return 0xffffffff if any(values) else 0
        case _: raise ValueError(op)
