"""F2FP conversions: TF32; F32 -> F16/BF16/E6M9 pack and MERGE_C; FP8/FP6/FP4 downconversion
(SATFINITE, RELU, RN/RZ/RP, uniform destinations); 8-bit, 4-bit and UE8M0 upconversion,
plain and scaled; S2_6 scaled fixed point; CX sources; opaque fallbacks."""
from tests.harness.manual import anchored, retained
import functools, itertools, random, struct
from tests.harness.case import encode
from tests.harness.case import Case, Cases, Later, cx_source, put, source
from tests.refs import fpformat

NEW = ("SM100", "SM101", "SM103", "SM107", "SM120")  # where the focused SM100+ conversion suites run
FP8, FP6 = ("E4M3", "E5M2"), ("E2M3", "E3M2")


def into(k, group, value, fields, regs, reg):
    """Place value in the first operand of `group` ("Rb/URb/Sb"): R/UR number `reg`, else literal."""
    name = source(k, *group.split("/"))
    put(k, name, value, fields, regs, None, reg, imm=(k.operand_types[name].type,))
    return name


def has(k, *operands): return all(n in k.operand_types for n in operands)


def dest(k): return "URd" if k.name.startswith("uf2fp_") else "Rd"


def observed(k): return ("UR" if dest(k) == "URd" else "R") + "8"


# References of the sampled (anchored) sweeps, deferred with Later until sampling keeps a case.

def widened(fmt, codes, relu):
    """The codes widened to F16 halves, low first; RELU zeroes negative halves."""
    halves = (fpformat.widen(fmt, code) for code in codes)
    return sum((0 if relu and h & 0x8000 else h) << 16 * j for j, h in enumerate(halves))


def scaled_halves(fmt, lanes, *flags):
    """(code, scale) lanes upconverted to BF16 halves, low first."""
    return sum(fpformat.scaled_bf16(code, fmt, scale, *flags) << 16 * j for j, (code, scale) in enumerate(lanes))


def fp4_nibbles(words, fmt, relu, src):
    """Each word's two halves packed to E2M1/E0M3 nibbles, low first."""
    lanes = [fpformat.fp4(w >> (16 * j) & 0xffff, fmt, relu, src) for w in words for j in range(2)]
    return sum(lane << (4 * j) for j, lane in enumerate(lanes))


def fp8_down_word(fmt, rnd, satfinite, relu, srcfmt, a, b, high):
    """`high` above the FP8 pair: A in the high byte (PACK_AB), or B's high half (UNPACK_B)."""
    x, y = (fpformat.fp8(v, fmt, rnd, satfinite, relu, srcfmt) for v in (a, b))
    return high << 16 | ((y << 8) | x if srcfmt != "F32" else (x << 8) | y)


def fp6_down_word(fmt, rnd, relu, srcfmt, x, y, high):
    ox, oy = (fpformat.fp6(v, fmt, rnd, relu, srcfmt) for v in (x, y))
    return high << 16 | ((oy << 8) | ox if srcfmt != "F32" else (ox << 8) | oy)


def fp4_down_word(fmt, relu, a, b, carry):
    ref = lambda v: fpformat.fp4(v, fmt, relu, relu_nan=True)
    return (carry & 0xffffff00) | (ref(a) << 4) | ref(b)


def scaled_pair(keep, one, a, scale_a, b, scale_b):
    """`keep` above one(a) in the high byte and one(b) in the low."""
    return (keep << 16) | (one(a, scale_a) << 8) | one(b, scale_b)


