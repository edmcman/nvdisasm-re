def parts(bits, fraction, bias):
    exponent = (bits >> fraction) & (31 if fraction == 10 else 255)
    return (-1 if bits >> (15 if fraction == 10 else 31) else 1,
            (bits & ((1 << fraction) - 1)) | ((1 << fraction) if exponent else 0),
            max(exponent, 1) - bias)


def block(a, b, c, extra=1):
    terms = []
    infinities = set()
    for x, y in zip(a, b):
        ex, ey = (x >> 10) & 31, (y >> 10) & 31
        if ex == 31 or ey == 31:
            if ex == 31 and x & 1023 or ey == 31 and y & 1023:
                return 0x7fffffff
            if x & 0x7fff == 0 or y & 0x7fff == 0:
                return 0x7fffffff
            infinities.add((x ^ y) >> 15)
            continue
        sx, mx, px = parts(x, 10, 15)
        sy, my, py = parts(y, 10, 15)
        if mx and my: terms.append((sx * sy, mx * my << (3 + extra), px + py))
    ec = c >> 23 & 255
    if ec == 255:
        if c & 0x7fffff: return 0x7fffffff
        infinities.add(c >> 31)
    else:
        sc, mc, pc = parts(c, 23, 127)
        if mc: terms.append((sc, mc << extra, pc))
    if len(infinities) > 1: return 0x7fffffff
    if infinities: return (infinities.pop() << 31) | 0x7f800000
    if not terms: return 0
    exponent = max(e for _, _, e in terms)
    total = sum(s * (m >> (exponent - e)) for s, m, e in terms)
    if not total: return 0
    sign, mantissa = int(total < 0) << 31, abs(total)
    lead = mantissa.bit_length() - 1
    out_exponent = exponent + lead - (23 + extra)
    if out_exponent < -126:
        shift = exponent + 126 - extra
        return sign | (mantissa << shift if shift >= 0 else mantissa >> -shift)
    if out_exponent > 127: return sign | 0x7f800000
    normalized = mantissa >> (lead - 23) if lead >= 23 else mantissa << (23 - lead)
    return sign | ((out_exponent + 127) << 23) | (normalized & 0x7fffff)


def fragment(groups, lane, block_size=8, extra=1):
    out = []
    for j, c in enumerate(groups["Rc"][lane]):
        row, col = lane // 4 + (8 if j >= 2 else 0), lane % 4 * 2 + j % 2
        a = [(groups["Ra"][(row % 8) * 4 + (k % 8) // 2][int(row >= 8) + (2 if k >= 8 else 0)]
              >> (k % 2 * 16)) & 0xffff for k in range(16)]
        b = [(groups["Rb"][col * 4 + (k % 8) // 2][k // 8] >> (k % 2 * 16)) & 0xffff
             for k in range(16)]
        for start in range(0, 16, block_size):
            c = block(a[start:start + block_size], b[start:start + block_size], c, extra)
        out.append(c)
    return out
