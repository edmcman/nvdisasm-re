"""BMSK: direct operand forms and CX (bounded constant-descriptor) sources."""
import itertools, random
from tests.harness.case import cbank, cx_header, ram
from tests.refs.integer import bmsk

CX_CLASS = "bmsk__RCxR_RCxR"
CX_ARCHES = ("SM75", "SM80", "SM86", "SM89", "SM90")
CX_WIDTHS = [0, 1, 2, 15, 31, 32, 33, 63, 64, 0x7fffffff, 0x80000000, 0xffffffff]


def direct(c):
    edges = [0, 1, 15, 30, 31, 32, 33, 63, 64, 0x7fffffff, 0x80000000, 0xffffffff]
    rng = random.Random(18)
    varied = list(itertools.product(edges, repeat=2)) + [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(32)]
    for cls, shape in [("bmsk__RRR_RRR", "R"), ("bmsk__RUR_RUR", "UR"), ("bmsk__RuIR_RIR", "I"), ("bmsk__RCR_RCR", "C")]:
        vectors = list(itertools.product(range(34), repeat=2)) + varied if shape == "R" else varied
        for mode in ("C", "W"):
            for i, (a, n) in enumerate(vectors):
                rd = (2, 4, 8)[i % 3]; fields = dict(Rd=rd, Ra=2, cw=mode); regs = dict(R2=a); extras = {}
                match shape:
                    case "R": fields["Rb"] = 4; regs["R4"] = n
                    case "UR": fields["URb"] = 4; regs["UR4"] = n
                    case "I": fields["Sb"] = n
                    case "C": fields.update(Sb_bank=2, Sb_addr=16); extras["memory"] = [cbank(2, 16, n)]
                c.add(cls, fields, regs, {f"R{rd}": bmsk(a, n, mode), "__native": True}, inspect_pcode=True, **extras)
    for mode in ("C", "W"):
        for a, n in [(0, 0), (31, 32), (32, 33), (0xffffffff, 0xffffffff)]:
            c.add("bmsk__RRR_RRR", dict(Rd=8, Ra="RZ", Rb=4, cw=mode), dict(R4=n), dict(R8=bmsk(0, n, mode), __native=True), inspect_pcode=True)
            c.add("bmsk__RRR_RRR", dict(Rd=8, Ra=2, Rb="RZ", cw=mode), dict(R2=a), dict(R8=0, __native=True), inspect_pcode=True)
        c.add("bmsk__RUR_RUR", dict(Rd=8, Ra=2, URb="URZ", cw=mode), dict(R2=1), dict(R8=0, __native=True), inspect_pcode=True)
        c.add("bmsk__RRR_RRR", dict(Rd=8, Ra=2, Rb=2, cw=mode), dict(R2=31), dict(R8=bmsk(31, 31, mode), __native=True), inspect_pcode=True)
        c.add("bmsk__RRR_RRR", dict(Rd="RZ", Ra=2, Rb=4, cw=mode), dict(R2=1, R4=2), dict(RZ=0), inspect_pcode=True)
        for pred, invert in itertools.product((0, 1), repeat=2):
            c.add("bmsk__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, cw=mode, Pg=0, **{"Pg@not": invert}),
                  dict(R2=1, R4=3, R8=0xdeadbeef, P0=pred), dict(R8=bmsk(1, 3, mode) if pred ^ invert else 0xdeadbeef, __native=True),
                  inspect_pcode=True)


