"""F2FP conversions: packs, downconverts and upconverts, plain and UE8M0-scaled, on sass.semantics.fp.fpformat."""
from sass.semantics.fp import fpformat
from sass.semantics.fp.fpformat import FORMATS, ROUNDING
from sass.semantics.bitvec import concat, const, extract, ite, lnot, zext
from sass.semantics.family import family, outputs, unmodelled

FP8, FP6 = ("E4M3", "E5M2"), ("E2M3", "E3M2")
NARROW8 = FP8 + FP6 + ("UE5M3",)


def downconvert(k): return "_to_8b_downconvert__" in k.name


def destination(i):
    """The destination format's constants, chosen by dstfmt: one conversion for every format."""
    param = lambda f, w=32: i.select_lookup_const("dstfmt", {n: f(n) for n in NARROW8 if n in i.labels("dstfmt")}, w)
    return fpformat.Dest(
        mbits=param(lambda n: FORMATS[n].mbits), emin=param(lambda n: FORMATS[n].emin),
        max_code=param(lambda n: FORMATS[n].max_code),
        # A NaN keeps its sign; FP6 has no NaN (SATFINITE: +max); UE5M3's is 0xff.
        nan_signed=param(lambda n: n in FP8, 1), nan_mag=param(lambda n: {"UE5M3": 0xff}.get(n, 0x7f if n in FP8 else 0x1f)),
        # Without SATFINITE, E4M3 overflows to +NaN, E5M2 to signed infinity, UE5M3 to NaN (FP6 requires SATFINITE).
        ovf_signed=param(lambda n: n not in ("E4M3", "UE5M3"), 1),
        ovf_mag=param(lambda n: {"E4M3": 0x7f, "E5M2": 0x7c, "UE5M3": 0xff}.get(n, FORMATS[n].max_code)))


def narrow_byte(i, p, r, general=False):
    """The Parts p rounded to the dstfmt byte: FP8/FP6 with the sign at bit 7, or unsigned UE5M3
    (the sign dropped first, as CUDA's cuda_fp8.hpp does). SATFINITE and RN/RZ/RP per the class;
    RELU zeroes negative results except NaNs; .PZO makes a -0 result +0 (PTX cvt)."""
    d = destination(i)
    sat, relu, pzo = (bit(i, n, on, off) for n, on, off in (("satfinite", "SATFINITE", "nosatfinite"), ("relu", "RELU", "norelu"), ("pzo", "PZO", "nopzo")))
    unsigned = i.select_lookup_const("dstfmt", {n: int(n == "UE5M3") for n in NARROW8 if n in i.labels("dstfmt")}, 1)
    sign, mag, nan = fpformat.round_parts(p._replace(sign=p.sign & lnot(unsigned)), d, r, sat, general)
    sign = sign & lnot(pzo & (extract(mag, 6, 0) == 0))
    return ite(unsigned == 1, extract(mag, 7, 0), ite(relu & sign & lnot(nan), const(0, 8), concat(sign, extract(mag, 6, 0))))


@family("F2FP", "UF2FP", classes=downconvert)
def f2fp_8b_downconvert(i):
    """Two values, Ra and Rb as F32 or Rb's halves as F16/BF16, rounded to FP8 (E4M3/E5M2), FP6
    (E2M3/E3M2) or UE5M3 bytes (narrow_byte) and packed above them with one half of Rc."""
    r = fpformat.chosen_rounding(i, "rndMode", {m: m for m in ROUNDING})
    convert = lambda x, src: narrow_byte(i, fpformat.parts(x, src), r)
    if i.select_fork("merge") not in ("PACK_AB_MERGE_C", "UNPACK_B_MERGE_C"): unmodelled(f"{i.select_fork('merge')} merge")
    src = i.select_fork("srcfmt")
    if "_f16_to_8b_" in i.cls:
        if src not in ("F16", "BF16"): unmodelled(f"{src} source")
        b = i.value("Rb|Sb")
        high, low = convert(extract(b, 31, 16), src), convert(extract(b, 15, 0), src)
    else:
        if src != "F32": unmodelled(f"{src} source")
        high, low = convert(i.value("Ra"), "F32"), convert(i.value("Rb|Sb"), "F32")
    match i.select_fork("extract"):
        case "H0": keep = extract(i.value("Rc|Sc"), 15, 0)
        case "H1": keep = extract(i.value("Rc|Sc"), 31, 16)
        case other: unmodelled(f"{other} extract")
    return outputs(Rd=concat(keep, high, low))


