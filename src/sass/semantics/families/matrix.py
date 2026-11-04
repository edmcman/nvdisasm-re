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
    if source != "TF32": return packed_product(x, y, const(source == "BF16", 1), extra)
    fraction, shift, sign_bit, exceptional, bias, width, product_shift = 23, 13, 31, 255, 129, 10, 3
    hidden = 1 << (fraction - shift)
    ex, ey = zext(extract(x, sign_bit - 1, fraction), 32), zext(extract(y, sign_bit - 1, fraction), 32)
    fx, fy = zext(extract(x, fraction - 1, shift), 32), zext(extract(y, fraction - 1, shift), 32)
    mx, my = fx | ite(ex == 0, const(0, 32), const(hidden, 32)), fy | ite(ey == 0, const(0, 32), const(hidden, 32))
    sign = extract(x ^ y, sign_bit, sign_bit)
    inf = (ex == exceptional) | (ey == exceptional)
    nan = ((ex == exceptional) & (fx != 0)) | ((ey == exceptional) & (fy != 0)) | (inf & ((mx == 0) | (my == 0)))
    mantissa = (mx * my) << (product_shift + extra)
    exponent = umax(ex, 1) + umax(ey, 1) + bias
    flags = ite(mantissa == 0, const(0, 32), exponent) | (zext(sign, 32) << width)
    flags = flags | (zext(nan, 32) << (width + 1)) | (zext(inf & ~sign, 32) << (width + 2)) | (zext(inf & sign, 32) << (width + 3))
    return concat(flags, mantissa)


@definition(lambda a: (f"hmma.packed.product.e{a['extra']}", ["x", "y", "bf16"]))
def packed_product(x, y, bf16, extra):
    """Encode an unnormalized F16 or BF16 product with a common exponent bias."""
    ex, ey = (ite(bf16, (v >> 7) & 255, (v >> 10) & 31) for v in (x, y))
    fx, fy = (ite(bf16, v & 127, v & 1023) for v in (x, y))
    hidden, exceptional = ite(bf16, const(128, 16), const(1024, 16)), ite(bf16, const(255, 16), const(31, 16))
    mx, my = (zext(f | ite(e == 0, const(0, 16), hidden), 32) for f, e in ((fx, ex), (fy, ey)))
    sign = extract(x ^ y, 15, 15)
    inf = (ex == exceptional) | (ey == exceptional)
    nan = ((ex == exceptional) & (fx != 0)) | ((ey == exceptional) & (fy != 0)) | (inf & ((mx == 0) | (my == 0)))
    mantissa = (mx * my) << ite(bf16, const(9 + extra, 32), const(3 + extra, 32))
    exponent = zext(umax(ex, 1), 32) + zext(umax(ey, 1), 32) + ite(bf16, const(129, 32), const(353, 32))
    flags = ite(mantissa == 0, const(0, 32), exponent) | (zext(sign, 32) << 10)
    flags = flags | (zext(nan, 32) << 11) | (zext(inf & ~sign, 32) << 12) | (zext(inf & sign, 32) << 13)
    return concat(flags, mantissa)


@definition(lambda a: (f"hmma.{a['source'].lower()}.f32.k{len(a['a'])}.e{a['extra']}" +
                      (f".l{-a['minimum_exponent']}" if a["source"] != "F16" else ""), ["a", "b", "c"]))
def tensor_block(a, b, c, extra, source="F16", minimum_exponent=-133):
    """Unnormalized products aligned with the F32 accumulator, truncated and summed once."""
    products = tuple(tensor_product(x, y, extra, source) for x, y in zip(a, b))
    return tensor_sum(products, c, extra, const(-133 if source == "F16" else minimum_exponent, 32))


