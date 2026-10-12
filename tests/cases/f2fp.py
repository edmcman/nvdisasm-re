"""F2FP conversions: TF32; F32 -> F16/BF16/E6M9 pack and MERGE_C; FP8/FP6/FP4 downconversion
(SATFINITE, RELU, RN/RZ/RP, uniform destinations); 8-bit, 4-bit and UE8M0 upconversion,
plain and scaled; S2_6 scaled fixed point; CX sources; opaque fallbacks."""
import itertools, random, struct
from sass.pyencode import encode
from tests.harness.case import Case, Cases, cx_source, put, source
from tests.refs import fpformat as fpf

NEW = ("SM100", "SM101", "SM103", "SM120")  # where the focused SM100+ conversion suites run
FP6 = ("E2M3", "E3M2")


def into(k, group, value, fields, regs, reg):
    """Place value in the first operand of `group` ("Rb/URb/Sb"): R/UR number `reg`, else literal."""
    name = source(k, *group.split("/"))
    put(k, name, value, fields, regs, None, reg, imm=(k.operand_types[name].type,))
    return name


def has(k, *operands): return all(n in k.operand_types for n in operands)


def dest(k): return "URd" if k.name.startswith("uf2fp_") else "Rd"


def observed(k): return ("UR" if dest(k) == "URd" else "R") + "8"


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
                result = fpf.tf32(bits, rnd == "RN")
                c.add(k.name, fields, regs, {"R8": result, "__native": True}, memory=memory)
                relu = 0x7fffffff if result & 0x7f800000 == 0x7f800000 and result & 0x7fffff else 0 if result & 0x80000000 else result
                c.add(k.name, dict(fields, relu="RELU"), regs, {"R8": relu, "__native": True}, memory=memory)


PACKED = [0, 0x80000000, 0x3f800000, 0xbf800000, 0x3f801000, 0x3f803000, 0x33000000, 0x33800000, 0x387fc000, 0x38800000,
          0x477fe000, 0x477ff000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 1, 0x007fffff, 0x00800000, 0x7f7fffff, 0xff7fffff]
PACKED += [struct.unpack("<I", struct.pack("<f", v))[0] for v in (2.0 ** -41, 2.0 ** -40, 2.0 ** -39, 2.0 ** -30, -2.0 ** -40,
                                                                  2.0 ** 31, 2.0 ** 32 - 2.0 ** 22, 2.0 ** 32 - 2.0 ** 21, 2.0 ** 32)]


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
                conv = lambda bits: fpf.pack16(bits, fmt, rnd, satfinite == "SATFINITE", relu == "RELU")
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
                    conv = lambda bits: fpf.pack16(bits, "E6M9", rnd, satfinite == "SATFINITE", relu == "RELU")
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
                    half = fpf.pack16(bits, fmt, rnd, satfinite == "SATFINITE", relu == "RELU")
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
                    c.add(k.name, fields, regs, {"R6": (half << 16) | (fpf.fp8(av, fmt) << 8) | fpf.fp8(bv, fmt), "__native": True}, memory=memory)


