"""F2FP conversions: packs, downconverts and upconverts, plain and UE8M0-scaled, on sass.semantics.fp.fpformat."""
from sass.semantics.fp import fpformat
from sass.semantics.fp.fpformat import FORMATS, ROUNDING
from sass.semantics.bitvec import chosen, concat, const, extract, is_const, ite, lnot, value, zext
from sass.semantics.family import definition, family, outputs, same, unmodelled

FP8, FP6 = ("E4M3", "E5M2"), ("E2M3", "E3M2")
NARROW8 = FP8 + FP6 + ("UE5M3",)


def downconvert(k): return "_to_8b_downconvert__" in k.name


def byte_dest(n):
    """The Dest of a dstfmt byte format, as numbers. A NaN keeps its sign; FP6 has no NaN
    (SATFINITE: +max); UE5M3's is 0xff. Without SATFINITE, E4M3 overflows to +NaN, E5M2 to signed
    infinity, UE5M3 to NaN (FP6 requires SATFINITE)."""
    return fpformat.Dest(mbits=FORMATS[n].mbits, emin=FORMATS[n].emin, max_code=FORMATS[n].max_code,
                         nan_signed=int(n in FP8), nan_mag={"UE5M3": 0xff}.get(n, 0x7f if n in FP8 else 0x1f),
                         ovf_signed=int(n not in ("E4M3", "UE5M3")),
                         ovf_mag={"E4M3": 0x7f, "E5M2": 0x7c, "UE5M3": 0xff}.get(n, FORMATS[n].max_code))


def destination(i):
    """The destination format's constants, chosen by dstfmt: one conversion for every format."""
    labels = [n for n in NARROW8 if n in i.labels("dstfmt")]
    return fpformat.Dest(*(i.select_lookup_const("dstfmt", {n: byte_dest(n)[k] for n in labels}, 1 if k in (3, 5) else 32) for k in range(7)))


def byte_settings(i):
    """(Dest, SATFINITE, RELU, PZO, unsigned) of the dstfmt byte, decode-time choices."""
    d = destination(i)
    sat, relu, pzo = (bit(i, n, on, off) for n, on, off in (("satfinite", "SATFINITE", "nosatfinite"), ("relu", "RELU", "norelu"), ("pzo", "PZO", "nopzo")))
    unsigned = i.select_lookup_const("dstfmt", {n: int(n == "UE5M3") for n in NARROW8 if n in i.labels("dstfmt")}, 1)
    return d, sat, relu, pzo, unsigned


def byte_code(p, d, r, sat, relu, pzo, unsigned, general):
    """The Parts p rounded to the dstfmt byte: FP8/FP6 with the sign at bit 7, or unsigned UE5M3
    (the sign dropped first, as CUDA's cuda_fp8.hpp does). SATFINITE and RN/RZ/RP per the class;
    RELU zeroes negative results except NaNs; .PZO makes a -0 result +0 (PTX cvt)."""
    sign, mag, nan = fpformat.round_parts(p._replace(sign=p.sign & lnot(unsigned)), d, r, sat, general)
    sign = sign & lnot(pzo & (extract(mag, 6, 0) == 0))
    return ite(unsigned == 1, extract(mag, 7, 0), ite(relu & sign & lnot(nan), const(0, 8), concat(sign, extract(mag, 6, 0))))


def flags(**bits):
    """The .<name> suffixes of constant flags that are set; None if one isn't constant."""
    values = {n: fpformat.constant(b) for n, b in bits.items()}
    return None if None in values.values() else "".join(f".{n}" for n, v in values.items() if v)


