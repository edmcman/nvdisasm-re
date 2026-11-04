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


def tensor_block(a, b, c, extra):
    from fractions import Fraction
    from sass.semantics.fp import softfloat
    terms, infinities = [], set()
    for x, y in zip(a, b):
        kx, sx, vx = softfloat.unpack(x, 16)
        ky, sy, vy = softfloat.unpack(y, 16)
        if "nan" in (kx, ky) or (kx, ky) in (("zero", "inf"), ("inf", "zero")):
            return 0x7fffffff
        if "inf" in (kx, ky):
            infinities.add(sx ^ sy)
        elif vx and vy:
            exponent = max(x >> 10 & 31, 1) + max(y >> 10 & 31, 1) - 30
            terms.append((vx * vy, exponent))
    kind, sign, value = softfloat.unpack(c, 32)
    match kind:
        case "nan": return 0x7fffffff
        case "inf": infinities.add(sign)
        case "finite": terms.append((value, max(c >> 23 & 255, 1) - 127))
    if len(infinities) > 1: return 0x7fffffff
    if infinities: return next(iter(infinities)) << 31 | 0x7f800000
    if not terms: return 0
    quantum = Fraction(2) ** (max(e for _, e in terms) - 23 - extra)
    aligned = sum((-1 if v < 0 else 1) * (abs(v) // quantum) for v, _ in terms)
    return softfloat.pack(aligned * quantum, 32, "RZ")


def hmma(groups, lane, arch):
    a, b = [[0] * 16 for _ in range(16)], [[0] * 8 for _ in range(16)]
    for owner in range(32):
        row, thread = divmod(owner, 4)
        for word, value in enumerate(groups["Ra"][owner]):
            r, k = row + 8 * (word % 2), thread * 2 + 8 * (word // 2)
            a[r][k:k + 2] = [value & 65535, value >> 16]
        for word, value in enumerate(groups["Rb"][owner]):
            for n in (0, 1):
                b[thread * 2 + 8 * word + n][row] = value >> (n * 16) & 65535
    block, extra = (8, 1) if int(arch[2:]) < 90 else (16, 2)
    out = []
    for j, c in enumerate(groups["Rc"][lane]):
        row, column = lane // 4 + 8 * (j // 2), lane % 4 * 2 + j % 2
        for start in range(0, 16, block):
            c = tensor_block(a[row][start:start + block],
                             [b[k][column] for k in range(start, start + block)], c, extra)
        out.append(c)
    return out