def cx(c):
    """The CX header holds a base and a size in 16-byte units; loads past the size read 0."""
    if c.arch not in CX_ARCHES: return
    bits = 51 if c.arch == "SM90" else 45

    def add(mode, a, width, units, offset, base, ur=4, rd=8, pred=None):
        fields = dict(Rd=rd, Ra=2, URb=ur, Sb_offset=offset, cw=mode)
        regs = dict(cx_header(c.arch, ur, base, units), R2=a)
        expected = bmsk(a, width if offset + 4 <= units * 16 else 0, mode)
        if pred is not None:
            fields["Pg"] = 0; regs.update(P0=pred, R8=0xdeadbeef)
            if not pred: expected = 0xdeadbeef
        # Nonzero backing bytes beyond bounds detect an unconditional load.
        c.add(CX_CLASS, fields, regs, {f"R{rd}": expected, "__native": True}, memory=[ram(base + offset, width)])
    for mode, a, width in itertools.product(("C", "W"), [0, 1, 15, 31, 32, 33, 0xffffffff], CX_WIDTHS):
        add(mode, a, width, 128, 4, 0x1234567800, rd=2 if a & 1 else 8)
    for mode, units, offset in itertools.product(("C", "W"), [0, 1, 2, 3, (1 << (64 - bits)) - 1], [0, 4, 12, 16, 28, 32, 44, 48, 65532]):
        for base in [0x1000, 0x1234567800, (1 << (bits + (6 if c.arch == "SM90" else 4))) - 64]:
            add(mode, 1, 5, units, offset, base)
    for ur in (0, 2, 4, 30, 60): add("C", 1, 3, 1, 4, 0x1000, ur=ur)
    add("C", 1, 3, 0, 0, 0)
    for pred in (0, 1): add("C", 1, 3, 1, 4, 0x1000, pred=pred)


def emulator_cases(c):
    direct(c); cx(c)


def gpu_fixtures():
    """Compiled clamp/wrap kernels (differing only at bit 75) and the patched CX wrapper."""
    from sass.decode import ir
    from tests.harness.gpu import Fixture, Template, compile
    from tests.harness.templates import cx_fixture, rows
    image = compile("bmsk_semantics")
    rng = random.Random(18); edges = [0, 1, 15, 30, 31, 32, 33, 63, 64, 255, 256, 0x7fffffff, 0x80000000, 0xffffffff]
    vs = list(itertools.product(range(34), repeat=2)) + list(itertools.product(edges, repeat=2)) + [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(128)]
    vs += [(0, 0)] * (-len(vs) % 32)
    out, words = [], {}
    for mode in ("C", "W"):
        t = Template(image, "sem_bmsk_" + mode.lower())
        targets = [o for o in range(0, len(t.code), 16) if t.decoded(o).klass.mnemonic == "BMSK"]
        if len(targets) != 1 or t.decoded(targets[0]).env["cw"] != ir.load("SM89").enums["CWMode"][mode]:
            raise RuntimeError(f"{mode}: compiler did not emit exactly one BMSK in the requested mode")
        words[mode] = int.from_bytes(t.code[targets[0]:targets[0] + 16], "little")
        out.append(Fixture(name=f"bmsk_{mode.lower()}", image=image, kernel="sem_bmsk_" + mode.lower(), data=rows([(a, n, 0) for a, n in vs]),
                           expected=[bmsk(a, n, mode) for a, n in vs], visited=tuple(targets)))
    if words["C"] ^ words["W"] != 1 << 75: raise AssertionError("clamp/wrap fixtures must differ only at instruction bit 75")
    positions = list(range(34)) + [0x7fffffff, 0x80000000, 0xffffffff] + list(range(91))
    forms = [(mode, 128, 4, width) for mode, width in itertools.product(("C", "W"), CX_WIDTHS)]
    # Never perform an in-bounds access past the real input allocation.
    forms += [("C", units, offset, 5) for units, offset in itertools.product([0, 1, 2, 3, 128, 0x40000, 0x7ffff], [4, 16, 28, 40, 64, 65532])
              if not (offset >= len(positions) * 12 and offset < units * 16)]
    for mode, units, offset, width in forms:
        triples = [(a, width, 0) for a in positions]
        loaded = rows(triples)[offset:offset + 4] if offset + 4 <= units * 16 else b"\0\0\0\0"
        out.append(cx_fixture(f"bmsk_cx_{mode}_{units}_{offset}_{width}", CX_CLASS, dict(Rd=7, Ra=2, cw=mode), triples,
                              [bmsk(a, int.from_bytes(loaded.ljust(4, b"\0"), "little"), mode) for a in positions], units=units, offset=offset))
    return out
