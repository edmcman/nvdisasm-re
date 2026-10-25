"""Integer/float conversions: F2I, I2F/I2FP, FRND, F2F, F2IP (F32 pair to saturated bytes) and
I2IP (saturated/truncated integer pack)."""
from tests.harness.manual import retained
from tests.harness import autocase_profiles
from tests.harness.case import cx_source, pair, put
from tests.refs import float
from tests.refs.float import f2i_trunc, f2ip_byte
from tests.refs.integer import i2ip


def cx_name(k): return next(n for n, a in k.operand_types.items() if a.type == "CX")


@retained("Literal encodings and measured F2I NTZ/sign boundaries anchor the independent generated reference; readable truncation is a distinct contract.")
def scalar(c):
    for cls, fields, regs, expected in [
            ("f2i__Rb_32b", dict(Rd=8, Rb=2, dstfmt="S32", rnd="TRUNC"), dict(R2=0x3fc00000), dict(R8=1)),
            ("i2f__Rb_32b", dict(Rd=8, Rb=2), dict(R2=7), dict(R8=0x40e00000)),
            ("i2fp__RRR", dict(Rd=8, Rb=2, srcfmt="S32", dstfmt="F32"), dict(R2=0xfffffffe), dict(R8=0xc0000000)),
            ("i2f__IS_64b", dict(Rd=8, Sb=-1, dstfmt="F32"), {}, dict(R8=0xcf800000)),
            ("f2i__Ib_16b", dict(Rd=8, Sb=0x3e00, rnd="TRUNC"), {}, dict(R8=1)),
            ("f2i__Ib_64b", dict(Rd=8, Sb=0x40080000, rnd="TRUNC"), {}, dict(R8=3))]:
        c.add(cls, fields, regs, expected)
    # F2I.TRUNC.NTZ to S32: NaN gives 0, infinities and finite overflow saturate.
    for bits in (0x7fc00000, 0x7f800001, 0x7f800000, 0xff800000, 0x4effffff, 0x4f000000, 0xcf000000, 0xcf000001,
                 0x3fc00000, 0xbfc00000, 0x3f000000, 0xbf000000, 1, 0x80000001):
        for absolute in (0, 1):
            for negate in (0, 1):
                effective = (bits & 0x7fffffff if absolute else bits) ^ (0x80000000 if negate else 0)
                c.add("f2i__Rb_32b", dict(Rd=8, Rb=2, dstfmt="S32", rnd="TRUNC", ntz="NTZ", **{"Rb@absolute": absolute, "Rb@negate": negate}),
                      dict(R2=bits), dict(R8=f2i_trunc(effective), __native=True))
    if c.fp == "readable":
        for rnd in ("ROUND", "FLOOR", "CEIL"):
            c.add("f2i__Rb_32b", dict(Rd=8, Rb=2, dstfmt="S32", rnd=rnd, ntz="NTZ"), dict(R2=0x3fe00000), dict(R8=1, __native=True))


