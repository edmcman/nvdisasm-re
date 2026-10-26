"""Packed integer lanes: IDP.4A/2A dot products, VABSDIFF4, VIMNMX, VIADDMNMX, VIMNMX3, VISET,
including CX (constant-descriptor) source forms."""
import random
from tests.harness.case import cx_header, cx_source, ram, source
from tests.refs.bits import MASK32
from tests.harness.manual import retained
from tests.refs.integer import dot2, dot4, vabsdiff4, viaddmnmx, vminmax

def cx_name(k):
    return next(n for n, a in k.operand_types.items() if a.type == "CX")


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


@retained("Constant PT inputs and explicit CX descriptor regressions supplement generated register-predicate profiles.")
def minmax(c):
    for k in (k for k in c.of("VIMNMX", cx=True) if "pred" not in k.name):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        a, b = 0xfffffffe, 3
        fields = {"Rd": 8, "Ra": 2, "fmt": "S32", "relu": "norelu", "Pp": "PT"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b); fields.update(cx_fields)
        c.add(k.name, fields, {"R2": a, **cx_regs}, {"R8": vminmax((a, b), 32, True, True), "__native": True}, memory=memory)
    for k in c.of("VIADDMNMX", cx=True):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        a, b, bound = 0x7fffffff, 2, 0x80000000
        fields = {"Rd": 8, "Ra": 2, "Rc": 6, "fmt": "U32", "relu": "norelu", "Pp": "PT"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b); fields.update(cx_fields)
        c.add(k.name, fields, {"R2": a, "R6": bound, **cx_regs}, {"R8": viaddmnmx(a, b, bound, 32, False, True), "__native": True}, memory=memory)
    for k in c.of("VIMNMX3", cx=True):
        name = cx_name(k)
        if name not in ("Sb", "Rb"): continue
        a, b, d = 0xfffffffe, 3, 1
        fields = {"Rd": 8, "Ra": 2, "Rc": 6, "fmt": "S32", "relu": "norelu", "Pp": "PT"}
        cx_fields, cx_regs, memory = cx_source(c.arch, k, name, b); fields.update(cx_fields)
        c.add(k.name, fields, {"R2": a, "R6": d, **cx_regs}, {"R8": vminmax((a, b, d), 32, True, True), "__native": True}, memory=memory)
    # PT is a constant input, so exercise it directly rather than replacing it with P0.
    for op in ('VIMNMX', 'VIADDMNMX', 'VIMNMX3'):
        for k in c.of(op, op.lower() + '__RRR_RRR'):
            a, b, bound = 0x80007fff, 0x7fff8000, 0xffffffff
            for invert in (0, 1):
                fields = dict(Rd=2, Ra=4, Rb=6, fmt='S16x2', relu='RELU', Pp='PT')
                if op != 'VIMNMX': fields['Rc'] = 8
                fields.update({n: 'PT' for n in ('Pu', 'Pv') if n in k.operand_types})
                fields['Pp@not'] = invert
                want = (viaddmnmx(a, b, bound, 16, True, not invert, True) if op == 'VIADDMNMX'
                        else vminmax((a, b) if op == 'VIMNMX' else (a, b, bound), 16, True, not invert, True))
                c.add(k.name, fields, {'R4':a, 'R6':b, 'R8':bound}, {'R2':want, '__native':True})


def emulator_cases(c):
    cx_regressions(c); minmax(c)


def gpu_fixtures(arch="SM89"):
    from tests.harness import scalar
    from tests.harness.templates import cx_fixture
    out = [scalar.launch(f"vabsdiff4_{f}_{r}", lambda x, y, z, f=f, r=r: vabsdiff4(x, y, z, f == "s8", "ACC" if r == "acc" else "SIMD_MRG"), arch=arch)
           for f in ("s8", "u8") for r in ("merge", "acc")]
    out += [scalar.launch(f"idp2a_{m}_{a}{b}", lambda x, y, z, m=m, a=a, b=b: dot2(x, y, z, a == "s", b == "s", m == "hi"), arch=arch)
            for m in ("lo", "hi") for a in "su" for b in "su"]
    # CX source B: every thread reads the same descriptor word; Rc aliases Ra.
    rng = random.Random(0x1D0)
    vs = [(0x01020304, 0x05060708, 0x10203040), (0x7f80ff01, 0x80ff017f, 0xffffffff), (0, 0xffffffff, 0), (0xffffffff, 0xffffffff, 0x80000000)]
    vs += [(rng.getrandbits(32), rng.getrandbits(32), rng.getrandbits(32)) for _ in range(124)]
    cx = 0x80ff017f
    out += [cx_fixture(f"idp_cx_{a}_{b}", "idp_4a__CXb", dict(Rd=7, Ra=2, Rc=2, Pg=7, mode="4A", SrcAFmt=a, SrcBFmt=b),
                       [(v[0], cx, 0) for v in vs], [dot4(v[0], cx, v[0], a == "S8", b == "S8") for v in vs], arch=arch)
            for a in ("S8", "U8") for b in ("S8", "U8")]
    edges = (0, 0xffffffff, 0x807f01ff, 0xff010080, 0x7f80ff01, 0x01020304, 0x8000007f, 0x7f000080)
    values = [edges[i % len(edges)] ^ ((i * 0x01010101) & MASK32) for i in range(128)]
    out += [cx_fixture(f"vabsdiff4_cx_{f}_{red}_{cx:x}", "vabsdiff4__RCxR_RCxR", dict(Rd=7, Pu="PT", Ra=2, Rc=2, fmt=f, red=red, Pg=7),
                       [(a, cx, 0) for a in values], [vabsdiff4(a, cx, a, f == "S8", red) for a in values], arch=arch)
            for f in ("S8", "U8") for red in ("ACC", "SIMD_MRG") for cx in (0x807f01ff, 0xff010080, 0x7f80ff01, 0x01020304)]
    return out
