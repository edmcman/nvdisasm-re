from sass.semantics.fp import softfloat


def float_add(size, old, value):
    match size:
        case "F16x2.RN":
            return sum(float_add("F16.RN", old >> n & 0xffff, value >> n & 0xffff) << n for n in (0, 16))
        case "F16.RN": width, ftz = 16, False
        case "F32.FTZ.RN": width, ftz = 32, True
        case "F64.RN": width, ftz = 64, False
        case _: raise ValueError(size)
    if width == 64:
        for operand in (value, old):
            if softfloat.unpack(operand, width)[0] == "nan": return operand
    result = softfloat.arithmetic("add", (old, value), width, ftz=ftz)
    if softfloat.unpack(result, width)[0] == "nan":
        return {16: 0x7fff, 32: 0x7fffffff, 64: 0xfff8000000000000}[width]
    return result


def update(op, old, value, width=32, signed=False, replacement=0):
    mask = (1 << width) - 1
    old, value, replacement = (x & mask for x in (old, value, replacement))
    number = lambda x: x - (1 << width) if signed and x >> (width - 1) else x
    match op:
        case "ADD": result = old + value
        case "MIN": result = min((old, value), key=number)
        case "MAX": result = max((old, value), key=number)
        case "INC": result = 0 if old >= value else old + 1
        case "DEC": result = value if old == 0 or old > value else old - 1
        case "AND": result = old & value
        case "OR": result = old | value
        case "XOR": result = old ^ value
        case "EXCH": result = value
        case "CAS": result = replacement if old == value else old
        case _: raise ValueError(op)
    return result & mask