@definition(lambda a: (f"hmma.f32.k{len(a['products'])}.e{a['extra']}", ["products", "c", "minimum_exponent"]))
def tensor_sum(products, c, extra, minimum_exponent):
    """Align encoded tensor products with C and normalize their truncated sum."""
    width, bias, minimum = 10, 256, 383 + minimum_exponent
    terms = []
    nan = const(0, 1)
    positive, negative = const(0, 1), const(0, 1)
    for product in products:
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
    exponent = minimum
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
    bits = ite(slt(output_exponent, 1), subnormal, (output_exponent << 23) | (normalized & 0x7fffff))
    bits = ite(slt(output_exponent, 255), bits, const(0x7f800000, 32))
    finite = (zext(sign & (bits != 0), 32) << 31) | bits
    return ite(nan | (positive & negative), const(0x7fffffff, 32),
               ite(positive | negative, (zext(negative, 32) << 31) | 0x7f800000,
                   ite(magnitude == 0, const(0, 32), finite)))


@definition(lambda a: (f"hmma168{a['count']}.{a['source'].lower()}.f32.g{a['block']}.e{a['extra']}" +
                      (f".l{-a['minimum_exponent']}" if a["source"] != "F16" else ""), ["a", "b", "c"]))
def tensor_fragment(a, b, c, block, extra, source="F16", count=16, minimum_exponent=-133):
    """Four F32 accumulators updated from two tensor matrix rows and columns."""
    if source != "TF32":
        return packed_fragment(a, b, c, const(source == "BF16", 1), const(minimum_exponent, 32), block, extra)
    result = []
    for j in range(4):
        row = tuple(a[j // 2 * count + k] for k in range(count))
        column = tuple(b[j % 2 * count + k] for k in range(count))
        value = c[j]
        for start in range(0, count, block):
            value = tensor_block(row[start:start + block], column[start:start + block], value, extra, source, minimum_exponent)
        result.append(value)
    return concat(*reversed(result))


@definition(lambda a: (f"hmma16816.packed.f32.g{a['block']}.e{a['extra']}", ["a", "b", "c", "bf16", "minimum_exponent"]))
def packed_fragment(a, b, c, bf16, minimum_exponent, block, extra):
    """Four accumulators updated with packed half or bfloat matrix fragments."""
    result = []
    for j in range(4):
        half = lambda words, offset, k: extract(words[offset + k // 2], k % 2 * 16 + 15, k % 2 * 16)
        row = tuple(half(a, j // 2 * 8, k) for k in range(16))
        column = tuple(half(b, j % 2 * 8, k) for k in range(16))
        value = c[j]
        for start in range(0, 16, block):
            products = tuple(packed_product(row[k], column[k], bf16, extra) for k in range(start, start + block))
            value = tensor_sum(products, value, extra, minimum_exponent)
        result.append(value)
    return concat(*reversed(result))


@family("HMMA", classes=lambda k: k.name == "hmma_x8_" and "srcfmt" in k.operand_types)
def hmma(i):
    size, dst = str(i.select_fork("size")), str(i.select_fork("dstfmt"))
    packed = i.select_in("srcfmt", ("F16", "BF16"))
    if dst != "F32" or (packed and size != "16816"):
        unmodelled(f"HMMA {size}.{dst}: fragment/numerical model unresolved")
    if packed:
        bf16 = i.select_lookup_const("srcfmt", {"F16": 0, "BF16": 1}, w=1)
    elif not i.select_in("srcfmt", ("TF32",)) or size not in ("1684", "1688"):
        unmodelled(f"HMMA {size}.{dst}: fragment/numerical model unresolved")
    current = {name: i.value(name, modifiers=False) for name in ("Ra", "Rb", "Rc")}
    lane = primitive.lane()
    count = int(size[3:])
    block, extra = ((8 if packed else 4), 1) if int(i.arch.name[2:]) < 90 else (count, 2)
    sign = 0x80008000 if packed else 0x80000000
    negation = {name: i.modified(name, "negate", const(0, 32), lambda _: const(sign, 32))
                for name in ("Ra", "Rb")}
    def word(name, owner, n):
        value = primitive.operand_lane(name, owner, n, extract(current[name], n * 32 + 31, n * 32))
        return value ^ negation[name]
    row, column = lane >> 2, (lane & 3) * 2
    if packed:
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
    minimum = -132 if i.arch.name == "SM89" else -133
    result = (packed_fragment(a, b, c, bf16, const(minimum, 32), block, extra) if packed
              else tensor_fragment(a, b, c, block, extra, "TF32", count, minimum))
    return outputs(Rd=result)
