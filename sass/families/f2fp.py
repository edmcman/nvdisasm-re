"""F2FP conversions (migrated part: the FP8/FP6 downconverts)."""
from sass import fpformat
from sass.fpformat import FORMATS, ROUNDING
from sass.sem import concat, const, extract, ite, lnot
from sass.sem_family import family, outputs, unmodelled

FP8, FP6 = ("E4M3", "E5M2"), ("E2M3", "E3M2")


def downconvert(k): return "_to_8b_downconvert__" in k.name


def destination(i):
    """The destination format's constants, chosen by dstfmt: one conversion for every format."""
    param = lambda f, w=32: i.select_lookup_const("dstfmt", {n: f(n) for n in FP8 + FP6}, w)
    return fpformat.Dest(
        mbits=param(lambda n: FORMATS[n].mbits), emin=param(lambda n: FORMATS[n].emin),
        max_code=param(lambda n: FORMATS[n].max_code),
        # A NaN keeps its sign; FP6 has no NaN (SATFINITE: +max).
        nan_signed=param(lambda n: n in FP8, 1), nan_mag=param(lambda n: 0x7f if n in FP8 else 0x1f),
        # Without SATFINITE, E4M3 overflows to +NaN, E5M2 to signed infinity (FP6 requires SATFINITE).
        ovf_signed=param(lambda n: n != "E4M3", 1),
        ovf_mag=param(lambda n: {"E4M3": 0x7f, "E5M2": 0x7c}.get(n, FORMATS[n].max_code)))


@family("F2FP", "UF2FP", classes=downconvert)
def f2fp_8b_downconvert(i):
    """Two values, Ra and Rb as F32 or Rb's halves as F16/BF16, rounded to FP8 (E4M3/E5M2) or FP6
    (E2M3/E3M2) bytes (sign at bit 7) and packed above them with one half of Rc. RELU zeroes
    negative results except NaNs."""
    d = destination(i)
    r = fpformat.Rounding(*(i.select_lookup_const("rndMode", {m: m == mode for m in ROUNDING}, 1) for mode in ROUNDING))
    sat = i.select_lookup_const("satfinite", {"SATFINITE": 1, "nosatfinite": 0}, 1)
    relu = i.select_lookup_const("relu", {"RELU": 1, "norelu": 0}, 1)

    def convert(x, src):
        sign, mag, nan = fpformat.narrow_parts(x, src, d, r, sat)
        return ite(relu & sign & lnot(nan), const(0, 8), concat(sign, extract(mag, 6, 0)))

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