@retained("Dense byte saturation/NaN thresholds, literal opaque guards and CX layout anchors exceed the generated pack profile.")
def f2ip(c):
    # F2IP rounds two F32 inputs to nearest-even, saturates each to S8/U8, and copies
    # C[15:0] into the high half of the packed result.
    values = [0, 0x80000000, 0x3f000000, 0xbf000000, 0x3fc00000, 0xbfc00000, 0x40200000, 0xc0200000, 0x42ff0000, 0xc3000000, 0x43000000,
              0xc2ff0000, 0x42ff8000, 0xc2ff8000, 0x437f8000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 0x4f000000, 0xcf000000]
    for k in c.of("F2IP"):
        srcs = [next((n for n in group.split("/") if n in k.operand_types), None) for group in ("Ra", "Rb/URb/Sb", "Rc/Sc/URc")]
        if not all(srcs): continue
        for dstfmt in c.labels(k, "dstfmt", ("S8", "U8")):
            signed = dstfmt == "S8"
            for j, a in enumerate(values):
                b, d = values[(j + 7) % len(values)], (0x1357 + j * 0x101) & 0xffffffff
                rd = 2 if j % 2 == 0 else 8
                fields, regs, memory = {"Rd": rd, "dstfmt": dstfmt, "srcfmt": "F32", "rnd": "ROUND", "ntz": "NTZ", "relu": "norelu"}, {}, []
                if all(put(k, n, v, fields, regs, memory, r, ureg=10, bank=(0, {"Sb": 0, "Sc": 4}.get(n, 8)), imm=("F32Imm", "SImm", "UImm"))
                       for n, v, r in zip(srcs, (a, b, d), (2, 4, 6))):
                    c.add(k.name, fields, regs, {f"R{rd}": ((d & 0xffff) << 16) | (f2ip_byte(a, signed) << 8) | f2ip_byte(b, signed), "__native": True},
                          memory=memory)
    for k in c.of("F2IP", cx=True):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        a, b, d = 0x3fc00000, 0x40200000, 0x12345678
        fields = {"Rd": 8, "Ra": 2, "Rc": 6, "dstfmt": "S8", "srcfmt": "F32", "rnd": "ROUND", "ntz": "NTZ", "relu": "norelu"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b); fields.update(cx_fields)
        c.add(k.name, fields, {"R2": a, "R6": d, **cx_regs}, {"R8": ((d & 0xffff) << 16) | (f2ip_byte(a, True) << 8) | f2ip_byte(b, True), "__native": True},
              memory=memory)
    c.add("f2ip__RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, dstfmt="S8", srcfmt="F32", rnd="TRUNC", ntz="NTZ", relu="norelu"),
          dict(R2=0x3f800000, R4=0x40000000, R6=0), dict(error="sass_opaque_F2IP"))


@retained("Destination-width saturation/RELU boundary neighborhoods and explicit CX anchors exceed the generated pack profile.")
def i2ip_case(c):
    # I2IP inserts two saturated or truncated S32 values into a packed word. Sub-byte formats
    # take the unused high bits from C; S16/U16 fill the result and ptxas uses RZ for C.
    for k in c.of("I2IP"):
        srcs = [next((n for n in group.split("/") if n in k.operand_types), None) for group in ("Ra", "Rb/URb/Sb", "Rc")]
        if not all(srcs): continue
        extract_field = next(n for n, x in k.operand_types.items() if x.type in ("EXTRACT", "HSEL", "ONLY24", "ONLY28"))
        sat_field = next(n for n, x in k.operand_types.items() if x.type in ("SATONLY", "SATRELU", "SATRELU_RELU"))
        modes = c.labels(k, sat_field, ("nosatrelu", "nosatrelu_relu", "SAT", "SATRELU", "RELU"))
        for fmt in c.labels(k, "dstfmt", ("S2", "U2", "S4", "U4", "S8", "U8", "S16", "U16")):
            width, signed = int(fmt[1:]), fmt.startswith("S")
            extracts = ("24",) if width == 4 else ("28",) if width == 2 else ("H0", "H1")
            for extract in c.labels(k, extract_field, extracts):
                for mode in modes:
                    if not signed and mode in ("SATRELU", "RELU"): continue
                    sat, relu = mode in ("SAT", "SATRELU"), mode in ("RELU", "SATRELU")
                    lo = -(1 << (width - 1)) if signed else 0
                    hi = (1 << (width - 1)) - 1 if signed else (1 << width) - 1
                    # Truncation, both saturation bounds and RELU around the target range.
                    samples = [0, 1, -1, lo - 1, lo, hi, hi + 1, 0x12345678]
                    for j, a in enumerate(samples):
                        b, d = samples[(j + 3) % len(samples)], (0xa53c91e7 + j * 0x10203) & 0xffffffff
                        rd = 2 if j == 7 else 8
                        fields, regs, memory = {"Rd": rd, "dstfmt": fmt, "srcfmt": "S32", sat_field: mode, extract_field: extract}, {}, []
                        ok = True
                        for n, v, r in zip(srcs, (a, b, d), (2, 4, 6)):
                            if n == "Rc" and width == 16: fields[n] = "RZ"; continue
                            ok = ok and put(k, n, v & 0xffffffff if k.operand_types[n].type in ("Register", "UniformRegister", "C") else v,
                                            fields, regs, memory, r, ureg=10, bank=(0, 0x20), imm=("SImm", "UImm"))
                        if ok:
                            c.add(k.name, fields, regs, {f"R{rd}": i2ip(a, b, d, width, signed, sat, relu, extract), "__native": True}, memory=memory)
    for k in c.of("I2IP", "i2ip__", cx=True):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        extract_field = next(n for n, x in k.operand_types.items() if x.type in ("EXTRACT", "HSEL", "ONLY24", "ONLY28"))
        sat_field = next(n for n, x in k.operand_types.items() if x.type in ("SATONLY", "SATRELU", "SATRELU_RELU"))
        a, b, d = 300, -140, 0x12345678
        for extract in c.labels(k, extract_field, ("H0", "H1")):
            fields = {"Rd": 8, "Ra": 2, "Rc": 6, "dstfmt": "S8", "srcfmt": "S32", sat_field: "SAT", extract_field: extract}
            cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b); fields.update(cx_fields)
            # Saturate each input to S8; H0/H1 selects C's preserved half.
            c.add(k.name, fields, {"R2": a, "R6": d, **cx_regs}, {"R8": i2ip(a, b, d, 8, True, True, False, extract), "__native": True}, memory=memory)


