"""Packed integer lanes: IDP.4A/2A dot products, VABSDIFF4, VIMNMX, VIADDMNMX, VIMNMX3, VISET,
including CX (constant-descriptor) source forms."""
import random
from tests.harness.case import cx_header, cx_source, put, ram, source, flagged
from tests.refs.bits import MASK32
from tests.harness.manual import retained
from tests.refs.integer import COMPARISONS, dot2, dot4, vabsdiff4, viaddmnmx, vminmax, vset_mask

FMTS = [("S32", 32, True), ("U32", 32, False), ("S16x2", 16, True), ("U16x2", 16, False), ("S8x4", 8, True), ("U8x4", 8, False)]


def cx_name(k):
    return next(n for n, a in k.operand_types.items() if a.type == "CX")


def relus(signed): return ("norelu", "RELU") if signed else ("norelu",)


@retained("Explicit CX header register/base regressions: the descriptor child is an address, not operand data.")
def cx_regressions(c):
    # CX children URb/Sb_offset describe a bounded constant-buffer address; the dot
    # product consumes the loaded 32-bit word from Sb.
    for k in c.of("IDP", "idp_4a__", cx=True)[:1]:
        c.add(k.name, dict(Rd=8, Ra=2, URb=4, Sb_offset=0, Rc=6, mode="4A", SrcAFmt="U8", SrcBFmt="U8"),
              {"R2": 0x04030201, "R6": 10, **cx_header(c.arch, 4, 0x1000)}, {"R8": 30, "__native": True},
              memory=[ram(0x1000, 0x01020304)])
    for k in c.of("IDP", "idp_2a__", cx=True):
        a, b, acc = 0x80017fff, 0x807f01ff, 0x12345678
        fields = {"Rd": 8, "Ra": 2, "Rc": 6, "mode": "2A.HI", "SrcAFmt": "S16", "SrcBFmt": "S8"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, cx_name(k), b); fields.update(cx_fields)
        c.add(k.name, fields, {"R2": a, "R6": acc, **cx_regs}, {"R8": dot2(a, b, acc, True, True, True), "__native": True}, memory=memory)


def vabsdiff(c):
    # VABSDIFF4 ACC sums the four byte differences plus Rc; SIMD_MRG packs them in byte lanes.
    words = [0, 0xffffffff, 0x807f01ff, 0xff010080, 0x7f80ff01, 0x01020304]
    for k in c.of("VABSDIFF4"):
        srcb, srcc = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        if not srcb or not srcc: continue
        for fmt in c.labels(k, "fmt", ("S8", "U8")):
            for red in c.labels(k, "red", ("SIMD_MRG", "ACC")):
                for j, a in enumerate(words):
                    b, acc = words[(j + 2) % len(words)], words[(j + 4) % len(words)]
                    rd = 2 if j % 2 == 0 else 8
                    fields, regs, memory = {"Rd": rd, "Ra": 4, "fmt": fmt, "red": red, "Pu": "PT"}, {"R4": a}, []
                    put(k, srcb, b, fields, regs, memory, 10, bank=(2, 16), imm=("UImm",))
                    put(k, srcc, acc, fields, regs, memory, 12, bank=(3, 24), imm=("UImm",))
                    c.add(k.name, fields, regs, {f"R{rd}": vabsdiff4(a, b, acc, fmt == "S8", red), "__native": True}, memory=memory)
    for k in c.of("VABSDIFF4", cx=True):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        a, b, acc = 0x807f01ff, 0xff010080, 0x13579bdf
        fields = {"Rd": 8, "Ra": 2, "fmt": "S8", "red": "ACC", "Pu": "PT"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b)
        fields.update(cx_fields); regs = {"R2": a, **cx_regs}
        other = source(k, "Rc", "URc", "Sc")
        if other and other != name:
            if k.operand_types[other].type not in ("Register", "UniformRegister"): continue
            put(k, other, acc, fields, regs, [], 6)
        c.add(k.name, fields, regs, {"R8": vabsdiff4(a, b, acc, True, "ACC"), "__native": True}, memory=memory)