def byte_name(scaled):
    """A namer: f2fp.cvt.<mode>[.satfinite][.relu][.pzo].<format>.<src>[.scaled] for constant
    choices (a dstfmt byte format's Dest), else f2fp.cvt.byte.<src>[.scaled] of them too."""
    def name(a):
        fmt, m, mods = fpformat.format_of(a["d"]), fpformat.mode(a["r"]), flags(satfinite=a["sat"], relu=a["relu"], pzo=a["pzo"])
        sources = ["x", *(["scale"] if scaled else [])]
        suffix = f".{a['src'].lower()}{'.scaled' if scaled else ''}"
        if fmt in NARROW8 and m and mods is not None and all(map(same, a["d"], byte_dest(fmt))) and same(a["unsigned"], int(fmt == "UE5M3")):
            return f"f2fp.cvt.{m.lower()}{mods}.{fmt.lower()}{suffix}", sources
        return f"f2fp.cvt.byte{suffix}", ["d", "r", "sat", "relu", "pzo", "unsigned", *sources]
    return name


@definition(byte_name(False))
def f2fp_byte(d, r, sat, relu, pzo, unsigned, x, src):
    """PTX cvt from F32/F16/BF16 to an FP8 (E4M3, E5M2), FP6 (E2M3, E3M2) or UE5M3 byte with F2FP's
    rules: rounded once, the format's NaN and overflow results, SATFINITE to the largest finite
    value, RELU zeroing negative results except NaNs, .PZO making -0 +0."""
    return byte_code(fpformat.parts(x, src), d, r, sat, relu, pzo, unsigned, False)


def scaled_parts(x, scale, src):
    """The Parts of x divided by its UE8M0 scale (0xff: NaN)."""
    p = fpformat.parts(x, src, normalized=True).scaled(const(127, 32) - zext(scale, 32))
    return p._replace(nan=p.nan | (scale == 0xff))


@definition(byte_name(True))
def f2fp_byte_scaled(d, r, sat, relu, pzo, unsigned, x, scale, src):
    """PTX cvt .scaled::ue8m0 to an FP8/FP6/UE5M3 byte: x divided by its UE8M0 scale (0xff: NaN),
    then as f2fp.cvt to a byte."""
    return byte_code(scaled_parts(x, scale, src), d, r, sat, relu, pzo, unsigned, True)


@family("F2FP", "UF2FP", classes=downconvert)
def f2fp_8b_downconvert(i):
    """Two values, Ra and Rb as F32 or Rb's halves as F16/BF16, rounded to FP8 (E4M3/E5M2), FP6
    (E2M3/E3M2) or UE5M3 bytes (narrow_byte) and packed above them with one half of Rc."""
    r = fpformat.chosen_rounding(i, "rndMode", {m: m for m in ROUNDING})

    def convert(x, src):
        d, sat, relu, pzo, unsigned = byte_settings(i)
        return f2fp_byte(d, r, sat, relu, pzo, unsigned, x, str(src))
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


def tf32_name(a):
    rn, mods = fpformat.constant(a["rn"]), flags(relu=a["relu"])
    if rn is None or mods is None: return "f2fp.cvt.tf32.f32", ["rn", "relu", "x"]
    return f"f2fp.cvt.{'rn' if rn else 'rz'}{mods}.tf32.f32", ["x"]


@definition(tf32_name)
def to_tf32(rn, relu, x):
    """PTX cvt to TF32 with F2FP's rules: x keeping 10 fraction bits, rounded to nearest even or
    toward zero on the 13 discarded bits, Inf/NaN truncated, any NaN giving 0x7fffe000 (also with
    RELU); RELU makes negative values +0 (SM89 GPU)."""
    special = extract(x, 30, 23) == 0xff
    r = ite(rn & lnot(special), (x + 0xfff + zext(extract(x, 13, 13), 32)) & 0xffffe000, x & 0xffffe000)
    nan = extract(x, 30, 23) == 0xff
    nan = nan & (extract(x, 22, 0) != 0)
    return ite(nan, const(0x7fffe000, 32), relu_zero(relu, extract(r, 31, 31) == 1, r))