@retained('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def tf32(c):
    # F2FP.TF32.F32.PACK_B keeps 10 fraction bits, rounding the discarded 13 bits.
    values = [0, 0x80000000, 0x3f801000, 0x3f803000, 0xbf801000, 0xbf803000, 0x3f7ff000, 0x3f7ff001, 0x7f800000, 0xff800000]
    for k in c.of("F2FP", "f2fp_tf32__"):
        src = source(k, "Rb", "URb", "Sb")
        if src is None: continue
        for rnd in ("RN", "RZ"):
            for bits in values:
                fields, regs, memory = {"Rd": 8, "dstfmt": "TF32", "srcfmt": "F32", "merge": "PACK_B", "rndMode": rnd, "relu": "norelu"}, {}, []
                if not put(k, src, bits, fields, regs, memory, 4, ureg=10, bank=(2, 0x100), imm=("F32Imm",)): continue
                result = fpformat.tf32(bits, rnd == "RN")
                c.add(k.name, fields, regs, {"R8": result, "__native": True}, memory=memory)
                relu = result if result & 0x7fffffff > 0x7f800000 else 0 if result & 0x80000000 else result
                c.add(k.name, dict(fields, relu="RELU"), regs, {"R8": relu, "__native": True}, memory=memory)


PACKED = [0, 0x80000000, 0x3f800000, 0xbf800000, 0x3f801000, 0x3f803000, 0x33000000, 0x33800000, 0x387fc000, 0x38800000,
          0x477fe000, 0x477ff000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 1, 0x007fffff, 0x00800000, 0x7f7fffff, 0xff7fffff]
PACKED += [struct.unpack("<I", struct.pack("<f", v))[0] for v in (2.0 ** -41, 2.0 ** -40, 2.0 ** -39, 2.0 ** -30, -2.0 ** -40,
                                                                  2.0 ** 31, 2.0 ** 32 - 2.0 ** 22, 2.0 ** 32 - 2.0 ** 21, 2.0 ** 32)]


@retained('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def pack16(c):
    # PACK_AB F32 -> F16/BF16/E6M9 for scalar and uniform register inputs.
    for k in c.of("F2FP", "f2fp__"):
        srcs = [source(k, *group.split("/")) for group in ("Ra/URa", "Rb/URb")]
        if not all(srcs) or any(k.operand_types[n].type not in ("Register", "UniformRegister") for n in srcs): continue
        if not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "relu"): continue
        sat = "satfinite" in k.operand_types

        def add(fmt, rnd, relu, satfinite):
            for i, av in enumerate(PACKED):
                bv = PACKED[(i * 7 + 3) % len(PACKED)]
                fields = {"Rd": 8, "dstfmt": fmt, "srcfmt": "F32", "merge": "PACK_AB", "rndMode": rnd, "relu": relu,
                          **({"satfinite": satfinite} if sat else {})}
                regs = {}
                for n, value, reg in zip(srcs, (av, bv), (2, 4)): put(k, n, value, fields, regs, None, reg, ureg=reg + 6)
                conv = lambda bits: fpformat.pack16(bits, fmt, rnd, satfinite == "SATFINITE", relu == "RELU")
                c.add(k.name, fields, regs, {"R8": (conv(av) << 16) | conv(bv), "__native": True})
        for fmt in c.labels(k, "dstfmt", ("F16", "BF16", "E6M9")):
            if "F32" not in c.enum(k, "srcfmt") or "PACK_AB" not in c.enum(k, "merge"): continue
            for rnd in c.labels(k, "rndMode", ("RN", "RZ")): add(fmt, rnd, "norelu", "nosatfinite")
        if "RELU" in c.enum(k, "relu"):
            for fmt in c.labels(k, "dstfmt", ("F16", "BF16", "E6M9")):
                for relu, satfinite in itertools.product(("norelu", "RELU"), ("nosatfinite", "SATFINITE") if sat else (None,)):
                    if relu == "norelu" and satfinite != "SATFINITE": continue
                    for rnd in c.labels(k, "rndMode", ("RN", "RZ")): add(fmt, rnd, relu, satfinite)
    # E6M9 also accepts immediate and direct constant-bank B operands.
    for k in c.of("F2FP", "f2fp__"):
        srcs = [source(k, *group.split("/")) for group in ("Ra/URa", "Rb/URb/Sb")]
        if not all(srcs) or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "relu") or "E6M9" not in c.enum(k, "dstfmt"): continue
        sat_atom = k.operand_types.get("satfinite")
        sat_modes = ("nosatfinite", "SATFINITE") if sat_atom and "SATFINITE" in c.md.enums.get(sat_atom.type, {}) else (None,)
        for rnd, relu, satfinite in itertools.product(("RN", "RZ"), ("norelu", "RELU"), sat_modes):
            for i, av in enumerate(PACKED):
                bv = PACKED[(i * 7 + 3) % len(PACKED)]
                fields = {"Rd": 8, "dstfmt": "E6M9", "srcfmt": "F32", "merge": "PACK_AB", "rndMode": rnd, "relu": relu,
                          **({"satfinite": satfinite} if satfinite else {})}
                regs, memory = {}, []
                if all(put(k, n, v, fields, regs, memory, r, ureg=r + 6, bank=(2, 0x100), imm=("F32Imm",)) for n, v, r in zip(srcs, (av, bv), (2, 4))):
                    conv = lambda bits: fpformat.pack16(bits, "E6M9", rnd, satfinite == "SATFINITE", relu == "RELU")
                    c.add(k.name, fields, regs, {"R8": (conv(av) << 16) | conv(bv), "__native": True}, memory=memory)
    # MERGE_C converts one F32 input into the low half and copies the selected half of the base.
    for k in c.of("F2FP", "f2fp_merge_c__"):
        src, base = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        if not src or not base or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "relu", "extract"): continue
        sat_modes = ("nosatfinite", "SATFINITE") if "satfinite" in k.operand_types else (None,)
        for fmt, rnd, relu, satfinite, extract in itertools.product(("F16", "BF16", "E6M9"), ("RN", "RZ"), ("norelu", "RELU"), sat_modes, ("H0", "H1")):
            if (fmt not in c.enum(k, "dstfmt") or "F32" not in c.enum(k, "srcfmt") or "MERGE_C" not in c.enum(k, "merge")
                    or rnd not in c.enum(k, "rndMode") or extract not in c.enum(k, "extract")): continue
            for bits in (0x3fc00000, 0xbfc00000, 0x7fc12345, 0x7f800000, 0xff800000):
                fields = {"Rd": 8, "dstfmt": fmt, "srcfmt": "F32", "merge": "MERGE_C", "rndMode": rnd, "relu": relu, "extract": extract,
                          **({"satfinite": satfinite} if satfinite else {})}
                regs, memory = {}, []
                if all(put(k, n, v, fields, regs, memory, r, bank=(2, 0x100), imm=("F32Imm",)) for n, v, r in ((src, bits, 2), (base, 0xdeadbeef, 4))):
                    half = fpformat.pack16(bits, fmt, rnd, satfinite == "SATFINITE", relu == "RELU")
                    c.add(k.name, fields, regs, {"R8": ((0xdead if extract == "H1" else 0xbeef) << 16) | half, "__native": True}, memory=memory)
    for k in (k for k in c.of("F2FP", cx=True) if k.name == "f2fp__RCxR"):
        cx_fields, cx_regs, memory = cx_source(c.arch, k, cx_name(k), 0x40000000)
        c.add(k.name, {"Rd": 8, "Ra": 2, "dstfmt": "F16", "srcfmt": "F32", "merge": "PACK_AB", "rndMode": "RN", "relu": "norelu",
                       "satfinite": "nosatfinite", **cx_fields}, {"R2": 0x3f800000, **cx_regs}, {"R8": 0x3c004000, "__native": True}, memory=memory)
    for k in (k for k in c.of("F2FP", cx=True) if k.name in ("f2fp_merge_c__RCxR", "f2fp_merge_c__RRCx")):
        name = cx_name(k)
        fields = {"Rd": 8, "dstfmt": "F16", "srcfmt": "F32", "merge": "MERGE_C", "rndMode": "RN", "relu": "norelu", "satfinite": "nosatfinite", "extract": "H1"}
        if name == "Sb": fields["Rc"] = 6; regs = {"R6": 0xaabbccdd}
        else: fields["Rb"] = 2; regs = {"R2": 0x3f800000}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, 0x3f800000 if name == "Sb" else 0xaabbccdd)
        c.add(k.name, dict(fields, **cx_fields), dict(regs, **cx_regs), {"R8": 0xaabb3c00, "__native": True}, memory=memory)


def cx_name(k): return next(n for n, a in k.operand_types.items() if a.type == "CX")