# Keep fixture inputs byte-for-byte identical while the shared named profile owns the corpus.
F16, F32, F64 = (list(autocase_profiles.PROFILES['conversion_boundaries']['floats'][w]) for w in (16,32,64))
INTS = list(autocase_profiles.PROFILES['conversion_boundaries']['fixture_integers'])
MODES = {"ROUND": "RN", "FLOOR": "RM", "CEIL": "RP", "TRUNC": "RZ", "RN": "RN", "RM": "RM", "RP": "RP", "RZ": "RZ"}


@retained('FRND/UFRND width-specific exceptional and rounding corpora exceed their generated form profile; other scalar conversion corpora now live in conversion_boundaries.')
def ieee(c):
    """Retained dense integral-rounding corpus; scalar conversion corpora are generated."""
    def add(k, fields, x, src_w, value, dst_w):
        d, b = (n if n in k.operand_types else "U" + n for n in ("Rd", "Rb"))
        regs = pair(f"{b[:-1]}2", x) if src_w == 64 else {f"{b[:-1]}2": x & 0xffffffff}
        out = pair(f"{d[:-1]}8", value) if dst_w == 64 else {f"{d[:-1]}8": value}
        c.add(k.name, dict(fields, **{d: 8, b: 2}), regs, dict(out, __native=True), portable=True)
    register = lambda k: "Rb" in k.operand_types or "URb" in k.operand_types
    modes = lambda k: [l for l in c.labels(k, "rnd", MODES)]
    for k in c.of("FRND") + c.of("UFRND"):
        fmt = next((l for l in ("F32", "F64") if l in c.labels(k, "fmt", ("F32", "F64"))), None)
        if not register(k) or not fmt: continue
        w = int(fmt[1:])
        for rnd in modes(k):
            for ftz in ("noftz", "FTZ"):
                for x in (F32 if w == 32 else F64)[::1 if ftz == "noftz" else 3]:
                    add(k, dict(fmt=fmt, rnd=rnd, ftz=ftz), x, w, float.frnd(x, w, MODES[rnd], ftz == "FTZ"), w)


def emulator_cases(c):
    scalar(c); ieee(c); f2ip(c); i2ip_case(c)