@family("F2FP", "UF2FP", classes=form("f2fp_tf32"))
def f2fp_tf32(i):
    """Rd = Rb (F32) keeping 10 fraction bits: RN-even or RZ on the 13 discarded bits, Inf/NaN
    truncated, any NaN giving 0x7fffe000 (also with RELU); RELU makes negative values +0 (SM89 GPU)."""
    required(i, "dstfmt", "TF32"); required(i, "srcfmt", "F32"); required(i, "merge", "PACK_B")
    x = i.value("Rb|Sb", modifiers=False)
    rn = i.select_lookup_const("rndMode", {"RN": 1, "RZ": 0}, 1)
    return outputs(Rd=to_tf32(rn, bit(i, "relu", "RELU", "norelu"), x))


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


def pack16_name(kind, lanes):
    """A namer: f2fp.<kind>.<mode>[.satfinite][.relu].<format>.f32 when the destination, rounding
    and flags are constants (the destination one of pack16_destination's), else f2fp.<kind> of
    every field."""
    def name(a):
        sat, relu = fpformat.constant(a["sat"]), fpformat.constant(a["relu"])
        fmt, m = fpformat.format_of(a["d"]), fpformat.mode(a["r"])
        if None in (fmt, m, sat, relu) or [fpformat.constant(x) for x in a["d"][3:]] != [0, 0x7fff, 1, FORMATS[fmt].inf_code]:
            return f"f2fp.{kind}", ["d", "r", "sat", "relu", *lanes]
        flags = [".satfinite"] * bool(sat) + [".relu"] * bool(relu)
        return f"f2fp.{kind}.{m.lower()}{''.join(flags)}.{fmt.lower()}{'x2' if len(lanes) > 1 else ''}.f32", lanes
    return name


@definition(pack16_name("cvt", ["x"]))
def pack16_lane(d, r, sat, relu, x):
    """F2FP's F32 to F16/BF16/E6M9 lane (PTX cvt): rounded with the GPU's NaN (+0x7fff);
    SATFINITE makes infinities the largest finite value, RELU makes negative results +0 (NaN stays)."""
    sign, mag, nan = fpformat.narrow_parts(x, "F32", d, r, sat)
    return relu_zero(relu, sign & lnot(nan), concat(sign, extract(mag, 14, 0)))


@definition(pack16_name("pack", ["a", "b"]))
def pack16(d, r, sat, relu, a, b):
    """Two F32 values as F2FP lanes (f2fp.cvt), the first in the high half (PTX cvt to f16x2/bf16x2)."""
    return concat(pack16_lane(d, r, sat, relu, a), pack16_lane(d, r, sat, relu, b))


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
    b = i.value("Rb|Sb", modifiers=False)
    if merge_c: return outputs(Rd=concat(half(i, "Rc|Sc", "extract"), pack16_lane(d, r, sat, relu, b)))
    return outputs(Rd=pack16(d, r, sat, relu, i.value("Ra", modifiers=False), b))


def fp4_destination(i):
    """E2M1/E0M3, SATFINITE: NaN gives +max (7)."""
    param = lambda f: i.select_lookup_const("dstfmt", {n: f(n) for n in FP4})
    return fpformat.Dest(mbits=param(lambda n: FORMATS[n].mbits), emin=param(lambda n: FORMATS[n].emin),
                         max_code=7, nan_signed=0, nan_mag=7, ovf_signed=1, ovf_mag=7)


FP4_DEST = {n: fpformat.Dest(FORMATS[n].mbits, FORMATS[n].emin, 7, 0, 7, 1, 7) for n in FP4}


def nibble_name(a):
    fmt, mods = fpformat.format_of(a["d"]), flags(relu=a["relu"])
    if fmt in FP4 and mods is not None and all(map(same, a["d"], FP4_DEST[fmt])):
        return f"f2fp.cvt.rn.satfinite{mods}.{fmt.lower()}.{a['src'].lower()}", ["x"]
    return f"f2fp.cvt.nibble.{a['src'].lower()}", ["d", "relu", "x"]