@retained('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def fp8_pack(c):
    # F2FP FP8 PACK_AB_MERGE_C, with an exact rational-distance RN-even reference.
    values = [0, 0x80000000, 1, 0x007fffff, 0x00800000, 0x33800000, 0x3f800000, 0xbf800000, 0x3f801000, 0x3f803000,
              0x40600000, 0x43e00000, 0x43e10000, 0x43e08000, 0x477fe000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001]
    for k in c.of("F2FP", "f2fp_f32_to_8b_downconvert__"):
        srcs = [source(k, *group.split("/")) for group in ("Ra/URa", "Rb/URb/Sb", "Rc/URc/Sc")]
        if not all(srcs) or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "satfinite", "relu", "extract"): continue
        for fmt in c.labels(k, "dstfmt", ("E4M3", "E5M2")):
            for extract in c.labels(k, "extract", ("H0", "H1")):
                for i, av in enumerate(values):
                    bv, cv = values[(i * 7 + 3) % len(values)], (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff
                    fields = {"Rd": 6, "dstfmt": fmt, "srcfmt": "F32", "merge": "PACK_AB_MERGE_C", "rndMode": "RN", "satfinite": "SATFINITE",
                              "relu": "norelu", "extract": extract}
                    regs, memory = {}, []
                    for n, value, reg in zip(srcs, (av, bv, cv), (2, 4, 6)): put(k, n, value, fields, regs, memory, reg, ureg=reg + 6, bank=(2, 0x100), imm=("F32Imm",))
                    half = (cv >> 16) & 0xffff if extract == "H1" else cv & 0xffff
                    c.add(k.name, fields, regs, {"R6": (half << 16) | (fpformat.fp8(av, fmt) << 8) | fpformat.fp8(bv, fmt), "__native": True}, memory=memory)


@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def upconvert(c, part):
    # F2FP.F16.{E4M3,E5M2,E2M3,E3M2,E3M4}.UNPACK_B widens both bytes of the selected half.
    for k in c.of("F2FP", "f2fp_8b_upconvert__"):
        src = source(k, "Rb", "URb", "Sb")
        if src is None or k.operand_types[src].type not in ("Register", "UniformRegister") or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "relu", "extract_B"): continue
        reg = 2 if k.operand_types[src].type == "Register" else 8
        for fmt in c.labels(k, "srcfmt", ("E4M3", "E5M2", "E2M3", "E3M2", "E3M4")):
            top = 63 if fmt in FP6 else 255 if fmt == "E3M4" else 127
            for extract in c.labels(k, "extract_B", ("H0", "H1")):
                for relu in ("norelu", "RELU"):
                    for i in part.sweep(k, top + 1):
                        pair = i | ((top - i) << 8)
                        regs, fields = {}, {"Rd": 8, "dstfmt": "F16", "srcfmt": fmt, "merge": "UNPACK_B", "rndMode": "RN", "relu": relu, "extract_B": extract}
                        put(k, src, pair if extract == "H0" else pair << 16, fields, regs, None, reg)
                        c.add(k.name, fields, regs, {"R8": Later(widened, fmt, (i, top - i), relu == "RELU"), "__native": True})
    # SM100+ E2M1/E0M3 byte upconvert: low nibble -> low F16, high nibble -> high F16.
    for k in c.of("F2FP", "f2fp_4b_upconvert__", cx=None):
        src = source(k, "Rb", "URb", "Sb")
        if src is None or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "relu", "selB"): continue
        for fmt in c.labels(k, "srcfmt", ("E2M1", "E0M3")):
            for selector in c.labels(k, "selB", ("B0", "B1", "B2", "B3")):
                for relu in ("norelu", "RELU"):
                    for byte in part.sweep(k, 256):
                        fields, regs = {"Rd": 8, "dstfmt": "F16", "srcfmt": fmt, "merge": "UNPACK_B", "rndMode": "RN", "relu": relu, "selB": selector}, {}
                        put(k, src, byte << (int(selector[1:]) * 8), fields, regs, None, 2, ureg=4, imm=("F32Imm",))
                        c.add(k.name, fields, regs, {"R8": Later(widened, fmt, (byte & 0xf, byte >> 4), relu == "RELU"), "__native": True})
    # SM100+ scaled FP4 upconvert: per-lane UE8M0 multiplication after unpacking (PTX); low/high
    # nibbles pair with low/high scale bytes.
    for k in c.of("F2FP", "f2fp_4b_upconvert_scale__", cx=None):
        bsrc, csrc = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        if bsrc is None or csrc is None: continue
        for fmt in c.labels(k, "srcfmt", ("E2M1", "E0M3")):
            for selector in c.labels(k, "selB", ("B0", "B1", "B2", "B3")):
                for relu in ("norelu", "RELU"):
                    for byte in part.sweep(k, 256):
                        scales = byte | ((255 - byte) << 8)
                        fields = {"Rd": 8, "dstfmt": "BF16", "srcfmt": fmt, "merge": "UNPACK_B", "rndMode": "RN", "satfinite": "SATFINITE", "relu": relu, "selB": selector, "iswzC": "H0"}
                        regs = {}
                        put(k, bsrc, byte << (int(selector[1:]) * 8), fields, regs, None, 2, ureg=4, imm=(k.operand_types[bsrc].type,))
                        put(k, csrc, scales, fields, regs, None, 6, ureg=8, imm=(k.operand_types[csrc].type,))
                        lanes = ((byte & 15, scales & 0xff), (byte >> 4, scales >> 8))
                        c.add(k.name, fields, regs, {"R8": Later(scaled_halves, fmt, lanes, relu == "RELU"), "__native": True})
    # SM100+ UE8M0 to BF16x2 (CUDA host reference: 0xff NaN, 0 the 2^-127 subnormal, else << 7).
    for k in c.of("F2FP", "f2fp_E8_upconvert__", cx=None):
        src = source(k, "Rb", "URb", "Sb")
        if src is None or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "extract_B"): continue
        for extract in ("H0", "H1"):
            for byte in part.sweep(k, 256):
                pair = byte | ((255 - byte) << 8)
                fields, regs = {"Rd": 8, "dstfmt": "BF16", "srcfmt": "E8", "merge": "UNPACK_B", "rndMode": "RN", "extract_B": extract}, {}
                put(k, src, pair if extract == "H0" else pair << 16, fields, regs, None, 2, ureg=4, imm=("F32Imm",))
                c.add(k.name, fields, regs, {"R8": fpformat.e8m0_to_bf16(byte) | (fpformat.e8m0_to_bf16(255 - byte) << 16), "__native": True})