def ieee_fixtures(arch="SM89"):
    """The scalar_semantics WIDE_KERNEL conversions (one PTX cvt each, compiled to the target
    instruction): 64-bit inputs and outputs, edge values first, compared on the destination's bits."""
    import random
    from tests.harness import scalar
    roundings = {"rn": "RN", "rm": "RM", "rp": "RP", "rz": "RZ"}
    ints = INTS + [0xffef, 0xfff0, 0xffe0, 65504, 65519, 65520, 65536, 0x10001, 2049, 2051, 0xfffff7ff, 0x8000, 0x7fff, 0xabcd8001, 0x100000000000]
    table = []   # (kernel, inputs, reference, target, compared output bits)
    for m, rnd in roundings.items():
        table += [(f"frnd_{m}", F32, lambda x, r=rnd: float.frnd(x & 0xffffffff, 32, r), "FRND", 32),
                  (f"frnd_f64_{m}", F64, lambda x, r=rnd: float.frnd(x, 64, r), "FRND", 64),
                  (f"f2i_s32_f64_{m}", F64, lambda x, r=rnd: float.f2i(x, 64, 32, True, r, False), "F2I", 32),
                  (f"f2i_u64_f64_{m}", F64, lambda x, r=rnd: float.f2i(x, 64, 64, False, r, False), "F2I", 64),
                  (f"f2f_f32_f64_{m}", F64, lambda x, r=rnd: float.f2f(x, 64, 32, r), "F2F", 32),
                  (f"i2f_s64_{m}", ints, lambda x, r=rnd: float.i2f(x, 64, True, 32, r), "I2F", 32),
                  (f"i2f_u64_{m}", ints, lambda x, r=rnd: float.i2f(x, 64, False, 32, r), "I2F", 32),
                  (f"i2f_f64_s64_{m}", ints, lambda x, r=rnd: float.i2f(x, 64, True, 64, r), "I2F", 64),
                  (f"i2f_f16_s32_{m}", ints, lambda x, r=rnd: float.i2f(x, 32, True, 16, r), "I2F", 32)]
        for dst in ("s32", "u32", "s64", "u64"):
            n = int(dst[1:])   # 32-bit destinations compile to .NTZ, 64-bit ones to the plain form
            table.append((f"f2i_{dst}_{m}", F32, lambda x, r=rnd, n=n, s=dst[0] == "s": float.f2i(x & 0xffffffff, 32, n, s, r, n == 32), "F2I", n))
    table += [(f"i2f_u32_{m}", ints, lambda x, r=roundings[m]: float.i2f(x, 32, False, 32, r), "I2F", 32) for m in ("rm", "rp")]
    table += [("i2f_f64_s32", ints, lambda x: float.i2f(x, 32, True, 64, "RN"), "I2F", 64),
              ("i2f_f64_u32", ints, lambda x: float.i2f(x, 32, False, 64, "RN"), "I2F", 64),
              ("i2f_f16_u64", ints, lambda x: float.i2f(x, 64, False, 16, "RN"), "I2F", 32),
              ("i2f_s16", ints, lambda x: float.i2f(x, 16, True, 32, "RN"), "I2F", 32),
              ("i2f_f64_s16", ints, lambda x: float.i2f(x, 16, True, 64, "RN"), "I2F", 64),
              ("f2f_f16_f64", F64, lambda x: float.f2f(x, 64, 16, "RN"), "F2F", 32),
              ("f2f_f64_f32", F32, lambda x: float.f2f(x & 0xffffffff, 32, 64, "RN"), "F2F", 64),
              ("f2f_f64_f16", [x & 0xffff for x in F32] + [0x7c00, 0xfc00, 0x7e01, 0xfd01, 1, 0x8001, 0x7bff], lambda x: float.f2f(x & 0xffff, 16, 64, "RN"), "F2F", 64),
              *[(f"f2i_{d}_f16_{m}", F16, lambda x, n=int(d[1:]), sg=d[0] == "s", r=roundings[m]: float.f2i(x & 0xffff, 16, n, sg, r, n == 32), "F2I", int(d[1:]))
                for d, m in (("s32", "rz"), ("u32", "rm"), ("s64", "rn"), ("u64", "rp"))],
              ("frnd_ftz_rp", F32, lambda x: float.frnd(x & 0xffffffff, 32, "RP", True), "FRND", 32),
              ("f2i_ftz_rp", F32, lambda x: float.f2i(x & 0xffffffff, 32, 32, True, "RP", True, True), "F2I", 32)]
    out = []
    for name, values, reference, target, bits in table:
        rng, mask = random.Random(name), (1 << bits) - 1
        vs = [[values[i] if i < len(values) else rng.getrandbits(64), 0, 0] for i in range(128)]
        out.append(scalar.fixture(name, vs, [reference(v[0]) for v in vs], targets=(target,), wide=True,
                                  same=lambda g, w, m=mask: g & m == w, arch=arch))
    return out