@definition(nibble_name)
def f2fp_nibble(d, relu, x, src):
    """PTX cvt.rn.satfinite to an E2M1/E0M3 nibble with F2FP's rules: NaN gives +max (7); RELU
    zeroes negative results, from F32 also negative NaNs, from F16/BF16 a NaN stays +max."""
    sign, mag, nan = fpformat.narrow_parts(x, src, d, fpformat.rounding("RN"), 1)
    negative = extract(x, x.w - 1, x.w - 1) == 1 if src == "F32" else sign & lnot(nan)
    return relu_zero(relu, negative, concat(sign, extract(mag, 2, 0)))


@family("F2FP", "UF2FP", classes=form("f2fp_f32_to_4b_downconvert", "f2fp_f16_to_4b_downconvert"))
def f2fp_4b_downconvert(i):
    """Values rounded (RN, SATFINITE) to E2M1/E0M3 nibbles. F32: Rd = Rc[31:8] << 8 | Ra' << 4 | Rb';
    F16/BF16: Rd[15:0] the four halves of Rc (high) and Rb (low). RELU zeroes negative results; from F32
    also negative NaNs, from F16/BF16 a NaN stays +max."""
    required(i, "rndMode", "RN"); required(i, "satfinite", "SATFINITE")
    f32 = "_f32_to_" in i.cls
    required(i, "merge", "PACK_AB_MERGE_C" if f32 else "UNPACK_B_MERGE_C")
    d = fp4_destination(i)
    relu = bit(i, "relu", "RELU", "norelu")
    convert = lambda x, src: f2fp_nibble(d, relu, x, str(src))

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

    scaled = lambda x, scale: convert(x, scale, src)

    if f32: a, b = i.value("Ra", modifiers=False), i.value("Rb|Sb", modifiers=False)
    else:
        v = i.value("Rb|Sb", modifiers=False); a, b = extract(v, 31, 16), extract(v, 15, 0)
    scale_b = extract(c, 31, 24) if one_scale else extract(c, 23, 16)
    return outputs(Rd=concat(extract(c, 15, 0), scaled(a, extract(c, 31, 24)), scaled(b, scale_b)))


def s2_6_name(a):
    mods = flags(relu=a["relu"])
    if mods is None: return f"f2fp.cvt.s2_6.{a['src'].lower()}.scaled", ["relu", "x", "scale"]
    return f"f2fp.cvt.rn.satfinite{mods}.s2_6.{a['src'].lower()}.scaled", ["x", "scale"]


@definition(s2_6_name)
def to_s2_6(relu, x, scale, src):
    """PTX cvt .scaled::ue8m0 to S2_6 fixed point: x divided by its UE8M0 scale (0xff: NaN),
    rounded to nearest even and saturated, two's complement; NaN gives +127/64, RELU zeroes
    negative results."""
    d = fpformat.Dest(S2_6.mbits, S2_6.emin, 127, 0, 127, 1, 127)
    sign, mag, nan = fpformat.round_parts(scaled_parts(x, scale, src), d, fpformat.rounding("RN"), 1, general=True)
    return relu_zero(relu, sign & lnot(nan), extract(ite(sign == 1, -mag, mag), 7, 0))


@family("F2FP", "UF2FP", classes=form("f2fp_f32_to_mx8_downconvert_scale", "f2fp_f16_to_mx8_downconvert_scale"))
def f2fp_s2_6_downconvert(i):
    """Two scaled values (scaled_pair, a scale each) rounded (RN, SATFINITE) to S2_6 fixed point,
    two's complement. NaN gives +127/64; RELU zeroes negative results; .PZO changes nothing (no -0)."""
    required(i, "dstfmt", "S2_6"); required(i, "rndMode", "RN"); required(i, "satnarrow", "SATFINITE")
    relu = bit(i, "relu", "RELU", "norelu")
    bit(i, "pzo", "PZO", "nopzo")
    return scaled_pair(i, lambda x, scale, src: to_s2_6(relu, x, scale, src), one_scale=False)