# ---- the other forms ---------------------------------------------------------------------------

PACK16, FP4 = ("F16", "BF16", "E6M9"), ("E2M1", "E0M3")
UP8 = ("E4M3", "E5M2", "E2M3", "E3M2", "E3M4")
S2_6 = fpformat.Format(0, 7, 0, None)   # its magnitude: 7 bits of 2^-6, +-127/64 (two's complement outside)


def form(*prefixes):
    """Classes of these forms, for F2FP and its uniform variant (uf2fp_<form>)."""
    return lambda k: k.name.split("__")[0].removeprefix("u") in prefixes


def required(i, name, *labels):
    label = i.select_fork(name)
    if label not in labels: unmodelled(f"{name} {label}")
    return label


def bit(i, name, on, off):
    """1 when the selector is `on`, 0 when `off`, chosen at decode time (other labels unmodelled)."""
    return i.select_lookup_const(name, {on: 1, off: 0}, 1) if i.has_operand(name) else const(0, 1)


def relu_zero(relu, negative, x):
    """RELU: x becomes +0 when negative (callers exclude NaNs where those stay)."""
    return ite(relu & negative, const(0, x.w), x)


def half(i, names, selector):
    """One half of a register, chosen by an H0/H1 selector."""
    v = i.value(names, modifiers=False)
    return i.select_lookup(i.resolve(selector), {"H0": extract(v, 15, 0), "H1": extract(v, 31, 16)})


@family("F2FP", "UF2FP", classes=form("f2fp_tf32"))
def f2fp_tf32(i):
    """Rd = Rb (F32) keeping 10 fraction bits: RN-even or RZ on the 13 discarded bits, Inf/NaN
    truncated, any NaN giving 0x7fffe000 (also with RELU); RELU makes negative values +0 (SM89 GPU)."""
    required(i, "dstfmt", "TF32"); required(i, "srcfmt", "F32"); required(i, "merge", "PACK_B")
    x = i.value("Rb|Sb", modifiers=False)
    rn = i.select_lookup_const("rndMode", {"RN": 1, "RZ": 0}, 1)
    special = extract(x, 30, 23) == 0xff
    r = ite(rn & lnot(special), (x + 0xfff + zext(extract(x, 13, 13), 32)) & 0xffffe000, x & 0xffffe000)
    nan = extract(x, 30, 23) == 0xff
    nan = nan & (extract(x, 22, 0) != 0)
    relu = bit(i, "relu", "RELU", "norelu")
    return outputs(Rd=ite(nan, const(0x7fffe000, 32), relu_zero(relu, extract(r, 31, 31) == 1, r)))


def pack16_destination(i):
    """A NaN packs to +0x7fff in every format (SM89 GPU); overflow is signed infinity.
    SM86-SM90 UF2FP name the formats together (dfmt.sfmt, only F16.F32)."""
    if i.has_operand("dfmt.sfmt"):
        required(i, "dfmt.sfmt", "F16.F32")
        param = lambda f, w=32: f("F16")
    else:
        required(i, "srcfmt", "F32")
        param = lambda f, w=32: i.select_lookup_const("dstfmt", {n: f(n) for n in PACK16}, w)
    return fpformat.Dest(
        mbits=param(lambda n: FORMATS[n].mbits), emin=param(lambda n: FORMATS[n].emin),
        max_code=param(lambda n: FORMATS[n].max_code),
        nan_signed=param(lambda n: False, 1), nan_mag=param(lambda n: 0x7fff),
        ovf_signed=1, ovf_mag=param(lambda n: FORMATS[n].inf_code))


