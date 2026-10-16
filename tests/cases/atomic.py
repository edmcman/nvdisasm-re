"""Atomics: ATOMG/ATOMS/generic ATOM read-modify-writes and RED/REDG reductions, one thread per
cell (the value function; ordering between threads is not tested, docs/open.md)."""
from tests.cases.memory import GENERIC, LOCAL, SHARED, WINDOWS
from tests.harness.case import pair
from tests.refs import atomic, memory

CELL = 0x108   # Ra = 0x100 plus Ra_offset 8
INTEGERS = ("U32", "S32", "U64", "S64")
INTEGER = {32: [(0, 0), (1, 0xffffffff), (0x7fffffff, 1), (0x80000000, 0x7fffffff), (0xffffffff, 0xffffffff), (0x12345678, 0x0f0f0f0f)],
           64: [(0, 1), (1 << 63, (1 << 63) - 1), ((1 << 64) - 1, 1), (0x123456789abcdef0, 0x0fedcba987654321)]}
WRAPPING = [(0, 5), (4, 5), (5, 5), (6, 5), (0, 0), (0xffffffff, 0xffffffff), (3, 0)]   # INC/DEC around the bound b
OPS = ("ADD", "MIN", "MAX", "INC", "DEC", "AND", "OR", "XOR", "EXCH")
# (old, b, plain): plain pairs are ordinary values, which the readable lowering (no flushing) also gets right.
# NaN results are not emulator cases: the semantics don't model NaN payloads (docs/semantics.md), so
# those vectors (NAN_FLOATS) run only as GPU fixtures, whose NaN comparison is payload-blind.
FLOATS = {"F32.FTZ.RN": [(0x3f800000, 0x3f800000, True), (0x00000001, 0x3f800000, False), (0x80000000, 0x00000000, True),
                         (0x00800001, 0x80800000, False), (0x7f7fffff, 0x7f7fffff, False), (0xbf800000, 0x3fc00000, True)],
          "F64.RN": [(0x3ff0000000000000, 0x3ff0000000000000, True), (1, 0, False),
                     (0x7fefffffffffffff, 0x7fefffffffffffff, False), (0xbff0000000000000, 0x3ff8000000000000, True)],
          "F16x2.RN": [(0x3c003c00, 0x3c003c00, True), (0x3c000001, 0x3c000001, False),
                       (0x7bff3c00, 0x7bffbc00, False), (0xc0003800, 0x3c003800, True)]}
NAN_FLOATS = {"F32.FTZ.RN": [(0x7f800000, 0xff800000), (0x7fc00001, 0x3f800000)],
              "F64.RN": [(0x7ff0000000000000, 0xfff0000000000000), (0x7ff0000000000001, 0x3ff0000000000000), (0x3ff0000000000000, 0xfff4000000000002),
                         (0x7ff0000000000001, 0xfff8000000000002), (0xfff8000000000002, 0x7ff0000000000001)],
              "F16x2.RN": [(0x7c00fc00, 0xfc00fc00)]}
# (op, sz, the md rule's name): combinations its conditions reject on SM75-SM89 ("Invalid instruction
# combination (reason: ADD)" on SM89, "( rule name: ADD)" on SM75).
ILLEGAL = [("INC", "U64", "INC_DEC"), ("DEC", "S32", "INC_DEC"), ("ADD", "S64", "ADD)"), ("EXCH", "F32.FTZ.RN", "AND_OR_XOR_EXCH"),
           ("MIN", "F64.RN", "MIN_MAX"), ("SAFEADD", "U32", "SAFEADD")]
SPACE_OF = {"ATOMG": "ram", "RED": "ram", "REDG": "ram", "ATOMS": "shared"}


def classes(c, *names): return [c.classes[n] for n in names if n in c.classes]


def width(sz): return 64 if "64" in sz else 32


