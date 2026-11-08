from sass.semantics import ordered, primitive
from sass.semantics.bitvec import concat, const, extract, ite, op, slt, trunc, umax, zext
from sass.semantics.family import definition, family, unmodelled
from sass.semantics.families import convert
from sass.semantics.fp import fpformat


PACKED_BITS = {source: fpformat.FORMATS[source].mbits for source in ("F16", "BF16", "E6M9")}


@definition(lambda a: (f"ldmatrix.{'trans' if a['transpose'] else 'row'}.element", ["lane", "matrix", "n"]))
def element(lane, matrix, n, transpose):
    """The address-owning lane and halfword column of one matrix fragment element."""
    group, thread = lane >> 2, lane & 3
    row, column = (thread * 2 + n, group) if transpose else (group, thread * 2 + n)
    return concat(matrix * 8 + row, column)


def capture_operand(i, body, lane, active, name):
    source = body.capture(i.value(name, modifiers=False))
    snapshot, = body.special(ordered.CAPTURE_WORDS[source.w],
                             const(primitive.OPERAND_GROUPS.index(name), 32), lane, active, source)
    return snapshot


def snapshot_word(body, snapshot, lane, word):
    value, = body.special(ordered.READ_WORD, snapshot, lane, const(word, 32))
    return value


def matrix_word(body, lane, matrix, offset, current, uniform, transpose):
    def half(n):
        position = element(lane, matrix, const(n, 32), transpose)
        owner, column = extract(position, 63, 32), extract(position, 31, 0)
        address = snapshot_word(body, current, owner, 0) + offset
        if uniform is not None: address = address + snapshot_word(body, uniform, owner, 0)
        return body.read_memory("shared", zext(address, 64) + zext(column * 2, 64), 16)
    return concat(half(1), half(0))


@family("LDSM")
def ldsm(i):
    size, mode, num = str(i.select_fork("sz")), str(i.select_fork("mode")), str(i.select_fork("num"))
    if size != "16" or mode not in ("M88", "MT88"):
        unmodelled(f"matrix load {size}.{mode}: packed/expanded mapping unresolved")
    if num not in ("1", "2", "4"): unmodelled(f"matrix count {num}")
    body = i.body()
    lane, = body.special(ordered.LANE)
    active = i.active_lanes(body)
    current = capture_operand(i, body, lane, active, "Ra")
    uniform = capture_operand(i, body, lane, active, "Ra_URb") if i.has_operand("Ra_URb") else None
    offset = i.imm("Ra_offset")
    words = [matrix_word(body, lane, const(j, 32), offset, current, uniform, mode == "MT88")
             for j in range(int(num))]
    for snapshot in (current, uniform):
        if snapshot is not None: body.special(ordered.RELEASE_WORDS, snapshot)
    return i.assign_from_inputs(body, Rd=concat(*reversed(words)) if len(words) > 1 else words[0])


@definition(lambda a: (f"hmma.{a['source'].lower()}.product.e{a['extra']}", ["x", "y"]))
def tensor_product(x, y, extra, source="F16"):
    """An unnormalized tensor product, its exponent, sign and exceptional-value flags."""
    if source != "TF32": return packed_product(x, y, const(PACKED_BITS[source], 32), extra)
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


@definition(lambda a: (f"hmma.packed.product.e{a['extra']}", ["x", "y", "mbits"]))
def packed_product(x, y, mbits, extra):
    """Encode an unnormalized packed IEEE product with a common exponent bias."""
    hidden, exceptional = const(1, 16) << mbits, const(0x7fff, 16) >> mbits
    ex, ey = ((v & 0x7fff) >> mbits for v in (x, y))
    fx, fy = (v & (hidden - 1) for v in (x, y))
    mx, my = (zext(f | ite(e == 0, const(0, 16), hidden), 32) for f, e in ((fx, ex), (fy, ey)))
    sign = extract(x ^ y, 15, 15)
    inf = (ex == exceptional) | (ey == exceptional)
    nan = ((ex == exceptional) & (fx != 0)) | ((ey == exceptional) & (fy != 0)) | (inf & ((mx == 0) | (my == 0)))
    mantissa = (mx * my) << (23 + extra - 2 * mbits)
    bias = (const(1, 32) << (14 - mbits)) - 1
    exponent = zext(umax(ex, 1), 32) + zext(umax(ey, 1), 32) + 383 - 2 * bias
    flags = ite(mantissa == 0, const(0, 32), exponent) | (zext(sign, 32) << 10)
    flags = flags | (zext(nan, 32) << 11) | (zext(inf & ~sign, 32) << 12) | (zext(inf & sign, 32) << 13)
    return concat(flags, mantissa)


