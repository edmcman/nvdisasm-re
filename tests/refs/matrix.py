def load(memory, addresses, lane, matrices=1, transpose=False, offset=0, uniform=None):
    uniform = uniform or [0] * 32
    group, thread = divmod(lane, 4)
    out = []
    for matrix in range(matrices):
        def element(row, column):
            owner = matrix * 8 + row
            address = (addresses[owner] + uniform[owner] + offset) & 0xffffffff
            return int.from_bytes(bytes(memory.get(address + column * 2 + n, 0) for n in range(2)), "little")
        halves = [element(thread * 2 + n, group) if transpose else element(group, thread * 2 + n) for n in range(2)]
        out.append(halves[0] | halves[1] << 16)
    return out


def tensor_block(a, b, c, extra, source="F16", minimum_exponent=-133, negate=(False, False), destination=32):
    from fractions import Fraction
    from sass.semantics.fp import softfloat
    from tests.refs import fpformat
    nan, infinity = (0x7fff, 0x7c00) if destination == 16 else (0x7fffffff, 0x7f800000)
    terms, infinities = [], set()
    width, fraction, bias = {"F16": (16, 10, 15), "BF16": (16, 7, 127),
                             "E6M9": (16, 9, 31), "TF32": (32, 23, 127)}[source]
    def unpack(value):
        match source:
            case "BF16": return softfloat.unpack(value << 16, 32)
            case "E6M9":
                kind, sign, magnitude = fpformat.decode(source, value)
                return ("zero" if magnitude == 0 else kind), sign, (
                    None if magnitude is None else -magnitude if sign else magnitude)
            case _: return softfloat.unpack(value, width)
    for x, y in zip(a, b):
        if source == "TF32": x, y = x & 0xffffe000, y & 0xffffe000
        kx, sx, vx = unpack(x)
        ky, sy, vy = unpack(y)
        if negate[0]:
            sx ^= 1
            if vx is not None: vx = -vx
        if negate[1]:
            sy ^= 1
            if vy is not None: vy = -vy
        if "nan" in (kx, ky) or (kx, ky) in (("zero", "inf"), ("inf", "zero")):
            return nan
        if "inf" in (kx, ky):
            infinities.add(sx ^ sy)
        elif vx and vy:
            mask = (1 << (width - fraction - 1)) - 1
            exponent = max(x >> fraction & mask, 1) + max(y >> fraction & mask, 1) - 2 * bias
            terms.append((vx * vy, exponent))
    kind, sign, value = softfloat.unpack(c, 32)
    match kind:
        case "nan": return nan
        case "inf": infinities.add(sign)
        case "finite": terms.append((value, max(c >> 23 & 255, 1) - 127))
    if len(infinities) > 1: return nan
    if infinities: return next(iter(infinities)) << (destination - 1) | infinity
    if not terms: return 0
    exponent = max(e for _, e in terms)
    if source != "F16": exponent = max(exponent, minimum_exponent)
    quantum = Fraction(2) ** (exponent - 23 - extra)
    aligned = sum((-1 if v < 0 else 1) * (abs(v) // quantum) for v, _ in terms)
    if destination == 16:
        result = softfloat.pack(aligned * quantum, 16)
        return result if result & 0x7fff else 0
    if abs(aligned * quantum) >= Fraction(2) ** 128:
        return (int(aligned < 0) << 31) | 0x7f800000
    result = softfloat.pack(aligned * quantum, 32, "RZ")
    return result if result & 0x7fffffff else 0


def hmma(groups, lane, arch, source="F16", count=16, accumulate=True, negate=(False, False), destination="F32"):
    from sass.semantics.fp import softfloat
    def convert(v, src, dst):
        kind, sign, value = softfloat.unpack(v, src)
        match kind:
            case "nan": return 0x7fff if dst == 16 else 0x7fffffff
            case "inf": return sign << (dst - 1) | (0x7c00 if dst == 16 else 0x7f800000)
            case "zero": return sign << (dst - 1)
            case _: return softfloat.pack(value, dst)
    a, b = [[0] * count for _ in range(16)], [[0] * 8 for _ in range(count)]
    for owner in range(32):
        row, thread = divmod(owner, 4)
        for word, value in enumerate(groups["Ra"][owner]):
            if source != "TF32":
                r, k = row + 8 * (word % 2), thread * 2 + 8 * (word // 2)
                a[r][k:k + 2] = [value & 65535, value >> 16]
            else:
                a[row + 8 * (word % 2)][thread + 4 * (word // 2)] = value
        for word, value in enumerate(groups["Rb"][owner]):
            if source != "TF32":
                for n in (0, 1):
                    b[thread * 2 + 8 * word + n][row] = value >> (n * 16) & 65535
            else:
                b[thread + 4 * word][row] = value
    block, extra = ((4 if source == "TF32" else 8), 1) if int(arch[2:]) < 90 else (count, 2)
    out = []
    accumulators = groups["Rc"][lane]
    if destination == "F16":
        accumulators = [convert(v >> shift & 65535, 16, 32) for v in accumulators for shift in (0, 16)]
    for j, c in enumerate(accumulators):
        if not accumulate: c = 0
        row, column = lane // 4 + 8 * (j // 2), lane % 4 * 2 + j % 2
        for start in range(0, count, block):
            value = tensor_block(a[row][start:start + block],
                             [b[k][column] for k in range(start, start + block)], c, extra, source,
                             -132 if arch == "SM89" else -133, negate, 16 if destination == "F16" else 32)
            c = convert(value, 16, 32) if destination == "F16" else value
        out.append(value)
    return [out[j] | out[j + 1] << 16 for j in (0, 2)] if destination == "F16" else out
