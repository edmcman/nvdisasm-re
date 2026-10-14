"""Packed FP16: HADD2/HFMA2/HMUL2 lanes, broadcasts and modifiers; HMNMX2 (F16/BF16/E6M9,
NaN, FTZ, XORSIGN, IS_A source predicates); HADD2.F32 widening; HFMA2.MMA constant moves."""
import itertools, random
from itertools import product
from sass.semantics.fp import softfloat
from tests.harness.case import cbank, cx_header, cx_source, put, ram, source
from tests.refs.float import half2, half_minmax, hmul2, readable, same_halves, underflow_band, widen_half

SWIZZLES = ("H1_H0", "H0_H0", "H1_H1")


def lanes(value, swizzle="H1_H0", absolute=False, negate=False):
    """Two half lanes after modifiers (which act on both sign bits) and the broadcast."""
    if absolute: value &= 0x7fff7fff
    if negate: value ^= 0x80008000
    lo, hi = value & 0xffff, value >> 16
    return {"H0_H0": [lo, lo], "H1_H1": [hi, hi]}.get(swizzle, [lo, hi])


def reference(op, inputs, sat=False):
    """Per-lane softfloat result; None where a NaN or the emulator underflow band makes it moot."""
    result = 0
    for lane in (0, 1):
        xs = [v[lane] for v in inputs]; want = softfloat.arithmetic(op, xs, 16, sat=sat)
        if softfloat.unpack(want, 16)[0] == "nan": return None
        if not sat and underflow_band(op, xs, 16): return None
        result |= want << (16 * lane)
    return result


def cx_name(k): return next(n for n, a in k.operand_types.items() if a.type == "CX")