@family("F2FP", "UF2FP", classes=form("f2fp", "f2fp_merge_c"))
def f2fp_pack16(i):
    """F32 values rounded (RN, RZ) to F16/BF16/E6M9 halves: Rd = Ra' << 16 | Rb', or MERGE_C:
    Rc's H0/H1 half << 16 | Rb'. SATFINITE: infinities become the largest finite value; RELU zeroes
    negative results except NaNs."""
    merge_c = "merge_c" in i.cls
    if merge_c and not i.has_operand("extract"): unmodelled("immediate C without extract: which half is kept")
    required(i, "merge", "MERGE_C" if merge_c else "PACK_AB")
    d = pack16_destination(i)
    r = fpformat.chosen_rounding(i, "rndMode", {"RN": "RN", "RZ": "RZ"})
    sat, relu = bit(i, "satfinite", "SATFINITE", "nosatfinite"), bit(i, "relu", "RELU", "norelu")

    def convert(x):
        sign, mag, nan = fpformat.narrow_parts(x, "F32", d, r, sat)
        return relu_zero(relu, sign & lnot(nan), concat(sign, extract(mag, 14, 0)))

    low = convert(i.value("Rb|Sb", modifiers=False))
    high = half(i, "Rc|Sc", "extract") if merge_c else convert(i.value("Ra", modifiers=False))
    return outputs(Rd=concat(high, low))


def fp4_destination(i):
    """E2M1/E0M3, SATFINITE: NaN gives +max (7)."""
    param = lambda f: i.select_lookup_const("dstfmt", {n: f(n) for n in FP4})
    return fpformat.Dest(mbits=param(lambda n: FORMATS[n].mbits), emin=param(lambda n: FORMATS[n].emin),
                         max_code=7, nan_signed=0, nan_mag=7, ovf_signed=1, ovf_mag=7)


@family("F2FP", "UF2FP", classes=form("f2fp_f32_to_4b_downconvert", "f2fp_f16_to_4b_downconvert"))
def f2fp_4b_downconvert(i):
    """Values rounded (RN, SATFINITE) to E2M1/E0M3 nibbles. F32: Rd = Rc[31:8] << 8 | Ra' << 4 | Rb';
    F16/BF16: Rd[15:0] the four halves of Rc (high) and Rb (low). RELU zeroes negative results; from F32
    also negative NaNs, from F16/BF16 a NaN stays +max."""
    required(i, "rndMode", "RN"); required(i, "satfinite", "SATFINITE")
    f32 = "_f32_to_" in i.cls
    required(i, "merge", "PACK_AB_MERGE_C" if f32 else "UNPACK_B_MERGE_C")
    d, rn = fp4_destination(i), fpformat.rounding("RN")
    relu = bit(i, "relu", "RELU", "norelu")

    def convert(x, src):
        sign, mag, nan = fpformat.narrow_parts(x, src, d, rn, 1)
        negative = extract(x, x.w - 1, x.w - 1) == 1 if f32 else sign & lnot(nan)
        return relu_zero(relu, negative, concat(sign, extract(mag, 2, 0)))

    if f32:
        required(i, "srcfmt", "F32")
        c = i.value("Rc|Sc", modifiers=False)
        return outputs(Rd=concat(extract(c, 31, 8), convert(i.value("Ra", modifiers=False), "F32"),
                                 convert(i.value("Rb|Sb", modifiers=False), "F32")))
    src = str(required(i, "srcfmt", "F16", "BF16"))
    b, c = (i.value(n, modifiers=False) for n in ("Rb|Sb", "Rc|Sc"))
    return outputs(Rd=zext(concat(*(convert(extract(v, h + 15, h), src) for v in (c, b) for h in (16, 0))), 32))