def minmax(c):
    # CuLifter's VIMNMX is predicate-selected (PT: min) scalar or packed min/max.
    rng = random.Random("vimnmx")
    words = [0, 0xffffffff, 0x80007fff, 0x7fff8000, 0x12345678, 0xdeadbeef] + [rng.getrandbits(32) for _ in range(8)]
    for k in c.of("VIMNMX"):
        srcb = source(k, "Rb", "URb", "Sb")
        if not srcb: continue
        # vimnmx_pred's Pu/Pv (P0-P6, no PT) are written with unknown meaning: unmodelled.
        if any(p in k.operand_types and "PT" not in c.enum(k, p) for p in ("Pu", "Pv")): continue
        for fmt, width, signed in FMTS:
            if fmt not in c.enum(k, "fmt"): continue
            for relu in relus(signed):
                for is_min in (True, False):
                    for j, a in enumerate(words):
                        b = words[(j + 3) % len(words)]; rd = 2 if j % 2 else 8
                        fields = {"Rd": rd, "Ra": 4, "fmt": fmt, "relu": relu, "Pp": "PT" if is_min else "P0"}
                        for pred in ("Pu", "Pv"):
                            if pred in k.operand_types: fields[pred] = "PT"
                        regs, memory = {"R4": a}, []
                        put(k, srcb, b, fields, regs, memory, 10, bank=(0, 0), imm=("SImm", "UImm"))
                        if not is_min: regs["P0"] = 0
                        c.add(k.name, fields, regs, {f"R{rd}": vminmax((a, b), width, signed, is_min, relu == "RELU"), "__native": True}, memory=memory)
    for k in (k for k in c.of("VIMNMX", cx=True) if "pred" not in k.name):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        a, b = 0xfffffffe, 3
        fields = {"Rd": 8, "Ra": 2, "fmt": "S32", "relu": "norelu", "Pp": "PT"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b); fields.update(cx_fields)
        c.add(k.name, fields, {"R2": a, **cx_regs}, {"R8": vminmax((a, b), 32, True, True), "__native": True}, memory=memory)
    # CuLifter's VIADDMNMX adds modulo each lane width before min/max.
    for k in c.of("VIADDMNMX"):
        srcb, srcc = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        if not srcb or not srcc: continue
        for fmt, width, signed in FMTS[:4]:
            if fmt not in c.enum(k, "fmt"): continue
            for relu in relus(signed):
                for is_min in (True, False):
                    for j, a in enumerate(words):
                        b, bound = words[(j + 1) % len(words)], words[(j + 4) % len(words)]
                        negate = bool(j & 1 and flagged(k, srcb, "negate"))
                        rd = 2 if j % 2 else 8
                        fields, regs, memory = {"Rd": rd, "Ra": 4, "fmt": fmt, "relu": relu, "Pp": "PT" if is_min else "P0"}, {"R4": a}, []
                        put(k, srcb, b, fields, regs, memory, 10, bank=(0, 0), imm=("SImm", "UImm"))
                        put(k, srcc, bound, fields, regs, memory, 12, bank=(0, 4), imm=("SImm", "UImm"))
                        if negate: fields[srcb + "@negate"] = 1
                        if not is_min: regs["P0"] = 0
                        want = viaddmnmx(a, -b & MASK32 if negate else b, bound, width, signed, is_min, relu == "RELU")
                        c.add(k.name, fields, regs, {f"R{rd}": want, "__native": True}, memory=memory)
    for k in c.of("VIADDMNMX", cx=True):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        a, b, bound = 0x7fffffff, 2, 0x80000000
        fields = {"Rd": 8, "Ra": 2, "Rc": 6, "fmt": "U32", "relu": "norelu", "Pp": "PT"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b); fields.update(cx_fields)
        c.add(k.name, fields, {"R2": a, "R6": bound, **cx_regs}, {"R8": viaddmnmx(a, b, bound, 32, False, True), "__native": True}, memory=memory)
    # CuLifter's VIMNMX3 is a three-input lane-wise min/max; PT selects min and !PT max.
    rng = random.Random("vimnmx3")
    words = [0, 0xffffffff, 0x80007fff, 0x7fff8000, 0x12345678, 0xdeadbeef] + [rng.getrandbits(32) for _ in range(6)]
    for k in c.of("VIMNMX3"):
        srcb, srcc = source(k, "Rb", "URb", "Sb"), source(k, "Rc", "URc", "Sc")
        if not srcb or not srcc: continue
        for fmt, width, signed in FMTS[:4]:
            if fmt not in c.enum(k, "fmt"): continue
            for relu in relus(signed):
                for pred, pred_not, is_min in (("PT", 0, True), ("P0", 0, False), ("PT", 1, False)):
                    for j, a in enumerate(words):
                        b, d = words[(j + 2) % len(words)], words[(j + 4) % len(words)]
                        rd = 2 if j % 3 == 0 else 8
                        fields = {"Rd": rd, "Ra": 2, "Rc": 6, "fmt": fmt, "relu": relu, "Pp": pred, "Pp@not": pred_not}
                        regs, memory = {"R2": a, "R6": d}, []
                        put(k, srcb, b, fields, regs, memory, 10, bank=(0, 0), imm=("SImm", "UImm"))
                        if srcc != "Rc": put(k, srcc, d, fields, regs, memory, 12, bank=(0, 4), imm=("SImm", "UImm"))
                        if not is_min: regs["P0"] = 0
                        c.add(k.name, fields, regs, {f"R{rd}": vminmax((a, b, d), width, signed, is_min, relu == "RELU"), "__native": True},
                              memory=memory)
    for k in c.of("VIMNMX3", cx=True):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        a, b, d = 0xfffffffe, 3, 1
        fields = {"Rd": 8, "Ra": 2, "Rc": 6, "fmt": "S32", "relu": "norelu", "Pp": "PT"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b); fields.update(cx_fields)
        c.add(k.name, fields, {"R2": a, "R6": d, **cx_regs}, {"R8": vminmax((a, b, d), 32, True, True), "__native": True}, memory=memory)