@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def fp4_pack(c, part):
    # SM100+ F16/BF16 to E2M1x4/E0M3x4: B supplies the low byte and C the next (ptxas
    # UNPACK_B_MERGE_C lowering).
    thresholds = {"F16": ((0x3400, 0x3a00, 0x3d00, 0x3f00, 0x4100, 0x4300, 0x4500), (0x3800, 0x3e00, 0x4100, 0x4300, 0x4480, 0x4580, 0x4680)),
                  "BF16": ((0x3e80, 0x3f40, 0x3fa0, 0x3fe0, 0x4020, 0x4060, 0x40a0), (0x3f00, 0x3fc0, 0x4020, 0x4060, 0x4090, 0x40b0, 0x40d0))}
    edges = {"F16": [0, 1, 0x03ff, 0x0400, 0x8000, 0x8001, 0x7c00, 0xfc00, 0x7c01, 0x7e00, 0xfc01, 0xfe00],
             "BF16": [0, 1, 0x007f, 0x0080, 0x8000, 0x8001, 0x7f80, 0xff80, 0x7f81, 0x7fc0, 0xff81, 0xffc0]}
    for k in c.of("F2FP", "f2fp_f16_to_4b_downconvert__", cx=None):
        bsrc, csrc = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        if bsrc is None or csrc is None: continue
        for src in ("F16", "BF16"):
            vals = set(edges[src])
            for t in set(thresholds[src][0]) | set(thresholds[src][1]):
                vals.update((t - 1, t, t + 1, t | 0x8000, (t | 0x8000) - 1, (t | 0x8000) + 1))
            rng = random.Random("f2fp_4b_down")
            vals.update(rng.getrandbits(16) for _ in range(24))
            vals = sorted(vals)
            # Pair each boundary with a different lane to catch lane swaps.
            for fmt in c.labels(k, "dstfmt", ("E2M1", "E0M3")):
                for relu in ("norelu", "RELU"):
                    for i in part.sweep(k, len(vals)):
                        bword = vals[i] | (vals[(i * 7 + 3) % len(vals)] << 16)
                        cword = vals[(i * 11 + 5) % len(vals)] | (vals[(i * 13 + 9) % len(vals)] << 16)
                        fields = {"Rd": 8, "dstfmt": fmt, "srcfmt": src, "merge": "UNPACK_B_MERGE_C", "rndMode": "RN", "satfinite": "SATFINITE", "relu": relu}
                        regs = {}
                        put(k, bsrc, bword, fields, regs, None, 2, ureg=4, imm=(k.operand_types[bsrc].type,))
                        put(k, csrc, cword, fields, regs, None, 6, ureg=8, imm=(k.operand_types[csrc].type,))
                        c.add(k.name, fields, regs, {"R8": Later(fp4_nibbles, (bword, cword), fmt, relu == "RELU", src), "__native": True})


# --- Focused SM100+ downconversion suites ------------------------------------------------