@definition(lambda a: (f"hmma.{a['source'].lower()}.f32.k{len(a['a'])}.e{a['extra']}" +
                      (f".l{-a['minimum_exponent']}" if a["source"] != "F16" else ""), ["a", "b", "c"]))
def tensor_block(a, b, c, extra, source="F16", minimum_exponent=-133):
    """Unnormalized products aligned with the F32 accumulator, truncated and summed once."""
    products = tuple(tensor_product(x, y, extra, source) for x, y in zip(a, b))
    return tensor_sum(products, c, extra, const(-133 if source == "F16" else minimum_exponent, 32))


@definition(lambda a: (f"hmma.{a['destination'].lower()}.k{len(a['products'])}.e{a['extra']}", ["products", "c", "minimum_exponent"]))
def tensor_sum(products, c, extra, minimum_exponent, destination="F32"):
    """Align tensor products with C; normalize RZ to F32 or RN-even to F16."""
    return tensor_normalize(tensor_align(products, c, extra, minimum_exponent), extra, destination)


@definition(lambda a: (f"hmma.align.k{len(a['products'])}.e{a['extra']}", ["products", "c", "minimum_exponent"]))
def tensor_align(products, c, extra, minimum_exponent):
    """The signed magnitude, exponent and exceptional flags of an aligned tensor sum."""
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
    flags = zext(sign, 32) | (zext(nan, 32) << 1) | (zext(positive, 32) << 2) | (zext(negative, 32) << 3)
    return concat(exponent | (flags << 10), magnitude)


