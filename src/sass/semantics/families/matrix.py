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


@definition("hmma.f16.product.e{extra}")
def tensor_product(x, y, extra):
    """An unnormalized F16 product, its exponent, sign and exceptional-value flags."""
    ex, ey = zext(extract(x, 14, 10), 32), zext(extract(y, 14, 10), 32)
    fx, fy = zext(x & 1023, 32), zext(y & 1023, 32)
    mx, my = fx | ite(ex == 0, const(0, 32), const(1024, 32)), fy | ite(ey == 0, const(0, 32), const(1024, 32))
    sign = extract(x ^ y, 15, 15)
    inf = (ex == 31) | (ey == 31)
    nan = ((ex == 31) & (fx != 0)) | ((ey == 31) & (fy != 0)) | (inf & ((mx == 0) | (my == 0)))
    mantissa = (mx * my) << (3 + extra)
    exponent = umax(ex, 1) + umax(ey, 1) + 97
    flags = ite(mantissa == 0, const(0, 32), exponent) | (zext(sign, 32) << 8)
    flags = flags | (zext(nan, 32) << 9) | (zext(inf & ~sign, 32) << 10) | (zext(inf & sign, 32) << 11)
    return concat(flags, mantissa)


@definition(lambda a: (f"hmma.f16.f32.k{len(a['a'])}.e{a['extra']}", ["a", "b", "c"]))
def tensor_block(a, b, c, extra):
    """Unnormalized F16 products aligned with the F32 accumulator, truncated and summed once."""
    terms = []
    nan = const(0, 1)
    positive, negative = const(0, 1), const(0, 1)
    for x, y in zip(a, b):
        product = tensor_product(x, y, extra)
        flags, mantissa = extract(product, 63, 32), trunc(product, 32)
        nan = nan | extract(flags, 9, 9)
        positive, negative = positive | extract(flags, 10, 10), negative | extract(flags, 11, 11)
        terms.append((mantissa, flags & 255, extract(flags, 8, 8)))
    ec, fc = (c >> 23) & 255, c & 0x7fffff
    sc = extract(c, 31, 31)
    nan = nan | ((ec == 255) & (fc != 0))
    positive, negative = positive | ((ec == 255) & ~sc), negative | ((ec == 255) & sc)
    mc = (fc | ite(ec == 0, const(0, 32), const(0x800000, 32))) << extra
    terms.append((mc, ite(mc == 0, const(0, 32), umax(ec, 1)), sc))
    exponent = const(0, 32)
    for _, e, _ in terms: exponent = umax(exponent, e)
    total = const(0, 64)
    for m, e, sign in terms:
        aligned = zext(m >> (exponent - e), 64)
        total = total + ite(sign, -aligned, aligned)
    sign = slt(total, 0)
    magnitude = trunc(ite(sign, -total, total), 32)
    lead = 31 - op("lzcount", magnitude, w=32)
    output_exponent = exponent + lead - (23 + extra)
    normalized = ite(lead >= 23, magnitude >> (lead - 23), magnitude << (23 - lead))
    subnormal = ite(exponent >= 1 + extra, magnitude << (exponent - 1 - extra),
                    magnitude >> (1 + extra - exponent))
    finite = (zext(sign, 32) << 31) | ite(slt(output_exponent, 1), subnormal,
                                                       (output_exponent << 23) | (normalized & 0x7fffff))
    return ite(nan | (positive & negative), const(0x7fffffff, 32),
               ite(positive | negative, (zext(negative, 32) << 31) | 0x7f800000,
                   ite(magnitude == 0, const(0, 32), finite)))


@definition("hmma16816.f16.f32.g{block}.e{extra}")
def tensor_fragment(a, b, c, block, extra):
    """Four F32 accumulators updated from two F16 matrix rows and columns."""
    result = []
    for j in range(4):
        half = lambda words, offset, k: extract(words[offset + k // 2], k % 2 * 16 + 15, k % 2 * 16)
        row = tuple(half(a, j // 2 * 8, k) for k in range(16))
        column = tuple(half(b, j % 2 * 8, k) for k in range(16))
        value = c[j]
        for start in range(0, 16, block):
            value = tensor_block(row[start:start + block], column[start:start + block], value, extra)
        result.append(value)
    return concat(*reversed(result))


@family("HMMA", classes=lambda k: k.name == "hmma_x8_" and "srcfmt" in k.operand_types)
def hmma(i):
    size, src, dst = str(i.select_fork("size")), str(i.select_fork("srcfmt")), str(i.select_fork("dstfmt"))
    if (size, src, dst) != ("16816", "F16", "F32"):
        unmodelled(f"HMMA {size}.{src}.{dst}: fragment/numerical model unresolved")
    if i.has_operand("UPp") and (not i.is_true("UPp") or i.operand_notted("UPp")):
        unmodelled("HMMA accumulator predicate")
    if i.operand_negated("Ra") or i.operand_negated("Rb"):
        unmodelled("HMMA source negation")
    current = {name: i.value(name, modifiers=False) for name in ("Ra", "Rb", "Rc")}
    lane = primitive.lane()
    block, extra = (8, 1) if int(i.arch.name[2:]) < 90 else (16, 2)
    def word(name, owner, n):
        return primitive.operand_lane(name, owner, n, extract(current[name], n * 32 + 31, n * 32))
    row, column = lane >> 2, (lane & 3) * 2
    a = tuple(word("Ra", row * 4 + k % 8 // 2, r + 2 * (k // 8))
              for r in (0, 1) for k in range(0, 16, 2))
    b = tuple(word("Rb", (column + col) * 4 + k % 8 // 2, k // 8)
              for col in (0, 1) for k in range(0, 16, 2))
    c = tuple(extract(current["Rc"], j * 32 + 31, j * 32) for j in range(4))
    return outputs(Rd=tensor_fragment(a, b, c, block, extra))
