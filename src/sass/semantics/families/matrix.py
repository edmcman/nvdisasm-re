from sass.semantics import primitive
from sass.semantics.bitvec import concat, const, extract, ite, load, op, slt, trunc, umax, zext
from sass.semantics.family import definition, family, outputs, unmodelled


@definition(lambda a: (f"ldmatrix.{'trans' if a['transpose'] else 'row'}.element", ["lane", "matrix", "n"]))
def element(lane, matrix, n, transpose):
    """The address-owning lane and halfword column of one matrix fragment element."""
    group, thread = lane >> 2, lane & 3
    row, column = (thread * 2 + n, group) if transpose else (group, thread * 2 + n)
    return concat(matrix * 8 + row, column)


def matrix_word(lane, matrix, offset, current, uniform, transpose):
    def half(n):
        position = element(lane, matrix, const(n, 32), transpose)
        owner, column = extract(position, 63, 32), extract(position, 31, 0)
        address = primitive.operand_lane("Ra", owner, 0, current) + offset
        if uniform is not None: address = address + primitive.operand_lane("Ra_URb", owner, 0, uniform)
        return load("shared", zext(address, 64) + zext(column * 2, 64), 16)
    return concat(half(1), half(0))


@family("LDSM")
def ldsm(i):
    size, mode, num = str(i.select_fork("sz")), str(i.select_fork("mode")), str(i.select_fork("num"))
    if size != "16" or mode not in ("M88", "MT88"):
        unmodelled(f"matrix load {size}.{mode}: packed/expanded mapping unresolved")
    if num not in ("1", "2", "4"): unmodelled(f"matrix count {num}")
    current = i.value("Ra", modifiers=False)
    uniform = i.value("Ra_URb", modifiers=False) if i.has_operand("Ra_URb") else None
    offset = i.imm("Ra_offset")
    words = [matrix_word(primitive.lane(), const(j, 32), offset, current, uniform, mode == "MT88")
             for j in range(int(num))]
    return outputs(Rd=concat(*reversed(words)) if len(words) > 1 else words[0])


@definition(lambda a: (f"hmma.{a['source'].lower()}.product.e{a['extra']}", ["x", "y"]))
def tensor_product(x, y, extra, source="F16"):
    """An unnormalized tensor product, its exponent, sign and exceptional-value flags."""
    fraction, shift, sign_bit, exceptional, bias, width = (10, 0, 15, 31, 97, 8) if source == "F16" else (23, 13, 31, 255, 129, 10)
    ex, ey = zext(extract(x, sign_bit - 1, fraction), 32), zext(extract(y, sign_bit - 1, fraction), 32)
    fx, fy = zext(extract(x, fraction - 1, shift), 32), zext(extract(y, fraction - 1, shift), 32)
    mx, my = fx | ite(ex == 0, const(0, 32), const(1024, 32)), fy | ite(ey == 0, const(0, 32), const(1024, 32))
    sign = extract(x ^ y, sign_bit, sign_bit)
    inf = (ex == exceptional) | (ey == exceptional)
    nan = ((ex == exceptional) & (fx != 0)) | ((ey == exceptional) & (fy != 0)) | (inf & ((mx == 0) | (my == 0)))
    mantissa = (mx * my) << (3 + extra)
    exponent = umax(ex, 1) + umax(ey, 1) + bias
    flags = ite(mantissa == 0, const(0, 32), exponent) | (zext(sign, 32) << width)
    flags = flags | (zext(nan, 32) << (width + 1)) | (zext(inf & ~sign, 32) << (width + 2)) | (zext(inf & sign, 32) << (width + 3))
    return concat(flags, mantissa)


@definition(lambda a: (f"hmma.{a['source'].lower()}.f32.k{len(a['a'])}.e{a['extra']}" +
                      (f".l{-a['minimum_exponent']}" if a["source"] == "TF32" else ""), ["a", "b", "c"]))