def arithmetic(c):
    rng = random.Random("fp16")
    packed = [0, 0x80008000, 0x00010001, 0x80010001, 0x03ff0400, 0x3c004000, 0xbc00c000, 0x7bfffbff, 0x7c003c00, 0x3c007e00, 0x3c014001]
    packed += [rng.getrandbits(32) for _ in range(15)]
    sat_field = "satrelu" if "hfma2__RRR" in c.classes and "satrelu" in c.classes["hfma2__RRR"].operand_types else "sat"
    for i, x in enumerate(packed):
        y, z = packed[(i * 7 + 3) % len(packed)], packed[(i * 5 + 1) % len(packed)]
        for swizzle in SWIZZLES:
            for absolute, negate in ((0, 0), (1, 0), (0, 1), (1, 1)):
                for sat in (False, True):
                    mods = {"Ra@absolute": absolute, "Ra@negate": negate}
                    a = lanes(x, swizzle, absolute, negate)
                    for op, cls, fields, inputs in [
                            ("add", "hadd2__RR", dict(Rd=2, Ra=2, Rc=4, iswzA=swizzle, iswzB_as_C=swizzle, sat="SAT" if sat else "nosat"), [a, lanes(y, swizzle)]),
                            ("fma", "hfma2__RRR", dict(Rd=2, Ra=2, Rb=4, Rc=6, iswzA=swizzle, iswzB=swizzle, iswzC=swizzle, **{sat_field: "SAT" if sat else "nosat"}),
                             [a, lanes(y, swizzle), lanes(z, swizzle)])]:
                        want = reference(op, inputs, sat)
                        if want is not None: c.add(cls, dict(fields, **mods), dict(R2=x, R4=y, R6=z), dict(R2=want, __native=True))
    # Distinct broadcasts on each input and asymmetric immediate/constant lanes.
    x, y, z = 0x3c004000, 0x42004400, 0x38003c00
    for a, b, d in product(SWIZZLES, repeat=3):
        c.add("hfma2__RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, iswzA=a, iswzB=b, iswzC=d), dict(R2=x, R4=y, R6=z),
              dict(R8=reference("fma", [lanes(x, a), lanes(y, b), lanes(z, d)]), __native=True))
    for cls, fields in [("hadd2__RI", dict(Rd=8, Ra=2, Sc=y >> 16, Sc1=y & 0xffff)), ("hfma2__RIR", dict(Rd=8, Ra=2, Sb=y >> 16, Sb1=y & 0xffff, Rc=6)),
                        ("hfma2__RRI", dict(Rd=8, Ra=2, Rb=4, Sc=z >> 16, Sc1=z & 0xffff)), ("hadd2__RC", dict(Rd=8, Ra=2, Sc_bank=0, Sc_addr=0x100)),
                        ("hfma2__RCR", dict(Rd=8, Ra=2, Sb_bank=0, Sb_addr=0x100, Rc=6)), ("hfma2__RRC", dict(Rd=8, Ra=2, Rb=4, Sc_bank=0, Sc_addr=0x104)),
                        ("hadd2__RU", dict(Rd=8, Ra=2, URc=4)), ("hfma2__RRU", dict(Rd=8, Ra=2, Rb=4, URc=6))]:
        fma = cls.startswith("hfma")
        want = reference("fma" if fma else "add", [lanes(x), lanes(y)] + ([lanes(z)] if fma else []))
        c.add(cls, fields, dict(R2=x, R4=y, R6=z, UR4=y, UR6=z), dict(R8=want, __native=True), memory=[cbank(0, 0x100, y | (z << 32), 8)])
    # HMUL2: each operand encoding, lane broadcast, source modifiers and destination overlap.
    values = [0x3c004000, 0x00010001, 0x80010001, 0x7bff3c00, 0x040003ff, 0xbc00c000, 0x35553aaa]
    for k in c.of("HMUL2", cx=None):
        src = source(k, "Rb", "Sb", "URb")
        if not src: continue
        typ = k.operand_types[src].type
        for i, x in enumerate(values):
            y = values[(i * 3 + 2) % len(values)]
            for sa, sb in (("H1_H0", "H1_H0"), ("H0_H0", "H1_H1"), ("H1_H1", "H0_H0")):
                fields = {"Rd": 2, "Ra": 2 if i % 2 == 0 else 4, "iswzA": sa, "ofmt": "F16_V2", "fmz": k.operand_types["fmz"].default,
                          "sat": "nosat", "Ra@absolute": int(bool(i & 1)), "Ra@negate": int(bool(i & 2))}
                regs, memory = {"R2": x, "R4": x}, []
                if typ in ("F16Imm", "UImm"): fields.update(Sb=y >> 16, Sb1=y & 0xffff)
                elif src == "Rb" or typ in ("UniformRegister", "C"):
                    put(k, src, y, fields, regs, memory, 6, bank=(0, 0x100)); fields["iswzB"] = sb
                else: continue
                a, b = lanes(x, sa, bool(i & 1), bool(i & 2)), lanes(y, sb if "iswzB" in k.operand_types else "H1_H0")
                want = sum(softfloat.arithmetic("mul", [a[lane], b[lane]], 16) << (16 * lane) for lane in (0, 1))
                c.add(k.name, fields, regs, dict(R2=want, __native=True), memory=memory)
    c.add("hadd2__RR", dict(Rd=8, Ra=2, Rc=4, ftz="FTZ"), {}, dict(error="sass_opaque_HADD2"))
    c.add("hfma2__RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmz="FMZ"), {}, dict(error="sass_opaque_HFMA2"))

    for mode, sat in product(("FMZ", "FTZ"), (False, True)):
        for a, b in ((0x00010001, 0x60006000), (0x80018001, 0x60006000), (0x14001400, 0x14001400),
                     (0x94009400, 0x14001400), (0x7e017c00, 0x3c003c00), (0xbc00c000, 0x40004000)):
            c.add("hmul2__RR", dict(Rd=8, Ra=2, Rb=4, fmz=mode, sat="SAT" if sat else "nosat"), dict(R2=a, R4=b),
                  dict(R8=hmul2(a, b, mode if c.fp == "exact" else None, sat), __native=True))
    # CX sources apply to both lowerings.
    for mnemonic, cls, fields, regs, want in [("HADD2", "hadd2__RCx", {"Rd": 8, "Ra": 2}, {"R2": 0x3c003c00}, 0x42004200),
                                              ("HFMA2", "hfma2__RCxR", {"Rd": 8, "Ra": 2, "Rc": 6}, {"R2": 0x3c003c00, "R6": 0x42004200}, 0x45004500),
                                              ("HMUL2", "hmul2__RCx", {"Rd": 8, "Ra": 2}, {"R2": 0x3c003c00}, 0x40004000),
                                              ("HMNMX2", "hmnmx2__RCxR", {"Rd": 8, "Ra": 2, "Pp": "PT"}, {"R2": 0x3c003c00}, 0x3c003c00)]:
        for k in (k for k in c.of(mnemonic, cx=True) if k.name == cls):
            cx_fields, cx_regs, memory = cx_source(c.arch, k, cx_name(k), 0x40004000)
            c.add(k.name, dict(fields, **cx_fields), dict(regs, **cx_regs), {"R8": want, "__native": True}, memory=memory, portable=True)


def minmax(c):
    if "hmnmx2__RRR" in c.classes:
        rrr = c.classes["hmnmx2__RRR"]; has_xor = "xorsign" in rrr.operand_types
        for a, b in [(0x3c003c00, 0xc000c000), (0x80000000, 0), (0x7e017c00, 0x40003c00), (0x00010001, 0x80018001),
                     (0xbc003c00, 0x4200c000), (0xffc17fc1, 0x7fc17fc1)]:
            for is_min, nan, ftz in product((False, True), repeat=3):
                # XORSIGN encodings require both input absolute modifiers.
                use_xor = has_xor and (a, b) == (0x3c003c00, 0xc000c000) and is_min and not nan and not ftz
                fields = dict(Rd=8, Ra=2, Rb=4, Pp="PT", nan="NAN" if nan else "nonan", ftz="FTZ" if ftz else "noftz",
                              iswzA="H1_H0", iswzB="H1_H0", **{"Pp@not": int(not is_min)})
                if use_xor: fields.update(xorsign="XORSIGN", **{"Ra@absolute": 1, "Rb@absolute": 1})
                elif has_xor: fields["xorsign"] = "noxorsign"
                c.add("hmnmx2__RRR", fields, {"R2": a, "R4": b}, {"R8": half_minmax(a, b, is_min, nan, ftz, use_xor), "__native": True})
        if has_xor:
            for fmt, a, b in (("F16_V2", 0x80010000, 0x7e017e01), ("BF16_V2", 0x80010000, 0x7fc17fc1), ("E6M9_V2", 0x80010000, 0x7e017e01)):
                if fmt not in c.enum(rrr, "ofmt"): continue
                for is_min in (False, True):
                    fields = dict(Rd=8, Ra=2, Rb=4, Pp="PT", ofmt=fmt, nan="nonan", ftz="noftz", xorsign="XORSIGN", iswzA="H1_H0", iswzB="H1_H0",
                                  **{"Pp@not": int(not is_min), "Ra@absolute": 1, "Rb@absolute": 1})
                    c.add("hmnmx2__RRR", fields, {"R2": a, "R4": b}, {"R8": half_minmax(a, b, is_min, xorsign=True, fmt=fmt[:-3]), "__native": True})
        xor_ok = has_xor and "XORSIGN" in c.enum(rrr, "xorsign")
        for fmt, vectors, xor_rule in (
                ("BF16_V2", [(0x3f803f80, 0xc000c000), (0, 0x80008000), (0x7fc17f80, 0x40003f80), (0x7f80ff80, 0x7f7fff7f), (0xbf803f80, 0x4200c000), (0xffc17fc1, 0x7fc17fc1)],
                 lambda a, nan, is_min: not nan and is_min and a in (0x3f803f80, 0xbf803f80)),
                ("E6M9_V2", [(0x40004000, 0x3f803f80), (0, 0x80008000), (0x7fff7fff, 0x3f803f80), (0x7e00fe00, 0x3f80bf80), (0x00010001, 0x80028002),
                             (0xc0003f80, 0x41003e00), (0xffff7fff, 0x7fff7fff)],
                 lambda a, nan, is_min: not nan and is_min)):
            if fmt not in c.enum(rrr, "ofmt"): continue
            for a, b in vectors:
                for is_min, nan, use_xor in product((False, True), (False, True), (False, True) if xor_ok else (False,)):
                    if use_xor and not xor_rule(a, nan, is_min): continue
                    fields = dict(Rd=8, Ra=2, Rb=4, Pp="PT", ofmt=fmt, nan="NAN" if nan else "nonan", ftz="noftz", iswzA="H1_H0", iswzB="H1_H0",
                                  **{"Pp@not": int(not is_min)})
                    if use_xor: fields.update(xorsign="XORSIGN", **{"Ra@absolute": 1, "Rb@absolute": 1})
                    elif xor_ok: fields["xorsign"] = "noxorsign"
                    c.add("hmnmx2__RRR", fields, {"R2": a, "R4": b}, {"R8": half_minmax(a, b, is_min, nan, False, use_xor, fmt=fmt[:-3]), "__native": True})
        if "hmnmx2__RIR" in c.classes:
            rir_xor = "xorsign" in c.classes["hmnmx2__RIR"].operand_types
            for swizzle in SWIZZLES:
                fields = dict(Rd=8, Ra=2, Sb=0x4200, Sb1=0x3e00, Pp="PT", iswzA=swizzle, nan="nonan", ftz="noftz", **({"xorsign": "noxorsign"} if rir_xor else {}))
                a = lanes(0x40003c00, swizzle); a = a[0] | a[1] << 16
                c.add("hmnmx2__RIR", fields, {"R2": 0x40003c00}, {"R8": half_minmax(a, 0x42003e00, True), "__native": True})
            if rir_xor:
                for src, abs_a in ((0xc000c000, 0), (0xc000c000, 1), (0x40004000, 0), (0x40004000, 1), (0xbc00bc00, 1)):
                    fields = dict(Rd=8, Ra=2, Sb=0x3c00, Sb1=0x3c00, Pp="PT", iswzA="H1_H0", nan="nonan", ftz="noftz", xorsign="XORSIGN", **{"Ra@absolute": abs_a})
                    c.add("hmnmx2__RIR", fields, {"R2": src}, {"R8": half_minmax(src, 0x3c003c00, True, xorsign=True, abs_a=abs_a, abs_b=False), "__native": True})
        for cls in ("hmnmx2__RUR", "hmnmx2__RCR"):
            if cls not in c.classes: continue
            a, b = 0x40003c00, 0x42003e00
            fields = dict(Rd=8, Ra=2, Pp="PT", iswzA="H1_H0", iswzB="H1_H0", nan="nonan", ftz="noftz")
            if "xorsign" in c.classes[cls].operand_types: fields["xorsign"] = "noxorsign"
            regs, memory = {"R2": a}, []
            put(c.classes[cls], source(c.classes[cls], "URb", "Sb"), b, fields, regs, memory, 4, bank=(0, 0x100))
            c.add(cls, fields, regs, {"R8": half_minmax(a, b, True), "__native": True}, memory=memory)
    # NVIDIA's HMNMX2 patent describes Pu/Pv as per-lane source indicators; IS_A enables
    # them and true denotes source A.
    for k in c.of("HMNMX2", "hmnmx2_pred__", cx=None):
        src = next((n for n, a in k.operand_types.items() if a.type == "CX"), None) or source(k, "Rb", "URb", "Sb")
        if src is None or "IS_A" not in c.enum(k, "isA"): continue
        typ = k.operand_types[src].type

        def operand(b, fields, regs):
            """Place b in the second source; returns its memory or None if unsupported."""
            match typ:
                case "Register" | "UniformRegister": put(k, src, b, fields, regs, [], 4); return []
                case "F32Imm": fields[src] = b; return []
                case "F64Imm": fields[src] = (b >> 16) & 0xffff; fields[src + "1"] = b & 0xffff; return []
                case "C": fields.update(Sb_bank=2, Sb_addr=0x100); return [cbank(2, 0x100, b)]
                case "CX":
                    cx_fields, cx_regs, memory = cx_source(c.arch, k, src, b); fields.update(cx_fields); regs.update(cx_regs); return memory
        a, b = 0x44003e00, 0x42004000  # A lanes 1.5/4.0, B lanes 2.0/3.0
        fields = dict(Rd=8, Ra=2, Pu=2, Pv=3, isA="IS_A", ofmt="F16_V2", ftz="noftz", nan="nonan", Pp="PT", iswzA="H1_H0")
        if "iswzB" in k.operand_types: fields["iswzB"] = "H1_H0"
        if "xorsign" in k.operand_types: fields["xorsign"] = "noxorsign"
        regs = {"R2": a}
        memory = operand(b, fields, regs)
        if memory is None: continue
        for is_min, flags in ((True, (1, 0)), (False, (0, 1))):
            c.add(k.name, dict(fields, **{"Pp@not": int(not is_min)}), dict(regs),
                  {"R8": half_minmax(a, b, is_min), "P2": flags[0], "P3": flags[1], "__native": True}, memory=memory)
        # Source flags on one-NaN inputs, and the canonical result on two NaNs.
        for av, bv, want, flags in ((0x7e007e00, 0x7e007e00, 0x7fff7fff, (1, 1)), (0x7e007e00, 0x3c003c00, 0x3c003c00, (0, 0)),
                                    (0x3c003c00, 0x7e007e00, 0x3c003c00, (1, 1))):
            nan_fields, nan_regs = dict(fields), dict(regs, R2=av)
            nan_memory = [ram(0x2000, bv)] if typ == "CX" else operand(bv, nan_fields, nan_regs)
            for is_min in (True, False):
                c.add(k.name, dict(nan_fields, **{"Pp@not": int(not is_min)}), dict(nan_regs),
                      {"R8": want, "P2": flags[0], "P3": flags[1], "__native": True}, memory=nan_memory)
        c.add(k.name, dict(fields, nan="NAN", **{"Pp@not": 1}), dict(regs), {"error": "sass_opaque_HMNMX2"}, memory=memory)


def widening(c):
    # HADD2.F32 adds two half lanes, takes the selected low result and widens it to F32. Its
    # first operand is -RZ; the second source's selector chooses the half for the low result.
    values = [0, 0x80000000, 0x00010001, 0x80010001, 0x03ff0400, 0x040003ff, 0x35553aaa, 0x3c004000, 0xbc00c000, 0x7bff3c00, 0x7c003c00, 0x3c007c00, 0x7e003c00]
    for k in c.of("HADD2", "hadd2_F32", cx=None):
        if any(a.type == "CX" for a in k.operand_types.values()):
            c.add(k.name, dict(Rd=8, Ra="RZ", URc=4, Sc_offset=0, ofmt="F32", **{"Ra@negate": 1}), cx_header(c.arch, 4, 0x1000),
                  {"R8": 0x3f800000, "__native": True}, memory=[ram(0x1000, 0x3c003c00)])
            if c.fp == "readable":
                cx_fields, cx_regs, memory = cx_source(c.arch, k, cx_name(k), 0x3c003c00)
                c.add(k.name, {"Rd": 8, "Ra": "RZ", "ofmt": "F32", "Ra@negate": 1, **cx_fields}, cx_regs, {"R8": 0x3f800000, "__native": True},
                      memory=memory, portable=True)
            continue
        src = source(k, "Rc", "URc", "Sc"); typ = k.operand_types[src].type
        swizzle_name = source(k, "iswzB_as_C", "iswzB")
        # Attribute modifiers live in the rule list, not operand_types.
        attrs = {(r[1], r[2]) for _, r in k.rules if r[0] == "attr"}
        for ftz, sat, swizzle, absolute, negate, value in product(("noftz", "FTZ"), ("nosat", "SAT"), SWIZZLES if swizzle_name else ("H1_H0",),
                                                                 (0, 1), (0, 1), values):
            fields, regs, memory = {"Rd": 8, "Ra": "RZ", "ofmt": "F32", "ftz": ftz, "sat": sat, "iswzA": "H1_H0", "Ra@negate": 1}, {}, []
            source_value = value
            if absolute: source_value &= 0x7fff7fff
            if negate: source_value ^= 0x80008000
            if typ == "F16Imm": fields[src] = source_value = value & 0xffff
            else: put(k, src, value, fields, regs, memory, 4, bank=(2, 0x100), imm=())
            if (src, "absolute") in attrs: fields[src + "@absolute"] = absolute
            if (src, "negate") in attrs: fields[src + "@negate"] = negate
            if swizzle_name: fields[swizzle_name] = swizzle
            lane = (source_value >> 16) & 0xffff if swizzle == "H1_H1" else source_value & 0xffff
            want = widen_half(softfloat.arithmetic("add", [0x8000, lane], 16, ftz=ftz == "FTZ" and c.fp != "readable", sat=sat == "SAT"))
            if want is not None: c.add(k.name, fields, regs, {"R8": want, "__native": True}, memory=memory)
        # A 16-bit scalar literal occupies the low lane in this one-use form.
        if typ == "F16Imm":
            for half in (0x0001, 0x3c00, 0x8000, 0xbc00):
                c.add(k.name, {"Rd": 8, "Ra": "RZ", "Sc": half, "ofmt": "F32", "ftz": "noftz", "sat": "nosat", "iswzA": "H1_H0", "Ra@negate": 1}, {},
                      {"R8": widen_half(softfloat.arithmetic("add", [0x8000, half], 16)), "__native": True})
    if "hfma2_mma__RRI_norelu" in c.classes:
        canonical = lambda h: 0x7fff if h & 0x7c00 == 0x7c00 and h & 0x3ff else h
        for bits in (0x3c004000, 0x80000000, 0x7e017d01, 0x000103ff):
            c.add("hfma2_mma__RRI_norelu", dict(Rd=8, Ra="RZ", Rb="RZ", Sc=bits >> 16, Sc1=bits & 0xffff, **{"Ra@negate": 1}), {},
                  dict(R8=canonical(bits & 0xffff) | (canonical(bits >> 16) << 16), __max_ops=1))
        c.add("hfma2_mma__RRI_norelu", dict(Rd=8, Ra=2, Rb="RZ", Sc=0x3c00, Sc1=0x4000, **{"Ra@negate": 1}), {}, dict(error="sass_opaque_HFMA2_MMA"))


def readable_formulas(c):
    if c.fp != "readable": return
    rng = random.Random("readable_fp16")
    packed = [0x3c004000, 0x00010001, 0x7bfffbff, 0xbc00c000] + [rng.getrandbits(32) for _ in range(10)]
    for i, x in enumerate(packed):
        y, z = packed[(i * 7 + 3) % len(packed)], packed[(i * 5 + 1) % len(packed)]
        for op, cls, fields in [("add", "hadd2__RR", dict(Rd=2, Ra=2, Rc=4, ftz="FTZ")), ("mul", "hmul2__RR", dict(Rd=2, Ra=2, Rb=4)),
                                ("fma", "hfma2__RRR", dict(Rd=2, Ra=2, Rb=4, Rc=6, fmz="FMZ"))]:
            inputs = [x, y, z][:3 if op == "fma" else 2]
            out = [readable(op, [v >> (16 * lane) & 0xffff for v in inputs], 16) for lane in (0, 1)]
            if None not in out: c.add(cls, fields, dict(R2=x, R4=y, R6=z), dict(R2=out[0] | out[1] << 16, __native=True), portable=True)


def emulator_cases(c):
    arithmetic(c); minmax(c); widening(c); readable_formulas(c)


def hmnmx2_reference(family):
    """Expected packed result for scalar HMNMX2 family `family` (XORSIGN RIR forms compare with 1.0)."""
    xorsign = family.startswith(("hmnmx2_xorsign", "hmnmx2_bf16_xorsign", "hmnmx2_e6m9_xorsign"))
    rir = family.startswith("hmnmx2_xorsign_rir")
    fmt = "BF16" if family.startswith("hmnmx2_bf16") else "E6M9" if family.startswith("hmnmx2_e6m9") else "F16"
    return lambda a, b, _: half_minmax(a, 0x3c003c00 if rir else b, family.endswith("_min") or xorsign, "_nan_" in family, "_ftz_" in family,
                                       xorsign, xorsign and (not rir or family == "hmnmx2_xorsign_rir"), xorsign and not rir, fmt)


def gpu_fixtures():
    from tests.harness import scalar
    from tests.harness.templates import cx_fixture, slot_fixture, rows
    from tests.harness.gpu import sm89
    out = [scalar.launch(f, lambda *v, op=f[1:4], sat=f.endswith("_sat"): half2(op, [lanes(x) for x in v[:3 if op == "fma" else 2]], sat=sat), same=same_halves)
           for f in ("hadd2", "hfma2", "hmul2", "hmul2_sat", "hadd2_sat", "hfma2_sat")]
    out += [scalar.launch(f, lambda a, b, _, mode=f[-3:].upper(): hmul2(a, b, mode), same=same_halves) for f in ("hmul2_ftz", "hmul2_fmz")]
    # HFMA2.MMA -RZ, RZ, immediate: each lane is -0 * +0 + the immediate lane.
    out += [scalar.launch(f, lambda *_, bits=bits: half2("fma", [[0x8000] * 2, [0] * 2, lanes(bits)]), same=same_halves)
            for f, bits in [("hfma2_move", 0x3c004000), *((f, scalar.PATCHES[f][2]["Sc"] << 16 | scalar.PATCHES[f][2]["Sc1"]) for f in scalar.PATCHES if f.startswith("hfma2_move_"))]]
    out += [scalar.launch("hadd2_lanes", lambda a, b, _: half2("add", [lanes(a, "H0_H0", absolute=True), lanes(b, "H1_H1", negate=True)]), same=same_halves),
            scalar.launch("hfma2_lanes", lambda a, b, c: half2("fma", [lanes(a, "H1_H1", negate=True), lanes(b, "H0_H0", absolute=True), lanes(c)]), same=same_halves),
            scalar.launch("hmul2_lanes", lambda a, b, _: half2("mul", [lanes(a, "H0_H0", absolute=True), lanes(b, "H1_H1", negate=True)]), same=same_halves)]
    out += [scalar.launch(f, hmnmx2_reference(f)) for f in scalar.TARGETS if f.startswith("hmnmx2_")]
    # HMNMX2 with a CX source B: per-format edges in A, finite and NaN B words.
    masks = {"F16_V2": (0x7c00, 0x03ff), "BF16_V2": (0x7f80, 0x007f), "E6M9_V2": (0x7e00, 0x01ff)}
    edges = {"F16_V2": [0, 0x8000, 1, 0x8001, 0x03ff, 0x83ff, 0x0400, 0x8400, 0x3c00, 0xbc00, 0x7c00, 0xfc00, 0x7e01, 0x7c01],
             "BF16_V2": [0, 0x8000, 1, 0x8001, 0x007f, 0x807f, 0x0080, 0x8080, 0x3f80, 0xbf80, 0x7f80, 0xff80, 0x7fc1, 0x7f81],
             "E6M9_V2": [0, 0x8000, 1, 0x8001, 0x01ff, 0x81ff, 0x0200, 0x8200, 0x3c00, 0xbc00, 0x7e00, 0xfe00, 0x7e01, 0x7e02]}
    specs = [(fmt, ftz, nan, xorsign, is_min, b) for fmt in masks for ftz in ((False, True) if fmt == "F16_V2" else (False,)) for nan in (False, True)
             for xorsign in (False, True) for is_min in (True, False)
             for b in ({"F16_V2": 0x3c003c00, "BF16_V2": 0x3f803f80, "E6M9_V2": 0x3c003c00}[fmt], {"F16_V2": 0x7e017e01, "BF16_V2": 0x7fc17fc1, "E6M9_V2": 0x7e017e01}[fmt])]
    for i, (fmt, ftz, nan, xorsign, is_min, b) in enumerate(specs):
        rng, e = random.Random(0x2c0 + i), edges[fmt]
        values = [e[j % len(e)] | (e[(j * 5 + 3) % len(e)] << 16) for j in range(16)]
        while len(values) < 128:
            pair = []
            for _ in range(2):
                x = rng.getrandbits(16)
                while x & masks[fmt][0] == masks[fmt][0]: x = rng.getrandbits(16)
                pair.append(x)
            values.append(pair[0] | (pair[1] << 16))
        fields = dict(Rd=7, Ra=2, ofmt=fmt, ftz="FTZ" if ftz else "noftz", nan="NAN" if nan else "nonan", xorsign="XORSIGN" if xorsign else "noxorsign",
                      iswzA="H1_H0", iswzB="H1_H0", Pg=7, Pp=7, **{"Pp@not": 0 if is_min else 1}, **({"Ra@absolute": 1, "Sb@absolute": 1} if xorsign else {}))
        out.append(cx_fixture(f"hmnmx2_cx_{i}", "hmnmx2__RCxR", fields, [(a, b, 0) for a in values],
                              [half_minmax(a, b, is_min, nan, ftz, xorsign, fmt=fmt[:-3]) for a in values]))
    # HADD2.F32 R11, -RZ, R6 at the carry kernel's placeholder; the emulator runs it without barriers.
    halves = [0x0000, 0x8000, 0x0001, 0x8001, 0x03ff, 0x0400, 0x1400, 0x3555, 0x3800, 0x3c00, 0x3e00, 0x4000, 0xbc00, 0xc000, 0x7bff]
    packed = [halves[i % len(halves)] | (halves[(i * 7 + 3) % len(halves)] << 16) for i in range(128)]
    for ftz, sat, selector, absolute, negate in product(("noftz", "FTZ"), ("nosat", "SAT"), SWIZZLES, (0, 1), (0, 1)):
        fields = dict(Rd=11, Ra="RZ", Rc=6, ofmt="F32", ftz=ftz, sat=sat, iswzA="H1_H0", iswzB_as_C=selector, **{"Ra@negate": 1, "Rc@absolute": absolute, "Rc@negate": negate})
        lane = lambda x: lanes(x, selector, absolute, negate)[0]
        want = [widen_half(softfloat.arithmetic("add", [0x8000, lane(x)], 16, ftz=ftz == "FTZ", sat=sat == "SAT")) for x in packed]
        out.append(slot_fixture(f"hadd2_f32_{ftz}_{sat}_{selector}_{absolute}_{negate}", [], [(i, ~i & 0xffffffff, x) for i, x in enumerate(packed)], want, 0x180,
                                final=sm89("hadd2_F32__RR", **fields), nobarrier_emulation=True))
    return out