def scaled_pair(i, convert, one_scale):
    """Rd = Rc[15:0] << 16 | convert(A) << 8 | convert(B): F32 Ra and Rb, or Rb's F16/BF16 halves
    (high first), each divided by its UE8M0 scale (0xff: NaN). The scales are Rc's high half, where
    ptxas 13.4 puts PTX cvt's scale-factor (`IMAD.U32 Rc, s, 0x10000`; one scale:
    `IMAD.SHL.U32 Rc, s, 0x1000000`): Rc[31:24] for A and Rc[23:16] for B (.scaled::n2), or Rc[31:24]
    for both (.scaled::n1, SCALE_BY_C). Rc[15:0] as MERGE_C's half (the unscaled forms' default H0)
    is inferred: ptxas always passes zero there."""
    f32 = "_f32_to_" in i.cls
    required(i, "merge", "PACK_AB_MERGE_C" if f32 else "UNPACK_B_MERGE_C")
    src = str(required(i, "srcfmt", *(("F32",) if f32 else ("F16", "BF16"))))
    c = i.value("Rc|Sc", modifiers=False)

    def scaled(x, scale):
        p = fpformat.parts(x, src, normalized=True).scaled(const(127, 32) - zext(scale, 32))
        return convert(p._replace(nan=p.nan | (scale == 0xff)))

    if f32: a, b = i.value("Ra", modifiers=False), i.value("Rb|Sb", modifiers=False)
    else:
        v = i.value("Rb|Sb", modifiers=False); a, b = extract(v, 31, 16), extract(v, 15, 0)
    scale_b = extract(c, 31, 24) if one_scale else extract(c, 23, 16)
    return outputs(Rd=concat(extract(c, 15, 0), scaled(a, extract(c, 31, 24)), scaled(b, scale_b)))


@family("F2FP", "UF2FP", classes=form("f2fp_f32_to_mx8_downconvert_scale", "f2fp_f16_to_mx8_downconvert_scale"))
def f2fp_s2_6_downconvert(i):
    """Two scaled values (scaled_pair, a scale each) rounded (RN, SATFINITE) to S2_6 fixed point,
    two's complement. NaN gives +127/64; RELU zeroes negative results; .PZO changes nothing (no -0)."""
    required(i, "dstfmt", "S2_6"); required(i, "rndMode", "RN"); required(i, "satnarrow", "SATFINITE")
    d = fpformat.Dest(S2_6.mbits, S2_6.emin, 127, 0, 127, 1, 127)
    relu = bit(i, "relu", "RELU", "norelu")
    bit(i, "pzo", "PZO", "nopzo")

    def convert(p):
        sign, mag, nan = fpformat.round_parts(p, d, fpformat.rounding("RN"), 1, general=True)
        return relu_zero(relu, sign & lnot(nan), extract(ite(sign == 1, -mag, mag), 7, 0))
    return scaled_pair(i, convert, one_scale=False)


@family("F2FP", "UF2FP", classes=form("f2fp_f32_to_8b_downconvert_scale", "f2fp_f16_to_8b_downconvert_scale"))
def f2fp_8b_downconvert_scaled(i):
    """SM107 (PTX cvt .scaled::n1::ue8m0): two values divided by one scale (scaled_pair) and
    rounded (RN, RZ) to FP8/FP6/UE5M3 bytes as the unscaled downconvert does (narrow_byte)."""
    required(i, "scalec", "SCALE_BY_C")
    r = fpformat.chosen_rounding(i, "rndMode", {"RN": "RN", "RZ": "RZ"})
    return scaled_pair(i, lambda p: narrow_byte(i, p, r, general=True), one_scale=True)


@family("F2FP", "UF2FP", classes=form("f2fp_E8_upconvert"))
def f2fp_e8_upconvert(i):
    """Rb's H0/H1 half as two UE8M0 bytes to BF16: byte << 7, 0xff gives NaN (0x7fff), 0 the
    subnormal 2^-127 (0x0040)."""
    required(i, "dstfmt", "BF16"); required(i, "srcfmt", "E8"); required(i, "merge", "UNPACK_B"); required(i, "rndMode", "RN")
    h = half(i, "Rb|Sb", "extract_B|extract")
    lane = lambda e: ite(e == 0xff, const(0x7fff, 16), ite(e == 0, const(0x40, 16), zext(e, 16) << 7))
    return outputs(Rd=concat(lane(extract(h, 15, 8)), lane(extract(h, 7, 0))))


def source_parts(i, fmts, byte):
    """The Parts of a source byte in the format srcfmt chooses: each component a decode-time choice."""
    def of(f):
        if f == "S2_6": return fpformat.fixed_parts(byte, 6)
        return fpformat.parts(extract(byte, FORMATS[f].width - 1, 0), f, normalized=True)
    each = {f: of(f) for f in fmts if f in i.labels("srcfmt")}
    return fpformat.Parts(*(i.select_lookup("srcfmt", {f: p[k] for f, p in each.items()}) for k in range(6)))