def upconvert(c):
    # F2FP.F16.{E4M3,E5M2,E2M3,E3M2,E3M4}.UNPACK_B widens both bytes of the selected half.
    for k in c.of("F2FP", "f2fp_8b_upconvert__"):
        src = source(k, "Rb", "URb", "Sb")
        if src is None or k.operand_types[src].type not in ("Register", "UniformRegister") or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "relu", "extract_B"): continue
        reg = 2 if k.operand_types[src].type == "Register" else 8
        for fmt in c.labels(k, "srcfmt", ("E4M3", "E5M2", "E2M3", "E3M2", "E3M4")):
            top = 63 if fmt in FP6 else 255 if fmt == "E3M4" else 127
            for extract in c.labels(k, "extract_B", ("H0", "H1")):
                for relu in ("norelu", "RELU"):
                    for i in range(top + 1):
                        pair = i | ((top - i) << 8)
                        regs, fields = {}, {"Rd": 8, "dstfmt": "F16", "srcfmt": fmt, "merge": "UNPACK_B", "rndMode": "RN", "relu": relu, "extract_B": extract}
                        put(k, src, pair if extract == "H0" else pair << 16, fields, regs, None, reg)
                        lo, hi = fpf.widen(fmt, i), fpf.widen(fmt, top - i)
                        if relu == "RELU": lo, hi = (0 if lo & 0x8000 else lo), (0 if hi & 0x8000 else hi)
                        c.add(k.name, fields, regs, {"R8": lo | (hi << 16), "__native": True})
    # SM100+ E2M1/E0M3 byte upconvert: low nibble -> low F16, high nibble -> high F16.
    for k in c.of("F2FP", "f2fp_4b_upconvert__", cx=None):
        src = source(k, "Rb", "URb", "Sb")
        if src is None or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "relu", "selB"): continue
        for fmt in c.labels(k, "srcfmt", ("E2M1", "E0M3")):
            for selector in c.labels(k, "selB", ("B0", "B1", "B2", "B3")):
                for relu in ("norelu", "RELU"):
                    for byte in range(256):
                        fields, regs = {"Rd": 8, "dstfmt": "F16", "srcfmt": fmt, "merge": "UNPACK_B", "rndMode": "RN", "relu": relu, "selB": selector}, {}
                        put(k, src, byte << (int(selector[1:]) * 8), fields, regs, None, 2, ureg=4, imm=("F32Imm",))
                        lo, hi = fpf.widen(fmt, byte & 0xf), fpf.widen(fmt, byte >> 4)
                        if relu == "RELU": lo, hi = (0 if lo & 0x8000 else lo), (0 if hi & 0x8000 else hi)
                        c.add(k.name, fields, regs, {"R8": lo | (hi << 16), "__native": True})
    # SM100+ scaled FP4 upconvert: per-lane UE8M0 multiplication after unpacking (PTX); low/high
    # nibbles pair with low/high scale bytes.
    for k in c.of("F2FP", "f2fp_4b_upconvert_scale__", cx=None):
        bsrc, csrc = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        if bsrc is None or csrc is None: continue
        for fmt in ("E2M1", "E0M3"):
            for selector in c.labels(k, "selB", ("B0", "B1", "B2", "B3")):
                for relu in ("norelu", "RELU"):
                    for byte in range(256):
                        scales = byte | ((255 - byte) << 8)
                        fields = {"Rd": 8, "dstfmt": "BF16", "srcfmt": fmt, "merge": "UNPACK_B", "rndMode": "RN", "satfinite": "SATFINITE", "relu": relu, "selB": selector}
                        regs = {}
                        put(k, bsrc, byte << (int(selector[1:]) * 8), fields, regs, None, 2, ureg=4, imm=(k.operand_types[bsrc].type,))
                        put(k, csrc, scales, fields, regs, None, 6, ureg=8, imm=(k.operand_types[csrc].type,))
                        lo = fpf.scaled_bf16(byte & 15, fmt, scales & 0xff, relu == "RELU")
                        hi = fpf.scaled_bf16(byte >> 4, fmt, scales >> 8, relu == "RELU")
                        c.add(k.name, fields, regs, {"R8": lo | (hi << 16), "__native": True})
    # SM100+ UE8M0 to BF16x2 (CUDA host reference: 0xff NaN, 0 the 2^-127 subnormal, else << 7).
    for k in c.of("F2FP", "f2fp_E8_upconvert__", cx=None):
        src = source(k, "Rb", "URb", "Sb")
        if src is None or not has(k, "dstfmt", "srcfmt", "merge", "rndMode", "extract_B"): continue
        for extract in ("H0", "H1"):
            for byte in range(256):
                pair = byte | ((255 - byte) << 8)
                fields, regs = {"Rd": 8, "dstfmt": "BF16", "srcfmt": "E8", "merge": "UNPACK_B", "rndMode": "RN", "extract_B": extract}, {}
                put(k, src, pair if extract == "H0" else pair << 16, fields, regs, None, 2, ureg=4, imm=("F32Imm",))
                c.add(k.name, fields, regs, {"R8": fpf.e8m0_to_bf16(byte) | (fpf.e8m0_to_bf16(255 - byte) << 16), "__native": True})