def tensor_block(a, b, c, extra, source="F16", minimum_exponent=-133):
    """Unnormalized products aligned with the F32 accumulator, truncated and summed once."""
    width, bias, minimum = (8, 0, 0) if source == "F16" else (10, 256, 383 + minimum_exponent)
    terms = []
    nan = const(0, 1)
    positive, negative = const(0, 1), const(0, 1)
    for x, y in zip(a, b):
        product = tensor_product(x, y, extra, source)
        flags, mantissa = extract(product, 63, 32), trunc(product, 32)
        nan = nan | extract(flags, width + 1, width + 1)
        positive, negative = positive | extract(flags, width + 2, width + 2), negative | extract(flags, width + 3, width + 3)
        terms.append((mantissa, flags & ((1 << width) - 1), extract(flags, width, width)))
    ec, fc = (c >> 23) & 255, c & 0x7fffff
    sc = extract(c, 31, 31)
    nan = nan | ((ec == 255) & (fc != 0))
    positive, negative = positive | ((ec == 255) & ~sc), negative | ((ec == 255) & sc)
    mc = (fc | ite(ec == 0, const(0, 32), const(0x800000, 32))) << extra
    terms.append((mc, ite(mc == 0, const(0, 32), umax(ec, 1) + bias), sc))
    exponent = const(minimum, 32)
    for _, e, _ in terms: exponent = umax(exponent, e)
    total = const(0, 64)
    for m, e, sign in terms:
        aligned = zext(m >> (exponent - e), 64)
        total = total + ite(sign, -aligned, aligned)
    sign = slt(total, 0)
    magnitude = trunc(ite(sign, -total, total), 32)
    lead = 31 - op("lzcount", magnitude, w=32)
    output_exponent = exponent + lead - (23 + extra + bias)
    normalized = ite(lead >= 23, magnitude >> (lead - 23), magnitude << (23 - lead))
    subnormal = ite(exponent >= 1 + extra + bias, magnitude << (exponent - 1 - extra - bias),
                    magnitude >> (1 + extra + bias - exponent))
    finite = (zext(sign, 32) << 31) | ite(slt(output_exponent, 1), subnormal,
                                                       (output_exponent << 23) | (normalized & 0x7fffff))
    if source == "TF32":
        finite = ite(slt(output_exponent, 255), finite, (zext(sign, 32) << 31) | 0x7f800000)
        finite = ite((finite & 0x7fffffff) == 0, const(0, 32), finite)
    return ite(nan | (positive & negative), const(0x7fffffff, 32),
               ite(positive | negative, (zext(negative, 32) << 31) | 0x7f800000,
                   ite(magnitude == 0, const(0, 32), finite)))


@definition(lambda a: (f"hmma168{a['count']}.{a['source'].lower()}.f32.g{a['block']}.e{a['extra']}" +
                      (f".l{-a['minimum_exponent']}" if a["source"] == "TF32" else ""), ["a", "b", "c"]))
def tensor_fragment(a, b, c, block, extra, source="F16", count=16, minimum_exponent=-133):
    """Four F32 accumulators updated from two tensor matrix rows and columns."""
    result = []
    for j in range(4):
        half = lambda words, offset, k: extract(words[offset + k // 2], k % 2 * 16 + 15, k % 2 * 16)
        element = half if source == "F16" else lambda words, offset, k: words[offset + k]
        span = count // 2 if source == "F16" else count
        row = tuple(element(a, j // 2 * span, k) for k in range(count))
        column = tuple(element(b, j % 2 * span, k) for k in range(count))
        value = c[j]
        for start in range(0, count, block):
            value = tensor_block(row[start:start + block], column[start:start + block], value, extra, source, minimum_exponent)
        result.append(value)
    return concat(*reversed(result))


@family("HMMA", classes=lambda k: k.name == "hmma_x8_" and "srcfmt" in k.operand_types)
def hmma(i):
    size, src, dst = str(i.select_fork("size")), str(i.select_fork("srcfmt")), str(i.select_fork("dstfmt"))
    if (size, src, dst) not in (("16816", "F16", "F32"), ("1684", "TF32", "F32"), ("1688", "TF32", "F32")):
        unmodelled(f"HMMA {size}.{src}.{dst}: fragment/numerical model unresolved")
    current = {name: i.value(name, modifiers=False) for name in ("Ra", "Rb", "Rc")}
    lane = primitive.lane()
    count = int(size[3:])
    block, extra = ((8 if src == "F16" else 4), 1) if int(i.arch.name[2:]) < 90 else (count, 2)
    sign = 0x80008000 if src == "F16" else 0x80000000
    negation = {name: i.modified(name, "negate", const(0, 32), lambda _: const(sign, 32))
                for name in ("Ra", "Rb")}
    def word(name, owner, n):
        value = primitive.operand_lane(name, owner, n, extract(current[name], n * 32 + 31, n * 32))
        return value ^ negation[name]
    row, column = lane >> 2, (lane & 3) * 2
    if src == "F16":
        a = tuple(word("Ra", row * 4 + k % 8 // 2, r + 2 * (k // 8))
                  for r in (0, 1) for k in range(0, count, 2))
        b = tuple(word("Rb", (column + col) * 4 + k % 8 // 2, k // 8)
                  for col in (0, 1) for k in range(0, count, 2))
    else:
        a = tuple(word("Ra", row * 4 + k % 4, r + 2 * (k // 4))
                  for r in (0, 1) for k in range(count))
        b = tuple(word("Rb", (column + col) * 4 + k % 4, k // 4)
                  for col in (0, 1) for k in range(count))
    enabled = i.pred("UPp") if i.has_operand("UPp") else const(1, 1)
    c = tuple(ite(enabled, extract(current["Rc"], j * 32 + 31, j * 32), const(0, 32)) for j in range(4))
    minimum = -132 if src == "TF32" and i.arch.name == "SM89" else -133
    return outputs(Rd=tensor_fragment(a, b, c, block, extra, src, count, minimum))