def scale_pair(i):
    """The two UE8M0 scales of a scaled upconvert, the low lane's in the low byte (PTX cvt
    .scaled::n2::ue8m0). ptxas puts the PTX scale-factor at Rc.H0, independent of the source half
    (F2FP.BF16.E4M3.UNPACK_B R5, R0.H1, R0.H0), and never encodes another iswzC: H1 and B0-B3 are
    unmodelled (research/semantics-migration/f2fp_scaled_ptx.md)."""
    if i.select_fork("iswzC") != "H0": unmodelled("iswzC other than H0")
    return extract(i.value("Rc|Sc", modifiers=False), 15, 0)


def upconvert(i, fmts, codes, dst, scales=None):
    """Each source code exactly in dst (F16), or times its UE8M0 scale (0xff: NaN) rounded RN to
    BF16, SATFINITE optional; NaN gives 0x7fff. RELU zeroes negative results except NaNs."""
    relu = bit(i, "relu", "RELU", "norelu")
    sat = bit(i, "satfinite", "SATFINITE", "nosatfinite") if scales is not None else 0
    d, f = fpformat.dest(dst, nan=(False, 0x7fff)), FORMATS[dst]
    lanes = []
    for k, code in enumerate(codes):
        if scales is None:   # each format's own exact widening, chosen at decode time
            x = i.select_lookup("srcfmt", {s: fpformat.widen(extract(code, FORMATS[s].width - 1, 0), s, dst, nan=(False, 0x7fff))
                                           for s in fmts if s in i.labels("srcfmt")})
            lanes.append(relu_zero(relu, extract(x, f.width - 1, f.width - 1) == 1, x))   # NaN is +0x7fff
            continue
        p = source_parts(i, fmts, code)
        scale = extract(scales, 8 * k + 7, 8 * k)
        p = p.scaled(zext(scale, 32) - 127)._replace(nan=p.nan | (scale == 0xff))
        sign, mag, _ = fpformat.round_parts(p, d, fpformat.rounding("RN"), sat, general=True)
        lanes.append(relu_zero(relu, sign & lnot(p.nan), concat(sign, extract(mag, f.width - 2, 0))))
    return outputs(Rd=concat(lanes[1], lanes[0]))


@family("F2FP", "UF2FP", classes=form("f2fp_8b_upconvert", "f2fp_8b_upconvert_scale"))
def f2fp_8b_upconvert(i):
    """Rb's H0/H1 half as two FP8/FP6 (or, scaled, S2_6) bytes: exactly to F16, or times the
    UE8M0 scales in Rc[15:0] and rounded to BF16."""
    scaled = "_scale" in i.cls
    required(i, "dstfmt", "BF16" if scaled else "F16"); required(i, "merge", "UNPACK_B"); required(i, "rndMode", "RN")
    h = half(i, "Rb|Sb", "extract_B|extract")
    codes = [extract(h, 7, 0), extract(h, 15, 8)]
    if not scaled: return upconvert(i, UP8, codes, "F16")
    return upconvert(i, UP8 + ("S2_6",), codes, "BF16", scale_pair(i))


@family("F2FP", "UF2FP", classes=form("f2fp_4b_upconvert", "f2fp_4b_upconvert_scale"))
def f2fp_4b_upconvert(i):
    """Rb's selB byte as two E2M1/E0M3 nibbles: exactly to F16, or times the UE8M0 scales in
    Rc[15:0] and rounded to BF16 (SATFINITE)."""
    scaled = "_scale" in i.cls
    required(i, "dstfmt", "BF16" if scaled else "F16"); required(i, "merge", "UNPACK_B"); required(i, "rndMode", "RN")
    if scaled: required(i, "satfinite", "SATFINITE")
    b = i.value("Rb|Sb", modifiers=False)
    byte = i.select_lookup("selB", {f"B{n}": extract(b, 8 * n + 7, 8 * n) for n in range(4)})
    codes = [zext(extract(byte, 3, 0), 8), zext(extract(byte, 7, 4), 8)]
    if not scaled: return upconvert(i, FP4, codes, "F16")
    return upconvert(i, FP4, codes, "BF16", scale_pair(i))