def fp4_pack(c):
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
            for fmt in ("E2M1", "E0M3"):
                for relu in ("norelu", "RELU"):
                    for i, x in enumerate(vals):
                        bword = x | (vals[(i * 7 + 3) % len(vals)] << 16)
                        cword = vals[(i * 11 + 5) % len(vals)] | (vals[(i * 13 + 9) % len(vals)] << 16)
                        fields = {"Rd": 8, "dstfmt": fmt, "srcfmt": src, "merge": "UNPACK_B_MERGE_C", "rndMode": "RN", "satfinite": "SATFINITE", "relu": relu}
                        regs = {}
                        put(k, bsrc, bword, fields, regs, None, 2, ureg=4, imm=(k.operand_types[bsrc].type,))
                        put(k, csrc, cword, fields, regs, None, 6, ureg=8, imm=(k.operand_types[csrc].type,))
                        lanes = [fpf.fp4(w >> (16 * j) & 0xffff, fmt, relu == "RELU", src) for w in (bword, cword) for j in range(2)]
                        c.add(k.name, fields, regs, {"R8": sum(lane << (4 * j) for j, lane in enumerate(lanes)), "__native": True})


# --- Focused SM100+ downconversion suites ------------------------------------------------

def fp8_down(c):
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
        cvt = [fpf.fp8(v, fmt, rnd, satfinite == "SATFINITE", relu == "RELU", srcfmt) for v in (a, b)]
        packed = (cvt[1] << 8) | cvt[0] if half else (cvt[0] << 8) | cvt[1]
        c.add(k.name, fields, regs, {observed(k): ((carry >> 16 if extract == "H1" else carry & 0xffff) << 16) | packed, "__native": True, "__fp8_nan": fmt})
    for k in f32_classes:
        for fmt, relu, extract, rnd in itertools.product(("E4M3", "E5M2"), ("norelu", "RELU"), ("H0", "H1"), ["RN"] + c.labels(k, "rndMode", ("RP",))):
            for i, a in enumerate(sample):
                add(k, fmt, "F32", rnd, relu, extract, "SATFINITE", a, sample[(i * 7 + 3) % len(sample)], (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff)
    for k in half_classes:
        for srcfmt in c.labels(k, "srcfmt", ("F16", "BF16")):
            for fmt, relu, extract, rnd in itertools.product(("E4M3", "E5M2"), ("norelu", "RELU"), ("H0", "H1"), ["RN"] + c.labels(k, "rndMode", ("RP",))):
                vals = fp8_samples(fmt, srcfmt)
                for i, x in enumerate(vals):
                    add(k, fmt, srcfmt, rnd, relu, extract, "SATFINITE", x, vals[(i * 11 + 7) % len(vals)], (0xd35a9c71 ^ (i * 0x01010101)) & 0xffffffff)
    f32_values = (0, 0x80000000, 0x3f800000, 0xbf800000, 0x477fe000, 0x477ff000, 0xc77ff000, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc12345)
    half_values = (0, 0x8000, 0x3c00, 0xbc00, 0x7bff, 0xfbff, 0x7c00, 0xfc00, 0x7e00, 0xfe00)
    for k in f32_classes + half_classes:
        half = k in half_classes
        for srcfmt in c.labels(k, "srcfmt", ("F16", "BF16")) if half else ("F32",):
            samples = half_values if half else f32_values
            for fmt, rnd, relu, extract in itertools.product(("E4M3", "E5M2"), c.labels(k, "rndMode", ("RN", "RP")), ("norelu", "RELU"), ("H0", "H1")):
                for i, x in enumerate(samples):
                    add(k, fmt, srcfmt, rnd, relu, extract, "nosatfinite", x, samples[(i * 7 + 3) % len(samples)], (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff)


def fp8_samples(fmt, srcfmt):
    """Specials, values around every 7th FP8 midpoint, and random 16-bit sources."""
    special = [0, 0x8000, 1, 0x8001, 0x03ff, 0x83ff, 0x0400, 0x8400, 0x7c00, 0xfc00, 0x7e00, 0xfe00]
    finite = [v for _, v in fpf.finite(fmt)]
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


def fp6_down(c):
    """F32/F16/BF16 -> E2M3/E3M2 SATFINITE, RN, with RELU (the md allows FP6 destinations only with
    SATFINITE and RN)."""
    for k in fp6_classes(c):
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
                        ox, oy = (fpf.fp6(v, fmt, rnd, relu == "RELU", srcfmt) for v in (x, y))
                        packed = (oy << 8) | ox if half else (ox << 8) | oy
                        c.add(k.name, fields, regs, {observed(k): ((carry >> 16) & 0xffff) << 16 | packed, "__native": True})


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
    values = [v for _, v in fpf.finite(fmt)]
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


def fp4_down(c):
    """F32 -> E2M1/E0M3 PACK_AB_MERGE_C: PACK_AB writes the low byte, C supplies the upper bits."""
    thresholds = {"E2M1": (0x3e800000, 0x3f400000, 0x3fa00000, 0x3fe00000, 0x40200000, 0x40600000, 0x40a00000),
                  "E0M3": (0x3f000000, 0x3fc00000, 0x40200000, 0x40600000, 0x40900000, 0x40b00000, 0x40d00000)}
    rng = random.Random(0xf2f4)
    for k in c.named("f2fp_f32_to_4b_downconvert__"):
        for fmt in ("E2M1", "E0M3"):
            edges = [0, 0x80000000, 1, 0x80000001, 0x7f800000, 0xff800000, 0x7fc00001, 0xffc12345, 1, 0x007fffff]
            for t in thresholds[fmt]:
                edges.extend((t - 1, t, t + 1, t | 0x80000000, (t - 1) | 0x80000000, (t + 1) | 0x80000000))
            edges.extend(rng.getrandbits(32) for _ in range(20))
            for relu in ("norelu", "RELU"):
                for i, a in enumerate(edges):
                    b, carry = edges[(i * 7 + 5) % len(edges)], (0xa55a1234 ^ (i * 0x01010101)) & 0xffffffff
                    fields = dict(Rd=8, dstfmt=fmt, srcfmt="F32", merge="PACK_AB_MERGE_C", rndMode="RN", satfinite="SATFINITE", relu=relu)
                    regs = {}
                    for group, reg, value in (("Ra/URa", 2, a), ("Rb/URb/Sb", 4, b), ("Rc/URc/Sc", 6, carry)): into(k, group, value, fields, regs, reg)
                    ref = lambda v: fpf.fp4(v, fmt, relu == "RELU", relu_nan=True)
                    c.add(k.name, fields, regs, {"R8": (carry & 0xffffff00) | (ref(a) << 4) | ref(b), "__native": True})


def mx8_down(c):
    """F32/BF16 -> S2_6 with per-lane UE8M0 scales; NOSATFINITE (bit 77) is an opaque fallback."""
    floats = (0, 0x80000000, 0x3f000000, 0x3fc00000, 0xbfc00000, 0x3f400000, 0xbf400000, 0x3f600000, 0xbf600000, 0x3f800000, 0xbf800000,
              0x3ffe0000, 0xbffe0000, 0x40000000, 0xc0000000, 0x3c000000, 0xbc000000, 0x3cc00000, 0xbcc00000, 0x3d200000, 0xbd200000,
              0x7e800000, 0xfe800000, 0x7f000000, 0xff000000, 0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x7fc12345, 0xffc54321)
    scales = (0, 1, 126, 127, 128, 253, 254, 255)
    scale_pairs = tuple((s, scales[(i + 3) % len(scales)]) for i, s in enumerate(scales)) + tuple((s, s) for s in scales)
    first = {}
    for k in c.named("f2fp_f16_to_mx8_downconvert_scale__", "f2fp_f32_to_mx8_downconvert_scale__"):
        bf16 = k.name.startswith("f2fp_f16_")
        for i, a in enumerate(floats):
            b = floats[(i * 7 + 3) % len(floats)]
            if bf16: a, b = a & 0xffff0000, b & 0xffff0000
            for scale_a, scale_b in scale_pairs:
                for relu in ("norelu", "RELU"):
                    # A occupies the high output byte with the high scale byte; B the low.
                    fields = dict(dstfmt="S2_6", srcfmt="BF16" if bf16 else "F32", merge="UNPACK_B_MERGE_C" if bf16 else "PACK_AB_MERGE_C",
                                  rndMode="RN", satnarrow="SATFINITE", relu=relu, Rd=8)
                    regs = {}
                    if bf16: into(k, "Rb/URb/Sb", (a & 0xffff0000) | (b >> 16), fields, regs, 4)
                    else: into(k, "Ra/URa", a, fields, regs, 2); into(k, "Rb/URb/Sb", b, fields, regs, 4)
                    into(k, "Rc/URc/Sc", scale_b | (scale_a << 8), fields, regs, 6)
                    first.setdefault(k.name, (fields, regs))
                    c.add(k.name, fields, regs, {"R8": (fpf.s2_6(a, scale_a, relu == "RELU") << 8) | fpf.s2_6(b, scale_b, relu == "RELU"), "__native": True})
    if c.fp == "exact":
        for cls, (fields, regs) in first.items():
            raw = int.from_bytes(bytes.fromhex(encode(c.arch, cls, **fields)), "little") ^ (1 << 77)
            c.append(Case(cls, dict(word=raw.to_bytes(16, "little").hex(), registers=regs, observe=["R8"], inspect_pcode=True),
                          {"__opaque": "sass_opaque_F2FP("}))


def scaled_up(c):
    """Every byte and scale encoding for 8-bit scaled upconversion to BF16."""
    for k in c.named("f2fp_8b_upconvert_scale__"):
        bsrc, csrc = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        for fmt in c.labels(k, "srcfmt", ("E5M2", "E4M3", "E2M3", "E3M2", "E3M4", "S2_6")):
            for extract, relu, satfinite in itertools.product(("H0", "H1"), ("norelu", "RELU"), ("nosatfinite", "SATFINITE")):
                for i in range(256):
                    # Every byte and scale encoding per lane; the second lane permutes both, and
                    # the unselected half holds different data.
                    code0, scale0 = i, (i * 73 + 19) & 0xff
                    code1, scale1 = (i * 29 + 7) & 0xff, 255 - scale0
                    low, scales = code0 | (code1 << 8), scale0 | (scale1 << 8)
                    other_bytes = ((i ^ 0xa5) & 0xff) | (((i ^ 0x5a) & 0xff) << 8)
                    other_scales = ((i * 11 + 3) & 0xff) | (((i * 17 + 9) & 0xff) << 8)
                    bword = low | (other_bytes << 16) if extract == "H0" else other_bytes | (low << 16)
                    cword = scales | (other_scales << 16) if extract == "H0" else other_scales | (scales << 16)
                    fields = dict(Rd=8, dstfmt="BF16", srcfmt=fmt, merge="UNPACK_B", rndMode="RN", relu=relu, satfinite=satfinite, extract_B=extract)
                    regs = {}
                    into(k, bsrc, bword, fields, regs, 2); into(k, csrc, cword, fields, regs, 4)
                    want = [fpf.scaled_bf16(code, fmt, scale, relu == "RELU", satfinite == "SATFINITE") for code, scale in ((code0, scale0), (code1, scale1))]
                    c.add(k.name, fields, regs, {"R8": want[0] | (want[1] << 16), "__native": True})


def e3m4_up(c):
    for k in c.named("f2fp_8b_upconvert__"):
        src = source(k, "Rb", "URb", "Sb")
        if src is None: continue
        for extract, relu in itertools.product(("H0", "H1"), ("norelu", "RELU")):
            for code in range(256):
                packed = code | ((code ^ 0xff) << 8)
                fields, regs = dict(Rd=8, dstfmt="F16", srcfmt="E3M4", merge="UNPACK_B", rndMode="RN", relu=relu, extract_B=extract), {}
                into(k, src, packed if extract == "H0" else packed << 16, fields, regs, 2)
                lo, hi = fpf.widen("E3M4", code), fpf.widen("E3M4", code ^ 0xff)
                if relu != "norelu": lo, hi = (0 if lo & 0x8000 else lo), (0 if hi & 0x8000 else hi)
                # The selected half is the byte pair whose low byte is `code`.
                c.add(k.name, fields, regs, {"R8": lo | (hi << 16), "__native": True})


def emulator_cases(c):
    tf32(c); pack16(c); fp8_pack(c); upconvert(c); fp4_pack(c)
    if c.arch in NEW:
        fp8_down(c); fp6_down(c); fp4_down(c); mx8_down(c); scaled_up(c); e3m4_up(c)


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
    mx8 = Cases(arch); mx8_down(mx8)
    seen = {}
    for case in mx8:
        if "__native" in case.expected: seen.setdefault(case.cls, bytes.fromhex(case.request["word"]))
    words += list(seen.values())
    for i, k in enumerate(c.named("f2fp_8b_upconvert_scale__")):
        fields = dict(Rd=8, dstfmt="BF16", srcfmt=("E5M2", "E4M3", "E2M3", "E3M2", "E3M4", "S2_6")[i % 6], merge="UNPACK_B", rndMode="RN",
                      relu="RELU", satfinite="SATFINITE", extract_B="H1")
        regs = {}
        into(k, source(k, "Rb", "URb", "Sb"), 0x5a3c9f81, fields, regs, 2); into(k, source(k, "Rc", "URc", "Sc"), 0x7f00fe01, fields, regs, 4)
        words.append(word(k, fields))
    return words


def gpu_fixtures():
    from tests.harness import scalar
    from tests.harness.gpu import Fixture, Template, compile, sm89
    from tests.harness.templates import cx_fixture, rows
    from tests.refs.float import same_halves
    out = []
    for f in (f for f in scalar.TARGETS if f.startswith(("f2fp_f16_pack_", "f2fp_bf16_pack_"))):
        fmt = "BF16" if f.startswith("f2fp_bf16") else "F16"
        cvt = lambda x, fmt=fmt, f=f: fpf.pack16(x, fmt, "RZ" if f.endswith("_rz") else "RN", "satfinite" in f, "_relu" in f)
        # The immediate and constant-bank forms convert 1.5 into the low half.
        ref = (lambda x, y, z, cvt=cvt: (cvt(x) << 16) | cvt(0x3fc00000)) if f in ("f2fp_f16_pack_imm", "f2fp_f16_pack_c") else (lambda x, y, z, cvt=cvt: (cvt(x) << 16) | cvt(y))
        out.append(scalar.launch(f, ref, same=same_halves if fmt == "F16" else None))
    for f in (f for f in scalar.TARGETS if f.startswith(("f2fp_e4m3_pack", "f2fp_e5m2_pack"))):
        fmt, sat = f[5:9].upper(), not f.endswith("_nosat")
        out.append(scalar.launch(f, lambda x, y, z, fmt=fmt, sat=sat: ((z >> 16) << 16) | (fpf.fp8(x, fmt, "RN", sat) << 8) | fpf.fp8(y, fmt, "RN", sat)))
    for f in (f for f in scalar.TARGETS if f.startswith(("f2fp_e4m3_unpack_", "f2fp_e5m2_unpack_"))):
        fmt = f[5:9].upper()
        relu = (lambda h: 0 if h & 0x8000 else h) if f.endswith("_relu") else (lambda h: h)
        half = lambda w, fmt=fmt, relu=relu: relu(fpf.widen(fmt, w & 0xff)) | (relu(fpf.widen(fmt, (w >> 8) & 0xff)) << 16)
        out.append(scalar.launch(f, lambda w, *_, half=half: half(w) | (half(w >> 16) << 32)))
    # F2FP PACK_AB with CX source B (every lane reads the first record's B).
    rng = random.Random(0xf2f0)
    vals = [0, 0x80000000, 0x3f800000, 0xbf800000, 0x3fc00000, 0xc0200000, 0x387fc000, 0x38800000, 0x477fe000, 0x477ff000, 0x33000000, 0xb3800000]
    vals += [int.from_bytes(struct.pack("<f", rng.uniform(-500.0, 500.0)), "little") for _ in range(116)]
    out.append(cx_fixture("f2fp_cx", "f2fp__RCxR", dict(Rd=7, Ra=2, dstfmt="F16", srcfmt="F32", merge="PACK_AB", rndMode="RN", relu="norelu", satfinite="nosatfinite"),
                          list(zip(vals, vals[1:] + vals[:1], [0] * 128)), [(fpf.f32_to_f16(a) << 16) | fpf.f32_to_f16(vals[1]) for a in vals]))
    # MERGE_C with a CX conversion source; GPU F16 NaNs may differ in payload/sign from the reference.
    sources = (0x3f800000, 0xbf800000, 0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x7fc12345, 0xff812345)
    bases = (0xdeadbeef, 0xaabbccdd, 0x00017fff, 0x7bff8000, 0x7fc17e01, 0x3f803f80, 0xffff0001, 0x80017fff)
    specs = itertools.product(("F16", "BF16", "E6M9"), ("RN", "RZ"), (False, True), (False, True), ("H0", "H1"))
    for i, (fmt, rnd, relu, sat, extract) in enumerate(specs):
        src = sources[i % len(sources)]
        low = fpf.pack16(src, fmt, rnd, sat, relu)
        same = (lambda g, w: g == w or g >> 16 == w >> 16 and fpf.is_nan16(g & 0xffff, "F16") and fpf.is_nan16(w & 0xffff, "F16")) if fmt == "F16" else None
        out.append(cx_fixture(f"merge_c_cx_{i}", "f2fp_merge_c__RCxR", dict(Rd=7, Rc=2, dstfmt=fmt, srcfmt="F32", merge="MERGE_C", rndMode=rnd,
                              relu="RELU" if relu else "norelu", satfinite="SATFINITE" if sat else "nosatfinite", extract=extract, Pg=7),
                              [(bases[j % len(bases)], src if j == 0 else 0x3f800000, 0) for j in range(128)],
                              [(((bases[j % len(bases)] >> (16 if extract == "H1" else 0)) & 0xffff) << 16) | low for j in range(128)],
                              **({"same": same, "emulator_same": lambda e, w: e == w} if same else {})))
    # F2FP.E6M9 patched into the compiled F16 pack kernels (register, immediate and constant B).
    image = compile("scalar_semantics", "-diag-suppress=177")
    rng = random.Random(0xe6a9)
    edge = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x3f000000, 0x40000000,
            0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc00000, 0x7f800001, 0x7fffffff]
    for value in (2.0 ** -40, 2.0 ** -39, 2.0 ** -30, 2.0 ** 32 - 2.0 ** 22, 2.0 ** 32 - 2.0 ** 21, 2.0 ** 32):
        bits = struct.unpack("<I", struct.pack("<f", value))[0]; edge += [bits - 1, bits, bits + 1]
    vals = edge + [rng.getrandbits(32) for _ in range(512)]
    while len(vals) % 32: vals.append(rng.getrandbits(32))
    triples = [(a, vals[(i * 73 + 19) % len(vals)], 0xdeadbeef) for i, a in enumerate(vals)]
    for wrapper, form in (("f2fp_f16_pack_rn", "f2fp__RRR"), ("f2fp_f16_pack_imm", "f2fp__RIR"), ("f2fp_f16_pack_c", "f2fp__RCR")):
        t = Template(image, "sem_" + wrapper)
        offset, = [o for o in range(0, len(t.code), 16) if t.decoded(o).klass.mnemonic == "F2FP"]
        d = t.decoded(offset)
        if d.klass.name != form: raise RuntimeError(f"{wrapper}: unexpected form {d.klass.name}")
        for rnd, sat, relu in itertools.product(("RN", "RZ"), (False, True), (False, True)):
            fields = {n: v for n, v in d.env.items() if isinstance(n, str) and n in d.klass.operand_types}
            fields.update(dstfmt="E6M9", rndMode=rnd, satfinite="SATFINITE" if sat else "nosatfinite", relu="RELU" if relu else "norelu")
            cvt = lambda x, rnd=rnd, sat=sat, relu=relu: fpf.pack16(x, "E6M9", rnd, sat, relu)
            out.append(Fixture(name=f"e6m9_{wrapper}_{rnd}_{int(sat)}_{int(relu)}", image=t.patch({offset: (sm89(form, **fields), t.control(offset))}),
                               kernel="sem_" + wrapper, data=rows(triples), arguments=((4, 0x3fc00000),) if wrapper == "f2fp_f16_pack_c" else (),
                               expected=[(cvt(a) << 16) | cvt(b if wrapper == "f2fp_f16_pack_rn" else 0x3fc00000) for a, b, _ in triples],
                               exit=True))
    return out