def gpu_fixtures(arch="SM89"):
    from tests.harness import scalar
    from tests.harness.templates import cx_fixture
    from tests.refs.bits import sext
    out = [scalar.launch(f, lambda x, *_, a=a, n=n: f2i_trunc((x & 0x7fffffff if a else x) ^ (0x80000000 if n else 0)), arch=arch)
           for f, a, n in (("f2i", 0, 0), ("f2i_abs", 1, 0), ("f2i_neg", 0, 1), ("f2i_absneg", 1, 1))]
    out.append(scalar.launch("f2ip", lambda x, y, z: ((z & 0xffff) << 16) | (f2ip_byte(x, True) << 8) | f2ip_byte(y, True), arch=arch))
    for f in (f for f in scalar.TARGETS if f.startswith("i2ip_")):
        fmt, *mode = f.split("_")[1:]; width, signed = int(fmt[1:]), fmt[0] == "s"
        sat, relu = {(): (True, False), ("nosat",): (False, False), ("satrelu",): (True, True)}[tuple(mode)]
        out.append(scalar.launch(f, lambda x, y, z, w=width, s=signed, sa=sat, r=relu: i2ip(sext(x, 32), sext(y, 32), z, w, s, sa, r, "H0"), arch=arch))
    out.append(scalar.launch("i2f", lambda x, *_: float.i2f(x, 32, True, 32, "RN"), arch=arch))
    out += ieee_fixtures(arch=arch)
    # CX source B for F2IP and I2IP; Rc aliases Ra.
    a_values = (0, 0x80000000, 0x3f000000, 0xbf000000, 0x3fc00000, 0xbfc00000, 0x40200000, 0xc0200000, 0x42ff0000, 0xc3000000, 0x43000000,
                0xc2ff0000, 0x42ff8000, 0xc2ff8000, 0x437f8000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 0x4f000000, 0xcf000000)
    values = [a_values[i % len(a_values)] for i in range(128)]
    for dstfmt in ("S8", "U8"):
        for cx in (0x3fc00000, 0xc0200000, 0x42ff8000, 0xcf000000, 0x7fc00000, 0x7f800000, 0xbf000000, 0x3f000000):
            out.append(cx_fixture(f"f2ip_cx_{dstfmt}_{cx:x}", "f2ip__RCxR", dict(Rd=7, Ra=2, Rc=2, Pg=7, dstfmt=dstfmt, srcfmt="F32", rnd="ROUND", ntz="NTZ", relu="norelu"),
                                  [(a, cx, 0) for a in values], [((a & 0xffff) << 16) | (f2ip_byte(a, dstfmt == "S8") << 8) | f2ip_byte(cx, dstfmt == "S8") for a in values], arch=arch))
    ints = (0x12345678, 0x87654321, 0x0000ffff, 0xffff0000, 0, 1, -1, -3, 3, -9, 7, 8, 15, 16, 127, -128, 255, -256, 32767, -32768, 0x12345678, -0x1234567)
    values = [ints[i % len(ints)] & 0xffffffff for i in range(128)]
    for fmt in ("S8", "U8", "S16", "U16"):
        for mode in ("nosatrelu", "SAT", "SATRELU") if fmt[0] == "S" else ("nosatrelu", "SAT"):
            for extract in ("H0", "H1"):
                for cx in (0x12345678, 0xa5c391e7):
                    want = [i2ip(sext(a, 32), sext(cx, 32), a, int(fmt[1:]), fmt[0] == "S", mode in ("SAT", "SATRELU"), mode == "SATRELU", extract) for a in values]
                    out.append(cx_fixture(f"i2ip_cx_{fmt}_{mode}_{extract}_{cx:x}", "i2ip__RCxR_RCxR",
                                          dict(Rd=7, Ra=2, Rc=2, Pg=7, dstfmt=fmt, srcfmt="S32", satrelu=mode, extract_limited=extract),
                                          [(a, cx, 0) for a in values], want, arch=arch))
    return out