@family("F2FP", "UF2FP", classes=form("f2fp_f32_to_8b_downconvert_scale", "f2fp_f16_to_8b_downconvert_scale"))
def f2fp_8b_downconvert_scaled(i):
    """SM107 (PTX cvt .scaled::n1::ue8m0): two values divided by one scale (scaled_pair) and
    rounded (RN, RZ) to FP8/FP6/UE5M3 bytes as the unscaled downconvert does (narrow_byte)."""
    required(i, "scalec", "SCALE_BY_C")
    r = fpformat.chosen_rounding(i, "rndMode", {"RN": "RN", "RZ": "RZ"})

    def convert(x, scale, src):
        d, sat, relu, pzo, unsigned = byte_settings(i)
        return f2fp_byte_scaled(d, r, sat, relu, pzo, unsigned, x, scale, src)
    return scaled_pair(i, convert, one_scale=True)


@definition("cvt.rn.bf16x2.ue8m0x2")
def from_ue8m0(h):
    """PTX cvt.rn.bf16x2.ue8m0x2: two UE8M0 bytes to BF16, byte << 7, 0xff giving NaN (0x7fff) and 0
    the subnormal 2^-127 (0x0040)."""
    lane = lambda e: ite(e == 0xff, const(0x7fff, 16), ite(e == 0, const(0x40, 16), zext(e, 16) << 7))
    return concat(lane(extract(h, 15, 8)), lane(extract(h, 7, 0)))


@family("F2FP", "UF2FP", classes=form("f2fp_E8_upconvert"))
def f2fp_e8_upconvert(i):
    """Rb's H0/H1 half as two UE8M0 bytes to BF16: byte << 7, 0xff gives NaN (0x7fff), 0 the
    subnormal 2^-127 (0x0040)."""
    required(i, "dstfmt", "BF16"); required(i, "srcfmt", "E8"); required(i, "merge", "UNPACK_B"); required(i, "rndMode", "RN")
    return outputs(Rd=from_ue8m0(half(i, "Rb|Sb", "extract_B|extract")))


UPCONVERTS = {"8b": UP8, "4b": FP4, "8b.scaled": UP8 + ("S2_6",)}   # the formats srcfmt codes index


def source_parts(fmt, byte):
    """The Parts of a source byte in format fmt."""
    if fmt == "S2_6": return fpformat.fixed_parts(byte, 6)
    return fpformat.parts(extract(byte, FORMATS[fmt].width - 1, 0), fmt, normalized=True)


def upconvert_name(scaled):
    """A namer: f2fp.cvt[.rn][.satfinite][.relu].<f16|bf16>.<format>[.scaled] for constant choices,
    else f2fp.cvt.<f16|bf16>.<kind> of them too."""
    def name(a):
        mods = flags(satfinite=a["sat"], relu=a["relu"]) if scaled else flags(relu=a["relu"])
        if is_const(a["code"]) and mods is not None:
            fmt = UPCONVERTS[a["kind"]][value(a["code"])].lower()
            return (f"f2fp.cvt.rn{mods}.bf16.{fmt}.scaled", ["byte", "scale"]) if scaled else (f"f2fp.cvt{mods}.f16.{fmt}", ["byte"])
        return (f"f2fp.cvt.bf16.{a['kind']}", ["code", "relu", "sat", "byte", "scale"]) if scaled else (f"f2fp.cvt.f16.{a['kind']}", ["code", "relu", "byte"])
    return name


@definition(upconvert_name(False))
def upconverted(code, relu, byte, kind):
    """PTX cvt from an FP8/FP6/FP4 code to F16, exactly; NaN gives 0x7fff, RELU zeroes negative
    results except NaNs.

    code: the source format's index in UPCONVERTS[kind] (a decode-time choice in SLEIGH)."""
    fmts = UPCONVERTS[kind]
    x = chosen(code, lambda k: fpformat.widen(extract(byte, FORMATS[fmts[k]].width - 1, 0), fmts[k], "F16", nan=(False, 0x7fff)))
    return relu_zero(relu, extract(x, 15, 15) == 1, x)   # NaN is +0x7fff