def viset(c):
    """SM107's VISET.BM: lane masks of every comparison and format, from each B source kind."""
    rng = random.Random("viset")
    words = [0, 0xffffffff, 0x80007fff, 0x7fff8000, 0x7f80ff01, 0x12345678] + [rng.getrandbits(32) for _ in range(6)]
    for k in c.of("VISET"):
        srcb = source(k, "Rb", "URb", "Sb")
        for (fmt, width, signed), op in ((f, op) for f in FMTS for op in COMPARISONS):
            for j, a in enumerate(words):
                b = a if j % 4 == 0 else words[(j * 5 + 1) % len(words)]
                rd = 4 if j % 3 == 0 else 8
                fields, regs = {"Rd": rd, "Ra": 4, "fmt": fmt, "icmp": op}, {"R4": a}
                put(k, srcb, b, fields, regs, None, 6, ureg=10, imm=("UImm",))
                c.add(k.name, fields, regs, {f"R{rd}": vset_mask(a, b, width, signed, op), "__native": True})


def emulator_cases(c):
    cx_regressions(c); vabsdiff(c); minmax(c); viset(c)


def gpu_fixtures():
    from tests.harness import scalar
    from tests.harness.templates import cx_fixture
    out = [scalar.launch(f"vabsdiff4_{f}_{r}", lambda x, y, z, f=f, r=r: vabsdiff4(x, y, z, f == "s8", "ACC" if r == "acc" else "SIMD_MRG"))
           for f in ("s8", "u8") for r in ("merge", "acc")]
    out += [scalar.launch(f"idp2a_{m}_{a}{b}", lambda x, y, z, m=m, a=a, b=b: dot2(x, y, z, a == "s", b == "s", m == "hi"))
            for m in ("lo", "hi") for a in "su" for b in "su"]
    # CX source B: every thread reads the same descriptor word; Rc aliases Ra.
    rng = random.Random(0x1D0)
    vs = [(0x01020304, 0x05060708, 0x10203040), (0x7f80ff01, 0x80ff017f, 0xffffffff), (0, 0xffffffff, 0), (0xffffffff, 0xffffffff, 0x80000000)]
    vs += [(rng.getrandbits(32), rng.getrandbits(32), rng.getrandbits(32)) for _ in range(124)]
    cx = 0x80ff017f
    out += [cx_fixture(f"idp_cx_{a}_{b}", "idp_4a__CXb", dict(Rd=7, Ra=2, Rc=2, Pg=7, mode="4A", SrcAFmt=a, SrcBFmt=b),
                       [(v[0], cx, 0) for v in vs], [dot4(v[0], cx, v[0], a == "S8", b == "S8") for v in vs])
            for a in ("S8", "U8") for b in ("S8", "U8")]
    edges = (0, 0xffffffff, 0x807f01ff, 0xff010080, 0x7f80ff01, 0x01020304, 0x8000007f, 0x7f000080)
    values = [edges[i % len(edges)] ^ ((i * 0x01010101) & MASK32) for i in range(128)]
    out += [cx_fixture(f"vabsdiff4_cx_{f}_{red}_{cx:x}", "vabsdiff4__RCxR_RCxR", dict(Rd=7, Pu="PT", Ra=2, Rc=2, fmt=f, red=red, Pg=7),
                       [(a, cx, 0) for a in values], [vabsdiff4(a, cx, a, f == "S8", red) for a in values])
            for f in ("S8", "U8") for red in ("ACC", "SIMD_MRG") for cx in (0x807f01ff, 0xff010080, 0x7f80ff01, 0x01020304)]
    return out