def cell(space, address, value, w): return dict(space=space, address=address, hex=value.to_bytes(w // 8, "little").hex())


def sizes(c, k, wanted): return [s for s in wanted if s in c.enum(k, "sz")]


def address_fields(k):
    """Ra = R2(:R3) and offset 8; global forms with a 64-bit (.E) address."""
    return dict(Ra=2, Ra_offset=8, **(dict(e="E") if "e" in k.operand_types else {}))


def expect(op, sz, old, b, rd=8):
    """Registers and the cell an atomic leaves (rd=None: no destination)."""
    stored, returned = atomic.rmw(op, sz, old, b)
    regs = {} if rd is None else (pair(f"R{rd}", returned) if width(sz) == 64 else {f"R{rd}": returned})
    return regs, stored


def one(c, k, op, sz, old, b, space, rd=8):
    w = width(sz)
    regs, stored = expect(op, sz, old, b, rd)
    f = dict(address_fields(k), Rb=4, op=op, sz=sz, **({"Rd": rd} if rd is not None else {}))
    c.add(k.name, f, dict(R2=0x100, R3=0, **(pair("R4", b) if w == 64 else {"R4": b})),
          dict(regs, __memory=cell(space, CELL, stored, w)["hex"], __events=1, __atomic=1),
          memory=[cell(space, CELL, old, w)], observe_memory=[dict(space=space, address=CELL, size=w // 8)])


def combinations(c, k, ops, szs):
    """The (op, sz) pairs of `ops` x `szs` the class defines and the md's conditions allow."""
    rd = {"Rd": 8} if "Rd" in k.operand_types else {}
    return [(op, sz) for op in ops if op in c.enum(k, "op") for sz in sizes(c, k, szs)
            if c.legal(k.name, dict(address_fields(k), Rb=4, op=op, sz=sz, **rd))]


def integers(c):
    """Every legal integer operation and size on ATOMG, ATOMS and RED/REDG."""
    for k in classes(c, "atomg__RaNonRZ", "atomg_int__RaNonRZ", "atoms__RaNonRZ", "red__RaNonRZ", "redg_int__RaNonRZ"):
        space, rd = SPACE_OF[k.mnemonic], 8 if "Rd" in k.operand_types else None
        for op, sz in combinations(c, k, OPS, INTEGERS):
            for old, b in WRAPPING if op in ("INC", "DEC") else INTEGER[width(sz)]:
                one(c, k, op, sz, old, b, space, rd)


def floats(c):
    """Float ADDs (exact lowering: every vector; readable: ordinary values only)."""
    for k in classes(c, "atomg__RaNonRZ", "atomg_fp__RaNonRZ", "red__RaNonRZ", "redg_fp__RaNonRZ"):
        rd = 8 if "Rd" in k.operand_types else None
        for _, sz in combinations(c, k, ("ADD",), FLOATS):
            for old, b, plain in FLOATS[sz]:
                if c.fp == "exact" or plain: one(c, k, "ADD", sz, old, b, "ram", rd)


def compare_and_swap(c):
    """CAS: Rb is the compare value and Rc the new one, register pairs for .64 (ptxas's lowering of
    atom.cas, tests/ptx/atom/); a miss leaves the cell and still returns it."""
    for k in classes(c, "atomg_cas__RaNonRZ", "atoms_cas__RaNonRZ_CAS", "atoms_cas__RaNonRZ"):
        space = SPACE_OF[k.mnemonic]
        for sz in sizes(c, k, ("U32", "64")):
            w = width(sz)
            for old, compare, new in ((5, 5, 9), (5, 4, 9), ((1 << w) - 1, (1 << w) - 1, 0), (0x1234, 0x1234, 0x1234)):
                stored, returned = atomic.cas(old, compare, new)
                regs = pair("R8", returned) if w == 64 else {"R8": returned}
                operands = {**(pair("R4", compare) | pair("R6", new) if w == 64 else dict(R4=compare, R6=new))}
                c.add(k.name, dict(address_fields(k), Rd=8, Rb=4, Rc=6, sz=sz), dict(R2=0x100, R3=0, **operands),
                      dict(regs, __memory=cell(space, CELL, stored, w)["hex"], __events=1, __atomic=1),
                      memory=[cell(space, CELL, old, w)], observe_memory=[dict(space=space, address=CELL, size=w // 8)])


def aliasing(c):
    """Rd naming Ra or Rb: every input is read before Rd is written; Rd = RZ still stores."""
    for k in classes(c, "atomg__RaNonRZ", "atomg_int__RaNonRZ"):
        stored, returned = atomic.rmw("ADD", "U32", 40, 2)
        c.add(k.name, dict(address_fields(k), Rd=2, Rb=4, op="ADD", sz="U32"), dict(R2=0x100, R3=0, R4=2),
              dict(R2=returned, __memory=cell("ram", CELL, stored, 32)["hex"], __atomic=1),
              memory=[cell("ram", CELL, 40, 32)], observe_memory=[dict(space="ram", address=CELL, size=4)])
        c.add(k.name, dict(address_fields(k), Rd=4, Rb=4, op="ADD", sz="U32"), dict(R2=0x100, R3=0, R4=2),
              dict(R4=returned, __memory=cell("ram", CELL, stored, 32)["hex"], __atomic=1),
              memory=[cell("ram", CELL, 40, 32)], observe_memory=[dict(space="ram", address=CELL, size=4)])
        c.add(k.name, dict(address_fields(k), Rd="RZ", Rb=4, op="ADD", sz="U32"), dict(R2=0x100, R3=0, R4=2),
              dict(__memory=cell("ram", CELL, stored, 32)["hex"], __atomic=1),
              memory=[cell("ram", CELL, 40, 32)], observe_memory=[dict(space="ram", address=CELL, size=4)])


def generic(c):
    """ATOM routes a generic address through the shared and local windows like LD/ST; the cell
    elsewhere (global at the raw address, the other window at the offset) is not touched."""
    for k in classes(c, "atom__RaNonRZ", "atom_int__RaNonRZ"):
        for addr in GENERIC:
            space, at = memory.generic(addr, SHARED, LOCAL)
            base = addr - 8
            for target in ("ram", "shared", "localmem"):
                observed = at if target != "ram" else addr
                hit = (target, observed) == (space, at)
                stored, returned = atomic.rmw("ADD", "U32", 40, 2)
                c.add(k.name, dict(address_fields(k), Rd=8, Rb=4, op="ADD", sz="U32"),
                      dict(WINDOWS, R2=base & 0xffffffff, R3=base >> 32, R4=2),
                      dict(R8=returned, __memory=cell(target, observed, stored if hit else 40, 32)["hex"], __atomic=3),
                      memory=[cell(s, a, 40, 32) for s, a in {(space, at), (target, observed)}],
                      observe_memory=[dict(space=target, address=observed, size=4)])


def address_forms(c):
    """ADD.U32 through each other address form: RZ plus an absolute offset, Ra plus URc, URc alone, and
    desc[URc][Ra] (a descriptor that doesn't change the access, SM89 GPU)."""
    forms = {"_RaRZ": (dict(Ra="RZ", Ra_offset=CELL), {}),
             "uniform__Ra64": (dict(Ra=2, input_reg_sz_64_dist="64", Ra_URc=4, Ra_offset=8), dict(R2=0x80, R3=0, UR4=0x80, UR5=0)),
             "uniform__RaRZ": (dict(Ra="RZ", Ra_URc=4, Ra_offset=8), dict(UR4=0x100, UR5=0)),
             "uniform__memdesc": (dict(Ra=2, input_reg_sz_64_dist="64", Ra_URc=4, Ra_offset=8), dict(R2=0x100, R3=0, UR4=0, UR5=0))}
    stored, returned = atomic.rmw("ADD", "U32", 40, 2)
    for prefix in ("atomg_", "atomg_int_", "red_", "redg_int_", "atom_", "atom_int_", "atoms_"):
        for form, (fields, regs) in forms.items():
            k = c.classes.get(prefix + form) or (c.classes.get("atoms_uniform_") if prefix == "atoms_" and form == "uniform__RaRZ" else None)
            if k is None: continue
            space = "shared" if k.mnemonic == "ATOMS" else "ram"
            f = dict(fields, Rb=6, op="ADD", sz="U32", **({"Rd": 8} if "Rd" in k.operand_types else {}),
                     **({"e": "E"} if "e" in k.operand_types else {}), **({"e_desc": "EXP"} if "e_desc" in k.operand_types else {}))
            f = {n: v for n, v in f.items() if n in k.operand_types}
            c.add(k.name, f, dict(WINDOWS, R6=2, **regs), dict({"R8": returned} if "Rd" in k.operand_types else {}, __memory=cell(space, CELL, stored, 32)["hex"],
                                                         __atomic=3 if k.mnemonic == "ATOM" else 1),
                  memory=[cell(space, CELL, 40, 32)], observe_memory=[dict(space=space, address=CELL, size=4)])


def unmodelled(c):
    """Legal encodings that stay opaque: SAFEADD, float MIN/MAX, a predicate result other than PT, CAST."""
    for k in classes(c, "atomg__RaNonRZ", "atomg_int__RaNonRZ", "atomg_fp__RaNonRZ"):
        for op, sz in combinations(c, k, ("SAFEADD", "MIN", "MAX"), ("U64", "F16x2.RN", "F32.FTZ.RN")):
            if op == "SAFEADD" or sz.startswith("F"):
                c.add(k.name, dict(address_fields(k), Rd=8, Rb=4, op=op, sz=sz), {}, dict(error="sass_opaque_ATOMG"))
    for k in classes(c, "atomg__RaNonRZ", "atomg_int__RaNonRZ"):
        c.add(k.name, dict(address_fields(k), Pu=0, Rd=8, Rb=4, op="ADD", sz="U32"), {}, dict(error="sass_opaque_ATOMG"))
    for k in classes(c, "atoms_cas__RaNonRZ_CAST", "atoms_cast_destRd__RaNonRZ"):
        c.add(k.name, dict(Ra=2, Ra_offset=8, Rd=8, Rb=4, Rc=6, sz="U32"), {}, dict(error="sass_opaque_ATOMS"))


def illegal(c):
    """Combinations the md's conditions reject must not decode (pydecode, and Ghidra's language)."""
    for k in classes(c, "atomg__RaNonRZ"):
        for op, sz, rule in ILLEGAL:
            if op in c.enum(k, "op") and sz in c.enum(k, "sz"):
                c.add(k.name, dict(address_fields(k), Rd=8, Rb=4, op=op, sz=sz), {}, dict(illegal=("Invalid instruction combination", rule)), observe=[])


# Kernel (tests/kernels/atomic_semantics.cu atom_<name>) -> (op, sz, SASS it must execute).
KERNELS = {**{f"{op.lower()}_{t}": (op, sz, f"ATOMG.E.{op}" + (".S32" if sz == "S32" and op in ("MIN", "MAX") else ""))
              for op, t, sz in [("ADD", "u32", "U32"), ("MIN", "s32", "S32"), ("MIN", "u32", "U32"), ("MAX", "s32", "S32"), ("MAX", "u32", "U32"),
                                ("INC", "u32", "U32"), ("DEC", "u32", "U32"), ("AND", "b32", "U32"), ("OR", "b32", "U32"), ("XOR", "b32", "U32"),
                                ("EXCH", "b32", "U32")]},
           "cas_b32": ("CAS", "U32", "ATOMG.E.CAS"), "add_f32": ("ADD", "F32.FTZ.RN", "ATOMG.E.ADD.F32.FTZ.RN"),
           "add_f16x2": ("ADD", "F16x2.RN", "ATOMG.E.ADD.F16x2.RN"),
           "red_add_u32": ("ADD", "U32", "RED.E.ADD"), "red_max_s32": ("MAX", "S32", "RED.E.MAX.S32"), "red_add_f32": ("ADD", "F32.FTZ.RN", "RED.E.ADD.F32.FTZ.RN"),
           "generic_add_u32": ("ADD", "U32", "ATOM.E.ADD"),
           "shared_add_u32": ("ADD", "U32", "ATOMS.ADD"), "shared_min_s32": ("MIN", "S32", "ATOMS.MIN.S32"), "shared_inc_u32": ("INC", "U32", "ATOMS.INC"),
           "shared_exch_b32": ("EXCH", "U32", "ATOMS.EXCH"), "shared_cas_b32": ("CAS", "U32", "ATOMS.CAS"),
           "add_u64": ("ADD", "U64", "ATOMG.E.ADD.64"), "min_s64": ("MIN", "S64", "ATOMG.E.MIN.S64"), "max_u64": ("MAX", "U64", "ATOMG.E.MAX.64"),
           "and_b64": ("AND", "U64", "ATOMG.E.AND.64"), "exch_b64": ("EXCH", "U64", "ATOMG.E.EXCH.64"), "cas_b64": ("CAS", "64", "ATOMG.E.CAS.64"),
           "red_add_f64": ("ADD", "F64.RN", "RED.E.ADD.F64.RN")}
K = 0x9e3779b97f4a7c15


def gpu_vectors(name, op, sz):
    """128 (a, b, c) triples: the emulator cases' edges first, then random ones (CAS: every other one a hit)."""
    import random
    rng, w = random.Random(f"atom_{name}"), width(sz)
    edges = [(a, b) for a, b, _ in FLOATS[sz]] + NAN_FLOATS[sz] if sz in FLOATS else \
        WRAPPING if op in ("INC", "DEC") else INTEGER[w]
    vs = [(a, b, rng.getrandbits(w)) for a, b in edges]
    bits = 8 if op in ("INC", "DEC") else w   # small values reach both sides of the bound
    vs += [(rng.getrandbits(bits), rng.getrandbits(bits), rng.getrandbits(w)) for _ in range(128 - len(vs))]
    if op == "CAS": vs = [(a, a if j % 2 else b, c) for j, (a, b, c) in enumerate(vs)]
    return vs


def gpu_output(name, op, sz, a, b, c):
    """The kernel's output for one thread: returned | final << 32 (64-bit: final + returned * K, F64 RED: final)."""
    final, returned = atomic.cas(a, b, c) if op == "CAS" else atomic.rmw(op, sz, a, b)
    if name.startswith("red_"): returned = a
    if width(sz) == 32: return returned | final << 32
    return final if name == "red_add_f64" else (final + returned * K) % (1 << 64)


def float_same(sz):
    """Emulated and reference outputs equal, the final cell compared NaN-blind (the semantics don't
    model NaN payloads, docs/semantics.md)."""
    from tests.harness.scalar import nan, same_halves
    match sz:
        case "F32.FTZ.RN": return lambda e, w: e & 0xffffffff == w & 0xffffffff and (e >> 32 == w >> 32 or nan(e >> 32, 32) and nan(w >> 32, 32))
        case "F16x2.RN": return lambda e, w: e & 0xffffffff == w & 0xffffffff and same_halves(e >> 32, w >> 32)
        case "F64.RN": return lambda e, w: e == w or nan(e, 64) and nan(w, 64)
    return lambda e, w: e == w


def gpu_fixtures():
    import struct
    from sass.semantics.operations import ghidra_lowers
    from tests.harness.gpu import Fixture, compile
    image = compile("atomic_semantics", "-diag-suppress=177,550")
    out = []
    for name, (op, sz, target) in KERNELS.items():
        vs, w = gpu_vectors(name, op, sz), width(sz)
        out.append(Fixture(name=f"atom_{name}", image=image, kernel=f"atom_{name}", data=b"".join(struct.pack("<QQQ" if w == 64 else "<III", *v) for v in vs),
                           expected=[gpu_output(name, op, sz, *v) for v in vs], input_size=w // 8, output_size=8, targets=(target,),
                           emulator_same=float_same(sz), vectors=vs,
                           modes=("exact",) if ghidra_lowers("SM89", target.split(".")[0]) else ()))   # SM89's language leaves it opaque
    return out


def emulator_cases(c):
    for build in (integers, floats, compare_and_swap, aliasing, generic, address_forms, unmodelled, illegal): build(c)