@definition(upconvert_name(True))
def upconverted_scaled(code, relu, sat, byte, scale, kind):
    """PTX cvt .scaled::ue8m0 from an FP8/FP6/FP4/S2_6 code to BF16: times its UE8M0 scale (0xff:
    NaN), rounded to nearest even, SATFINITE optional; NaN gives 0x7fff, RELU zeroes negative results
    except NaNs.

    code: the source format's index in UPCONVERTS[kind] (a decode-time choice in SLEIGH)."""
    fmts, f = UPCONVERTS[kind], FORMATS["BF16"]
    p = fpformat.Parts(*(chosen(code, lambda k: source_parts(fmts[k], byte)[field]) for field in range(6)))
    p = p.scaled(zext(scale, 32) - 127)._replace(nan=p.nan | (scale == 0xff))
    sign, mag, _ = fpformat.round_parts(p, fpformat.dest("BF16", nan=(False, 0x7fff)), fpformat.rounding("RN"), sat, general=True)
    return relu_zero(relu, sign & lnot(p.nan), concat(sign, extract(mag, f.width - 2, 0)))


def scale_pair(i):
    """The two UE8M0 scales of a scaled upconvert, the low lane's in the low byte (PTX cvt
    .scaled::n2::ue8m0). ptxas puts the PTX scale-factor at Rc.H0, independent of the source half
    (F2FP.BF16.E4M3.UNPACK_B R5, R0.H1, R0.H0), and never encodes another iswzC: H1 and B0-B3 are
    unmodelled (research/semantics-migration/f2fp_scaled_ptx.md)."""
    if i.select_fork("iswzC") != "H0": unmodelled("iswzC other than H0")
    return extract(i.value("Rc|Sc", modifiers=False), 15, 0)


def upconvert(i, kind, codes, scales=None):
    """Each source code exactly in F16, or times its UE8M0 scale rounded to BF16 (upconverted,
    upconverted_scaled); the source format a decode-time choice of srcfmt."""
    relu = bit(i, "relu", "RELU", "norelu")
    sat = bit(i, "satfinite", "SATFINITE", "nosatfinite") if scales is not None else 0
    lanes = []
    for k, byte in enumerate(codes):
        code = i.select_lookup_const("srcfmt", {s: n for n, s in enumerate(UPCONVERTS[kind]) if s in i.labels("srcfmt")}, 8)
        if scales is None: lanes.append(upconverted(code, relu, byte, kind))
        else: lanes.append(upconverted_scaled(code, relu, sat, byte, extract(scales, 8 * k + 7, 8 * k), kind))
    return outputs(Rd=concat(lanes[1], lanes[0]))


@family("F2FP", "UF2FP", classes=form("f2fp_8b_upconvert", "f2fp_8b_upconvert_scale"))
def f2fp_8b_upconvert(i):
    """Rb's H0/H1 half as two FP8/FP6 (or, scaled, S2_6) bytes: exactly to F16, or times the
    UE8M0 scales in Rc[15:0] and rounded to BF16."""
    scaled = "_scale" in i.cls
    required(i, "dstfmt", "BF16" if scaled else "F16"); required(i, "merge", "UNPACK_B"); required(i, "rndMode", "RN")
    h = half(i, "Rb|Sb", "extract_B|extract")
    codes = [extract(h, 7, 0), extract(h, 15, 8)]
    if not scaled: return upconvert(i, "8b", codes)
    return upconvert(i, "8b.scaled", codes, scale_pair(i))


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
    if not scaled: return upconvert(i, "4b", codes)
    return upconvert(i, "4b", codes, scale_pair(i))