@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def fp8_down(c, part):
    """F32/F16/BF16 -> E4M3/E5M2 PACK/UNPACK_B_MERGE_C with RN/RP, RELU, (no)SATFINITE and
    uniform destinations. NaN signs are not compared."""
    f32_classes = c.named("f2fp_f32_to_8b_downconvert__", "uf2fp_f32_to_8b_downconvert__")
    half_classes = c.named("f2fp_f16_to_8b_downconvert__", "uf2fp_f16_to_8b_downconvert__")
    rng = random.Random(0xf328b)
    sample = [0, 0x80000000, 1, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x40600000, 0x43e10000, 0x477fe000,
              0x7f800000, 0xff800000, 0x7fc00000, 0xffc12345] + [rng.getrandbits(32) for _ in range(6)]

    def add(k, fmt, srcfmt, rnd, relu, extract, satfinite, a, b, carry):
        half = srcfmt != "F32"
        fields = {"dstfmt": fmt, "srcfmt": srcfmt, "merge": "UNPACK_B_MERGE_C" if half else "PACK_AB_MERGE_C", "rndMode": rnd,
                  "satfinite": satfinite, "relu": relu, "extract": extract, dest(k): 8}
        regs = {}
        if half: into(k, "Rb/URb/Sb", a | (b << 16), fields, regs, 2)
        else: into(k, "Ra/URa", a, fields, regs, 2); into(k, "Rb/URb/Sb", b, fields, regs, 4)
        into(k, "Rc/URc/Sc", carry, fields, regs, 6)
        high = carry >> 16 if extract == "H1" else carry & 0xffff
        c.add(k.name, fields, regs, {observed(k): Later(fp8_down_word, fmt, rnd, satfinite == "SATFINITE", relu == "RELU", srcfmt, a, b, high),
                                     "__native": True, "__fp8_nan": fmt})
    for k in f32_classes:
        for fmt, relu, extract, rnd in itertools.product(("E4M3", "E5M2"), ("norelu", "RELU"), ("H0", "H1"), ["RN"] + c.labels(k, "rndMode", ("RP",))):
            for i in part.sweep(k, len(sample)):
                add(k, fmt, "F32", rnd, relu, extract, "SATFINITE", sample[i], sample[(i * 7 + 3) % len(sample)], (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff)
    for k in half_classes:
        for srcfmt in c.labels(k, "srcfmt", ("F16", "BF16")):
            for fmt, relu, extract, rnd in itertools.product(("E4M3", "E5M2"), ("norelu", "RELU"), ("H0", "H1"), ["RN"] + c.labels(k, "rndMode", ("RP",))):
                vals = fp8_samples(fmt, srcfmt)
                for i in part.sweep(k, len(vals)):
                    add(k, fmt, srcfmt, rnd, relu, extract, "SATFINITE", vals[i], vals[(i * 11 + 7) % len(vals)], (0xd35a9c71 ^ (i * 0x01010101)) & 0xffffffff)
    f32_values = (0, 0x80000000, 0x3f800000, 0xbf800000, 0x477fe000, 0x477ff000, 0xc77ff000, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc12345)
    half_values = (0, 0x8000, 0x3c00, 0xbc00, 0x7bff, 0xfbff, 0x7c00, 0xfc00, 0x7e00, 0xfe00)
    for k in f32_classes + half_classes:
        half = k in half_classes
        for srcfmt in c.labels(k, "srcfmt", ("F16", "BF16")) if half else ("F32",):
            samples = half_values if half else f32_values
            for fmt, rnd, relu, extract in itertools.product(("E4M3", "E5M2"), c.labels(k, "rndMode", ("RN", "RP")), ("norelu", "RELU"), ("H0", "H1")):
                for i in part.sweep(k, len(samples)):
                    add(k, fmt, srcfmt, rnd, relu, extract, "nosatfinite", samples[i], samples[(i * 7 + 3) % len(samples)], (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff)


def fp8_samples(fmt, srcfmt):
    """Specials, values around every 7th FP8 midpoint, and random 16-bit sources."""
    special = [0, 0x8000, 1, 0x8001, 0x03ff, 0x83ff, 0x0400, 0x8400, 0x7c00, 0xfc00, 0x7e00, 0xfe00]
    finite = [v for _, v in fpformat.finite(fmt)]
    for idx, (low, high) in enumerate(zip(finite, finite[1:])):
        if idx % 7: continue
        midpoint = float((low + high) / 2)
        try: raw = int.from_bytes(struct.pack("<e", midpoint), "little") if srcfmt == "F16" else int.from_bytes(struct.pack("<f", midpoint), "little") >> 16
        except OverflowError: continue
        special.extend(x & 0xffff for x in (raw - 1, raw, raw + 1))
        special.extend(x | 0x8000 for x in (raw - 1, raw, raw + 1))
    rng = random.Random(0x8b00d + (0 if fmt == "E4M3" else 1) + (0 if srcfmt == "F16" else 2))
    special.extend(rng.getrandbits(16) for _ in range(8))
    return sorted(set(x & 0xffff for x in special))


def fp6_classes(c):
    return [k for k in c.md.classes if "to_8b_downconvert__" in k.name and not k.alternate and all(f in c.enum(k, "dstfmt") for f in FP6)]


@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def fp6_down(c, part):
    """F32/F16/BF16 -> E2M3/E3M2 SATFINITE, RN, with RELU (the md allows FP6 destinations only with
    SATFINITE and RN)."""
    for k in fp6_classes(c):
        if not part.whole(k): continue
        half = "_f16_to_8b_" in k.name
        for srcfmt in ("F16", "BF16") if half else ("F32",):
            for fmt in FP6:
                vals = fp6_samples(srcfmt, fmt)
                for rnd, relu in itertools.product(("RN",), ("norelu", "RELU")):
                    for i in range(0, len(vals) - 1, 2):
                        x, y, carry = vals[i], vals[i + 1], 0x9bd753e1 ^ (i * 0x01010101)
                        fields = {"dstfmt": fmt, "srcfmt": srcfmt, "merge": "UNPACK_B_MERGE_C" if half else "PACK_AB_MERGE_C", "rndMode": rnd,
                                  "satfinite": "SATFINITE", "relu": relu, "extract": "H1", dest(k): 8}
                        regs = {}
                        if half: into(k, "Rb/URb/Sb", x | (y << 16), fields, regs, 2)
                        else: into(k, "Ra/URa", x, fields, regs, 2); into(k, "Rb/URb/Sb", y, fields, regs, 4)
                        into(k, "Rc/URc/Sc", carry, fields, regs, 6)
                        c.add(k.name, fields, regs, {observed(k): Later(fp6_down_word, fmt, rnd, relu == "RELU", srcfmt, x, y, (carry >> 16) & 0xffff), "__native": True})


def fp6_probe(k, satfinite, relu, extract):
    half = "_f16_to_8b_" in k.name
    fields = {"dstfmt": "E2M3", "srcfmt": "F16" if half else "F32", "merge": "UNPACK_B_MERGE_C" if half else "PACK_AB_MERGE_C",
              "rndMode": "RN", "satfinite": satfinite, "relu": relu, "extract": extract, dest(k): 8}
    regs = {}
    if half: into(k, "Rb/URb/Sb", 0x3c003c00, fields, regs, 2)
    else: into(k, "Ra/URa", 0x3f800000, fields, regs, 2); into(k, "Rb/URb/Sb", 0x40000000, fields, regs, 4)
    into(k, "Rc/URc/Sc", 0xdeadbeef, fields, regs, 6)
    return fields, regs


def fp6_samples(srcfmt, fmt):
    """Specials, every FP6 value and midpoint, and random sources."""
    rng = random.Random(0xf600 + FP6.index(fmt) * 19 + {"F32": 0, "F16": 1, "BF16": 2}[srcfmt])
    values = [v for _, v in fpformat.finite(fmt)]
    mids = [(a + b) / 2 for a, b in zip(values, values[1:])]
    if srcfmt == "F32":
        samples = [0, 0x80000000, 1, 0x007fffff, 0x00800000, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc12345]
        samples.extend(rng.getrandbits(32) for _ in range(14))
        samples += [struct.unpack("<I", struct.pack("<f", float(v)))[0] for v in values + mids]
        return sorted(set(samples))
    samples = [0, 0x8000, 1, 0x7c00, 0xfc00, 0x7e00, 0xfe00]
    for v in values + mids:
        samples.append(int.from_bytes(struct.pack("<e", float(v)) if srcfmt == "F16" else struct.pack("<f", float(v))[2:4], "little"))
    samples.extend(rng.getrandbits(16) for _ in range(12))
    return sorted(set(samples))


@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def fp4_down(c, part):
    """F32 -> E2M1/E0M3 PACK_AB_MERGE_C: PACK_AB writes the low byte, C supplies the upper bits."""
    thresholds = {"E2M1": (0x3e800000, 0x3f400000, 0x3fa00000, 0x3fe00000, 0x40200000, 0x40600000, 0x40a00000),
                  "E0M3": (0x3f000000, 0x3fc00000, 0x40200000, 0x40600000, 0x40900000, 0x40b00000, 0x40d00000)}
    rng = random.Random(0xf2f4)
    for k in c.named("f2fp_f32_to_4b_downconvert__"):
        for fmt in c.labels(k, "dstfmt", ("E2M1", "E0M3")):
            edges = [0, 0x80000000, 1, 0x80000001, 0x7f800000, 0xff800000, 0x7fc00001, 0xffc12345, 1, 0x007fffff]
            for t in thresholds[fmt]:
                edges.extend((t - 1, t, t + 1, t | 0x80000000, (t - 1) | 0x80000000, (t + 1) | 0x80000000))
            edges.extend(rng.getrandbits(32) for _ in range(20))
            for relu in ("norelu", "RELU"):
                for i in part.sweep(k, len(edges)):
                    a, b, carry = edges[i], edges[(i * 7 + 5) % len(edges)], (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff
                    fields = dict(Rd=8, dstfmt=fmt, srcfmt="F32", merge="PACK_AB_MERGE_C", rndMode="RN", satfinite="SATFINITE", relu=relu)
                    regs = {}
                    for group, reg, value in (("Ra/URa", 2, a), ("Rb/URb/Sb", 4, b), ("Rc/URc/Sc", 6, carry)): into(k, group, value, fields, regs, reg)
                    c.add(k.name, fields, regs, {"R8": Later(fp4_down_word, fmt, relu == "RELU", a, b, carry), "__native": True})


@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def mx8_down(c, part):
    """F32/BF16 -> S2_6 with per-lane UE8M0 scales (PTX .scaled::n2::ue8m0: A's in Rc[31:24], B's in
    Rc[23:16], where ptxas puts scale-factor), Rc[15:0] kept above; NOSATFINITE (bit 77) is an opaque fallback."""
    floats = (0, 0x80000000, 0x3f000000, 0x3fc00000, 0xbfc00000, 0x3f400000, 0xbf400000, 0x3f600000, 0xbf600000, 0x3f800000, 0xbf800000,
              0x3ffe0000, 0xbffe0000, 0x40000000, 0xc0000000, 0x3c000000, 0xbc000000, 0x3cc00000, 0xbcc00000, 0x3d200000, 0xbd200000,
              0x7e800000, 0xfe800000, 0x7f000000, 0xff000000, 0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x7fc12345, 0xffc54321)
    scales = (0, 1, 126, 127, 128, 253, 254, 255)
    scale_pairs = tuple((s, scales[(i + 3) % len(scales)]) for i, s in enumerate(scales)) + tuple((s, s) for s in scales)
    first = {}
    for k in c.named("f2fp_f16_to_mx8_downconvert_scale__", "f2fp_f32_to_mx8_downconvert_scale__"):
        bf16 = k.name.startswith("f2fp_f16_")
        for i in part.sweep(k, len(floats)):
            a, b = floats[i], floats[(i * 7 + 3) % len(floats)]
            if bf16: a, b = a & 0xffff0000, b & 0xffff0000
            for scale_a, scale_b in scale_pairs:
                for relu in ("norelu", "RELU"):
                    # A occupies the high output byte with the high scale byte; B the low.
                    fields = dict(dstfmt="S2_6", srcfmt="BF16" if bf16 else "F32", merge="UNPACK_B_MERGE_C" if bf16 else "PACK_AB_MERGE_C",
                                  rndMode="RN", satnarrow="SATFINITE", relu=relu, Rd=8)
                    regs = {}
                    if bf16: into(k, "Rb/URb/Sb", (a & 0xffff0000) | (b >> 16), fields, regs, 4)
                    else: into(k, "Ra/URa", a, fields, regs, 2); into(k, "Rb/URb/Sb", b, fields, regs, 4)
                    keep = (i * 0x9e37 + scale_a) & 0xffff
                    into(k, "Rc/URc/Sc", (scale_a << 24) | (scale_b << 16) | keep, fields, regs, 6)
                    if i == 0: first.setdefault(k.name, (fields, regs))
                    s2_6 = functools.partial(fpformat.s2_6, relu=relu == "RELU")
                    c.add(k.name, fields, regs, {"R8": Later(scaled_pair, keep, s2_6, a, scale_a, b, scale_b), "__native": True})
    if c.fp == "exact":
        for cls, (fields, regs) in first.items():
            raw = int.from_bytes(bytes.fromhex(encode(c.arch, cls, **fields)), "little") ^ (1 << 77)
            c.append(Case(cls, dict(word=raw.to_bytes(16, "little").hex(), registers=regs, observe=["R8"], inspect_pcode=True),
                          {"__opaque": "sass_opaque_F2FP("}))


@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def scaled_8b_down(c, part):
    """SM107 F32/F16/BF16 -> E4M3/E5M2/E2M3/E3M2/UE5M3 divided by one UE8M0 scale (PTX cvt
    .scaled::n1::ue8m0; ptxas puts it in Rc[31:24]); A (or Rb's high half) in the high byte, Rc[15:0]
    kept above; Rc[23:16] holds noise. .PZO, and the unscaled forms' UE5M3/.PZO likewise."""
    floats = (0, 0x80000000, 1, 0x00800000, 0x3f000000, 0x3fc00000, 0xbfc00000, 0x3f800000, 0xbf800000, 0x40e00000, 0xc3e00000,
              0x43e00001, 0x47600000, 0x47700000, 0xc7700000, 0x3b000000, 0x3c000000, 0x3c400000, 0x7f7fffff, 0x7f800000, 0xff800000,
              0x7fc12345, 0xffc54321, 0x3e7fffff, 0x41300000, 0x41980000, 0x47740000, 0x477c0000, 0x37000000, 0x36800000)
    halves = {"F16": (0, 0x8000, 1, 0x0400, 0x3800, 0x3e00, 0xbe00, 0x3c00, 0xbc00, 0x4700, 0xdf00, 0x5f00, 0x7bff, 0x7c00, 0xfc00, 0x7e01, 0x2c00, 0x3500),
              "BF16": tuple(x >> 16 for x in floats)}
    scales = (0, 1, 120, 126, 127, 128, 134, 253, 254, 255)

    def code(fmt, rnd, sat, relu, pz, srcfmt):
        def one(x, scale):
            if fmt == "UE5M3": return fpformat.ue5m3(x, srcfmt, rnd, sat, scale)
            out = fpformat.fp8(x, fmt, rnd, sat, relu, srcfmt, scale) if fmt in FP8 else fpformat.fp6(x, fmt, rnd, relu, srcfmt, scale)
            return fpformat.pzo(out) if pz else out
        return one

    for k in c.named("f2fp_f16_to_8b_downconvert_scale__", "f2fp_f32_to_8b_downconvert_scale__", "f2fp_f32_to_8b_downconvert__", "f2fp_f16_to_8b_downconvert__"):
        rng, chosen = random.Random("f2fp_8b_down_scale-" + k.name), set(part.sweep(k, len(floats)))
        half, scaled = k.name.startswith("f2fp_f16_"), "_scale__" in k.name
        if not scaled and not c.labels(k, "dstfmt", ("UE5M3",)) and "pzo" not in k.operand_types: continue
        for srcfmt in (c.labels(k, "srcfmt", ("F16", "BF16")) if half else ["F32"]):
            for fmt, rnd, sat, relu, pz in itertools.product(c.labels(k, "dstfmt", FP8 + FP6 + ("UE5M3",)), ("RN", "RZ"), ("nosatfinite", "SATFINITE"), ("norelu", "RELU"), ("nopzo", "PZO") if "pzo" in k.operand_types else (None,)):
                if fmt in FP6 and sat != "SATFINITE" or fmt == "UE5M3" and relu == "RELU": continue
                if not scaled and fmt != "UE5M3" and pz != "PZO": continue   # the plain FP8/FP6 forms have their own suites
                for i, a in enumerate(floats):
                    b = floats[(i * 7 + 3) % len(floats)]
                    scale = scales[(i * 3 + rng.randrange(len(scales))) % len(scales)] if scaled else 127
                    if half: a, b = (halves[srcfmt][j % len(halves[srcfmt])] for j in (i, i * 5 + 2))
                    fields = dict(Rd=8, dstfmt=fmt, srcfmt=srcfmt, merge="UNPACK_B_MERGE_C" if half else "PACK_AB_MERGE_C",
                                  rndMode=rnd, satfinite=sat, relu=relu, **(dict(pzo=pz) if pz else {}), **(dict(scalec="SCALE_BY_C") if scaled else dict(extract="H0")))
                    regs, keep = {}, rng.getrandbits(16)
                    if half: into(k, "Rb/URb/Sb", (a << 16) | b, fields, regs, 4)
                    else: into(k, "Ra/URa", a, fields, regs, 2); into(k, "Rb/URb/Sb", b, fields, regs, 4)
                    into(k, "Rc/URc/Sc", (scale << 24) | (rng.getrandbits(8) << 16) | keep if scaled else keep, fields, regs, 6)
                    one = code(fmt, rnd, sat == "SATFINITE", relu == "RELU", pz == "PZO", srcfmt)
                    if i in chosen: c.add(k.name, fields, regs, {"R8": Later(scaled_pair, keep, one, a, scale, b, scale), "__native": True})


@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def scaled_up(c, part):
    """Every byte and scale encoding (exhaustive) for 8-bit scaled upconversion to BF16."""
    for k in c.named("f2fp_8b_upconvert_scale__"):
        bsrc, csrc = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        for fmt in c.labels(k, "srcfmt", ("E5M2", "E4M3", "E2M3", "E3M2", "E3M4", "S2_6")):
            for extract, relu, satfinite in itertools.product(("H0", "H1"), ("norelu", "RELU"), ("nosatfinite", "SATFINITE")):
                for i in part.sweep(k, 256):
                    # Every byte and scale encoding per lane; the second lane permutes both, and
                    # the unselected half holds different data.
                    code0, scale0 = i, (i * 73 + 19) & 0xff
                    code1, scale1 = (i * 29 + 7) & 0xff, 255 - scale0
                    low, scales = code0 | (code1 << 8), scale0 | (scale1 << 8)
                    other_bytes = ((i ^ 0xa5) & 0xff) | (((i ^ 0x5a) & 0xff) << 8)
                    other_scales = ((i * 11 + 3) & 0xff) | (((i * 17 + 9) & 0xff) << 8)
                    bword = low | (other_bytes << 16) if extract == "H0" else other_bytes | (low << 16)
                    cword = scales | (other_scales << 16)   # Rc.H0 (iswzC); extract_B picks only Rb's half
                    fields = dict(Rd=8, dstfmt="BF16", srcfmt=fmt, merge="UNPACK_B", rndMode="RN", relu=relu, satfinite=satfinite, extract_B=extract, iswzC="H0")
                    regs = {}
                    into(k, bsrc, bword, fields, regs, 2); into(k, csrc, cword, fields, regs, 4)
                    lanes = ((code0, scale0), (code1, scale1))
                    c.add(k.name, fields, regs, {"R8": Later(scaled_halves, fmt, lanes, relu == "RELU", satfinite == "SATFINITE"), "__native": True})


@anchored('Format-specific threshold/exhaustive, scaled-format and literal encoding-guard corpora exceed the ordinary pack profile.')
def e3m4_up(c, part):
    for k in c.named("f2fp_8b_upconvert__"):
        src = source(k, "Rb", "URb", "Sb")
        if src is None or not c.labels(k, "srcfmt", ("E3M4",)): continue
        for extract, relu in itertools.product(("H0", "H1"), ("norelu", "RELU")):
            for code in part.sweep(k, 256):
                packed = code | ((code ^ 0xff) << 8)
                fields, regs = dict(Rd=8, dstfmt="F16", srcfmt="E3M4", merge="UNPACK_B", rndMode="RN", relu=relu, extract_B=extract), {}
                into(k, src, packed if extract == "H0" else packed << 16, fields, regs, 2)
                # The selected half is the byte pair whose low byte is `code`.
                c.add(k.name, fields, regs, {"R8": Later(widened, "E3M4", (code, code ^ 0xff), relu != "norelu"), "__native": True})


def emulator_cases(c):
    tf32(c); pack16(c); fp8_pack(c); upconvert(c); fp4_pack(c)
    if c.arch in NEW:
        fp8_down(c); fp6_down(c); fp4_down(c); mx8_down(c); scaled_8b_down(c); scaled_up(c); e3m4_up(c)


def dataflow_words(arch):
    """Representative words of the focused SM100+ forms, for the register-dataflow check."""
    if arch not in NEW: return []
    c = Cases(arch); words = []
    word = lambda k, fields: bytes.fromhex(encode(arch, k.name, **fields))
    for k in c.named("f2fp_f32_to_4b_downconvert__"):
        fields, regs = dict(Rd=8, dstfmt="E2M1", srcfmt="F32", merge="PACK_AB_MERGE_C", rndMode="RN", satfinite="SATFINITE", relu="RELU"), {}
        for group, reg, value in (("Ra/URa", 2, 0xc0800000), ("Rb/URb/Sb", 4, 0x40200000), ("Rc/URc/Sc", 6, 0xdeadbeef)): into(k, group, value, fields, regs, reg)
        words.append(word(k, fields))
    for k in c.named("f2fp_f32_to_8b_downconvert__", "uf2fp_f32_to_8b_downconvert__", "f2fp_f16_to_8b_downconvert__", "uf2fp_f16_to_8b_downconvert__"):
        half = "_f16_to_8b_" in k.name
        fields = dict(dstfmt="E4M3", srcfmt="F16" if half else "F32", merge="UNPACK_B_MERGE_C" if half else "PACK_AB_MERGE_C",
                      rndMode="RN", satfinite="SATFINITE", relu="RELU", extract="H1", **{dest(k): 8})
        regs = {}
        if half: into(k, "Rb/URb/Sb", 0xc0803c00, fields, regs, 2)
        else: into(k, "Ra/URa", 0xc0800000, fields, regs, 2); into(k, "Rb/URb/Sb", 0x3f800000, fields, regs, 4)
        into(k, "Rc/URc/Sc", 0xdeadbeef, fields, regs, 6)
        words.append(word(k, fields))
    words += [word(k, fp6_probe(k, "SATFINITE", "RELU", "H1")[0]) for k in fp6_classes(c)]
    mx8 = Cases(arch); mx8_down(mx8); mx8.complete()
    seen = {}
    for case in mx8:
        if "__native" in case.expected: seen.setdefault(case.cls, bytes.fromhex(case.request["word"]))
    words += list(seen.values())
    for i, k in enumerate(c.named("f2fp_8b_upconvert_scale__")):
        fmts = c.labels(k, "srcfmt", ("E5M2", "E4M3", "E2M3", "E3M2", "E3M4", "S2_6"))
        fields = dict(Rd=8, dstfmt="BF16", srcfmt=fmts[i % len(fmts)], merge="UNPACK_B", rndMode="RN",
                      relu="RELU", satfinite="SATFINITE", extract_B="H1")
        regs = {}
        into(k, source(k, "Rb", "URb", "Sb"), 0x5a3c9f81, fields, regs, 2); into(k, source(k, "Rc", "URc", "Sc"), 0x7f00fe01, fields, regs, 4)
        words.append(word(k, fields))
    return words


def gpu_fixtures(arch="SM89"):
    from tests.harness import scalar
    from tests.harness.gpu import Fixture, Template, compile, encode
    from tests.harness.templates import cx_fixture, rows
    out = []
    for f in (f for f in scalar.TARGETS if f.startswith(("f2fp_f16_pack_", "f2fp_bf16_pack_"))):
        fmt = "BF16" if f.startswith("f2fp_bf16") else "F16"
        cvt = lambda x, fmt=fmt, f=f: fpformat.pack16(x, fmt, "RZ" if f.endswith("_rz") else "RN", "satfinite" in f, "_relu" in f)
        # The immediate and constant-bank forms convert 1.5 into the low half.
        ref = (lambda x, y, z, cvt=cvt: (cvt(x) << 16) | cvt(0x3fc00000)) if f in ("f2fp_f16_pack_imm", "f2fp_f16_pack_c") else (lambda x, y, z, cvt=cvt: (cvt(x) << 16) | cvt(y))
        out.append(scalar.launch(f, ref, arch=arch))
    for f in (f for f in scalar.TARGETS if f.startswith(("f2fp_e4m3_pack", "f2fp_e5m2_pack"))):
        fmt, sat = f[5:9].upper(), not f.endswith("_nosat")
        out.append(scalar.launch(f, lambda x, y, z, fmt=fmt, sat=sat: ((z >> 16) << 16) | (fpformat.fp8(x, fmt, "RN", sat) << 8) | fpformat.fp8(y, fmt, "RN", sat), arch=arch))
    for f in (f for f in scalar.TARGETS if f.startswith(("f2fp_e4m3_unpack_", "f2fp_e5m2_unpack_"))):
        fmt = f[5:9].upper()
        relu = (lambda h: 0 if h & 0x8000 else h) if f.endswith("_relu") else (lambda h: h)
        half = lambda w, fmt=fmt, relu=relu: relu(fpformat.widen(fmt, w & 0xff)) | (relu(fpformat.widen(fmt, (w >> 8) & 0xff)) << 16)
        out.append(scalar.launch(f, lambda w, *_, half=half: half(w) | (half(w >> 16) << 32), arch=arch))
    # F2FP PACK_AB with CX source B (every lane reads the first record's B).
    rng = random.Random(0xf2f0)
    vals = [0, 0x80000000, 0x3f800000, 0xbf800000, 0x3fc00000, 0xc0200000, 0x387fc000, 0x38800000, 0x477fe000, 0x477ff000, 0x33000000, 0xb3800000]
    vals += [int.from_bytes(struct.pack("<f", rng.uniform(-500.0, 500.0)), "little") for _ in range(116)]
    out.append(cx_fixture("f2fp_cx", "f2fp__RCxR", dict(Rd=7, Ra=2, dstfmt="F16", srcfmt="F32", merge="PACK_AB", rndMode="RN", relu="norelu", satfinite="nosatfinite"),
                          list(zip(vals, vals[1:] + vals[:1], [0] * 128)), [(fpformat.f32_to_f16(a) << 16) | fpformat.f32_to_f16(vals[1]) for a in vals], arch=arch))
    # MERGE_C with a CX conversion source.
    sources = (0x3f800000, 0xbf800000, 0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x7fc12345, 0xff812345)
    bases = (0xdeadbeef, 0xaabbccdd, 0x00017fff, 0x7bff8000, 0x7fc17e01, 0x3f803f80, 0xffff0001, 0x80017fff)
    specs = itertools.product(("F16", "BF16", "E6M9"), ("RN", "RZ"), (False, True), (False, True), ("H0", "H1"))
    for i, (fmt, rnd, relu, sat, extract) in enumerate(specs):
        src = sources[i % len(sources)]
        low = fpformat.pack16(src, fmt, rnd, sat, relu)
        out.append(cx_fixture(f"merge_c_cx_{i}", "f2fp_merge_c__RCxR", dict(Rd=7, Rc=2, dstfmt=fmt, srcfmt="F32", merge="MERGE_C", rndMode=rnd,
                              relu="RELU" if relu else "norelu", satfinite="SATFINITE" if sat else "nosatfinite", extract=extract, Pg=7),
                              [(bases[j % len(bases)], src if j == 0 else 0x3f800000, 0) for j in range(128)],
                              [(((bases[j % len(bases)] >> (16 if extract == "H1" else 0)) & 0xffff) << 16) | low for j in range(128)],
                              arch=arch))
    # F2FP.E6M9 patched into the compiled F16 pack kernels (register, immediate and constant B).
    image = compile("scalar_semantics", "-diag-suppress=177", arch=arch)
    rng = random.Random(0xe6a9)
    edge = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x3f000000, 0x40000000,
            0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc00000, 0x7f800001, 0x7fffffff]
    for value in (2.0 ** -40, 2.0 ** -39, 2.0 ** -30, 2.0 ** 32 - 2.0 ** 22, 2.0 ** 32 - 2.0 ** 21, 2.0 ** 32):
        bits = struct.unpack("<I", struct.pack("<f", value))[0]; edge += [bits - 1, bits, bits + 1]
    vals = edge + [rng.getrandbits(32) for _ in range(512)]
    while len(vals) % 32: vals.append(rng.getrandbits(32))
    triples = [(a, vals[(i * 73 + 19) % len(vals)], 0xdeadbeef) for i, a in enumerate(vals)]
    for wrapper, form in (("f2fp_f16_pack_rn", "f2fp__RRR"), ("f2fp_f16_pack_imm", "f2fp__RIR"), ("f2fp_f16_pack_c", "f2fp__RUR" if arch == "SM90" else "f2fp__RCR")):
        t = Template(image, "sem_" + wrapper)
        offset, = [o for o in range(0, len(t.code), 16) if t.decoded(o).klass.mnemonic == "F2FP"]
        d = t.decoded(offset)
        if d.klass.name != form: raise RuntimeError(f"{wrapper}: unexpected form {d.klass.name}")
        for rnd, sat, relu in itertools.product(("RN", "RZ"), (False, True), (False, True)):
            fields = {n: v for n, v in d.env.items() if isinstance(n, str) and n in d.klass.operand_types}
            fields.update(dstfmt="E6M9", rndMode=rnd, satfinite="SATFINITE" if sat else "nosatfinite", relu="RELU" if relu else "norelu")
            cvt = lambda x, rnd=rnd, sat=sat, relu=relu: fpformat.pack16(x, "E6M9", rnd, sat, relu)
            out.append(Fixture(name=f"e6m9_{wrapper}_{rnd}_{int(sat)}_{int(relu)}", image=t.patch({offset: (encode(form, arch=arch, **fields), t.control(offset))}),
                               kernel="sem_" + wrapper, data=rows(triples), arguments=((4, 0x3fc00000),) if wrapper == "f2fp_f16_pack_c" else (),
                               expected=[(cvt(a) << 16) | cvt(b if wrapper == "f2fp_f16_pack_rn" else 0x3fc00000) for a, b, _ in triples],
                               exit=True))
    return out