@definition(lambda a: (f"hmma.normalize.{a['destination'].lower()}.e{a['extra']}", ["aligned"]))
def tensor_normalize(aligned, extra, destination):
    """Round an aligned tensor sum to F32 with RZ or F16 with RN-even."""
    encoded, magnitude = extract(aligned, 63, 32), trunc(aligned, 32)
    flags, exponent = encoded >> 10, encoded & 1023
    sign, nan, positive, negative = (extract(flags, j, j) for j in range(4))
    bias = 256
    lead = 31 - op("lzcount", magnitude, w=32)
    output_exponent = exponent + lead - (23 + extra + bias)
    if destination == "F16":
        parts = fpformat.Parts(ite(positive | negative, negative, sign), magnitude,
                               exponent - (23 + extra + bias + 127), output_exponent - 127,
                               nan | (positive & negative), positive | negative)
        result_sign, code, _ = fpformat.round_parts(parts, fpformat.dest("F16"), fpformat.rounding("RN"), general=True)
        return concat(result_sign & (code != 0), extract(code, 14, 0))
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
        return packed_fragment(a, b, c, const(PACKED_BITS[source], 32), const(minimum_exponent, 32), block, extra)
    result = []
    for j in range(4):
        row = tuple(a[j // 2 * count + k] for k in range(count))
        column = tuple(b[j % 2 * count + k] for k in range(count))
        value = c[j]
        for start in range(0, count, block):
            value = tensor_block(row[start:start + block], column[start:start + block], value, extra, source, minimum_exponent)
        result.append(value)
    return concat(*reversed(result))


@definition(lambda a: (f"hmma16816.packed.f32.g{a['block']}.e{a['extra']}", ["a", "b", "c", "mbits", "minimum_exponent"]))
def packed_fragment(a, b, c, mbits, minimum_exponent, block, extra):
    """Four accumulators updated with packed 16-bit matrix fragments."""
    return packed_accumulate(a, b, c, mbits, minimum_exponent, block, extra)


@definition(lambda a: (f"hmma.packed.stage.k{len(a['a'])}.e{a['extra']}", ["a", "b", "c", "mbits", "minimum_exponent", "half_output"]))
def packed_stage(a, b, c, mbits, minimum_exponent, half_output, extra):
    """Four tensor results in 32-bit cells, with optional half rounding."""
    result = []
    count = len(a)
    for j in range(4):
        row = a[j // 2 * count // 2:(j // 2 + 1) * count // 2]
        column = b[j % 2 * count // 2:(j % 2 + 1) * count // 2]
        aligned = packed_dot(row, column, c[j], mbits, minimum_exponent, extra)
        result.append(ite(half_output, zext(tensor_normalize(aligned, extra, "F16"), 32),
                          tensor_normalize(aligned, extra, "F32")))
    return concat(*reversed(result))


@definition(lambda a: (f"hmma.packed.dot.k{len(a['a']) * 2}.e{a['extra']}", ["a", "b", "c", "mbits", "minimum_exponent"]))
def packed_dot(a, b, c, mbits, minimum_exponent, extra):
    """One aligned tensor dot product from packed 16-bit source words."""
    products = tuple(packed_product(extract(x, shift + 15, shift), extract(y, shift + 15, shift), mbits, extra)
                     for x, y in zip(a, b) for shift in (0, 16))
    return tensor_align(products, c, extra, minimum_exponent)


def packed_accumulate(a, b, c, mbits, minimum_exponent, block, extra, half_output=False):
    for start in range(0, 16, block):
        def segment(words):
            return tuple(words[r * 8 + start // 2 + k] for r in range(2) for k in range(block // 2))
        stage = packed_stage(segment(a), segment(b), c, mbits, minimum_exponent, const(half_output, 1), extra)
        c = (tuple(half_accumulator(extract(stage, j * 32 + 15, j * 32)) for j in range(4))
             if half_output else tuple(extract(stage, j * 32 + 31, j * 32) for j in range(4)))
    return half_result(stage) if half_output else stage


def half_result(stage):
    return concat(*(extract(stage, j * 32 + 15, j * 32) for j in reversed(range(4))))


@definition(lambda a: (f"hmma16816.f16.g{a['block']}.e{a['extra']}", ["a", "b", "c"]))
def half_fragment(a, b, c, block, extra):
    """Four packed F16 accumulators rounded RN-even after every tensor stage."""
    return packed_accumulate(a, b, half_accumulators(c), const(PACKED_BITS["F16"], 32), const(-133, 32), block, extra, True)


def half_accumulators(c):
    return tuple(half_accumulator(extract(c[j // 2], j % 2 * 16 + 15, j % 2 * 16)) for j in range(4))


@definition("hmma.f16.accumulator")
def half_accumulator(value):
    """Expand an F16 tensor accumulator without changing its value."""
    return convert.converted("F16", "F32", *fpformat.rounding("RN"), value)


@family("HMMA", classes=lambda k: k.name == "hmma_x8_" and "srcfmt" in k.operand_types)
def hmma(i):
    size, dst = str(i.select_fork("size")), str(i.select_fork("dstfmt"))
    packed = i.select_in("srcfmt", tuple(PACKED_BITS))
    if dst not in ("F16", "F32") or (packed and size not in ("1688", "16816")):
        unmodelled(f"HMMA {size}.{dst}: fragment/numerical model unresolved")
    if packed:
        mbits = i.select_lookup_const("srcfmt", PACKED_BITS, w=32)
    elif not i.select_in("srcfmt", ("TF32",)) or size not in ("1684", "1688"):
        unmodelled(f"HMMA {size}.{dst}: fragment/numerical model unresolved")
    body = i.body()
    lane, = body.special(ordered.LANE)
    active = i.active_lanes(body)
    snapshots = {name: capture_operand(i, body, lane, active, name) for name in ("Ra", "Rb")}
    current = body.capture(i.value("Rc", modifiers=False))
    count = int(size[3:])
    block, extra = ((8 if packed else 4), 1) if int(i.arch.name[2:]) < 90 else (count, 2)
    sign = 0x80008000 if packed else 0x80000000
    negation = {name: i.modified(name, "negate", const(0, 32), lambda _: const(sign, 32))
                for name in ("Ra", "Rb")}
    words = {}
    def word(name, owner, n):
        key = name, owner, n
        if key not in words: words[key] = snapshot_word(body, snapshots[name], owner, n)
        return words[key] ^ negation[name]
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
    for snapshot in snapshots.values(): body.special(ordered.RELEASE_WORDS, snapshot)
    enabled = body.capture(i.pred("UPp")) if i.has_operand("UPp") else const(1, 1)
    c = tuple(ite(enabled, extract(current, j * 32 + 31, j * 32), const(0, 32))
              for j in range(2 if dst == "F16" else 4))
    minimum = -132 if i.arch.name == "SM89" else -133
    if packed and count == 8:
        stage = packed_stage(a, b, half_accumulators(c) if dst == "F16" else c,
                             mbits, const(minimum, 32), const(dst == "F16", 1), extra)
        return i.assign_from_inputs(body, Rd=half_result(stage) if dst == "F16" else stage)
    result = (half_fragment(a, b, c, block, extra) if dst == "F16" else
              packed_fragment(a, b, c, mbits, const(minimum, 32), block, extra) if packed
              else tensor_fragment(a, b, c, block, extra, "TF32", count, minimum))
    return i.assign_from_inputs(body, Rd=result)
