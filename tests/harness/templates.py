"""Patchable SM89 kernels: compiled wrappers whose reserved instruction slots receive patched words.

- CX wrapper (bmsk_cx_semantics.cu): loads one input word per thread, applies the instruction at
  0x110 with a CX source whose descriptor comes from the kernel's second parameter, stores R7.
- Carry slots (carry_semantics.cu): eight BAR slots at 0x100-0x170 and a placeholder IMAD at 0x180
  (Ra=R2, Rb=R5, Rc=R6, Rd=R11; R11 is stored).
"""
from functools import cache
from sass.decode import cubin
from tests.harness.gpu import ENCODING, Template, compile, sm89

SLOTS = tuple(range(0x100, 0x180, 16))
SCOREBOARDS = 0x3f << 116  # wait on every scoreboard (drains earlier loads)


@cache
def cx_wrapper():
    """The checked CX wrapper template and the scheduling controls used for patched words."""
    t = Template(compile("bmsk_cx_semantics"), "sem_cx")
    meta = cubin.kernel_image(t.image, "sem_cx")
    if meta["parameter_base"] != 0x160 or [p["size"] for p in meta["parameters"]] != [8, 8]: raise RuntimeError("unexpected CX wrapper parameter ABI")
    d = t.decoded
    expect = [all(d(o).klass.mnemonic == "BAR" for o in range(0xd0, 0x110, 16)), d(0x110).klass.name == "bmsk__RRR_RRR",
              all(d(0x110).env[n] == v for n, v in dict(Rd=0, Ra=2, Rb=5).items()),
              d(0x130).klass.mnemonic == "LOP3" and all(d(0x130).env[n] == v for n, v in dict(Rd=7, Ra=0, Rc=7).items()),
              d(0x140).klass.mnemonic == "STG" and d(0x140).env["Rb"] == 7, d(0x80).klass.mnemonic == "LDG" and d(0x80).env["Rd"] == 2,
              d(0x30).klass.mnemonic == "ULDC" and d(0x30).env["URd"] == 4 and d(0x30).env["Sa_bank"] == 0 and d(0x30).env["Sa_addr"] == 0x118,
              d(0x120).klass.mnemonic == "IMAD" and d(0x120).env["Rd"] == 4, d(0x150).klass.mnemonic == "EXIT"]
    if not all(expect): raise RuntimeError("compiler changed the pinned CX wrapper")
    # Clear the five-bit schedule mode, select WAIT15_END_GROUP and wait on all scoreboards.
    control = (t.control(0x110) & ~(31 << 105)) | (15 << 105) | SCOREBOARDS
    return t, control


def cx_image(cls, fields, units=128, offset=4, after=("nop_", {})):
    """CX wrapper with the descriptor built from c[0][0x160] (size `units` x 16 bytes) in UR0/UR1,
    `cls` at 0x110 reading source B at descriptor byte `offset`, and `after` at 0x130 (before the
    store of R7)."""
    t, control = cx_wrapper()
    words = {0xd0: ("uldc_const__RCR", dict(URd=0, Sa_bank=0, Sa_addr=0x160, sz="64")),
             0xe0: ("ushf__URuIUR_URIR", dict(URd=0, URa=0, Sb=4, URc=1, dir="R", fmt="U64", hilo="LO")),
             0xf0: ("ushf__URuIUR_URIR", dict(URd=1, URa="URZ", Sb=4, URc=1, dir="R", fmt="U32", hilo="HI")),
             0x100: ("ulop3_lut__URuIUR_URIR", dict(URd=1, URa=1, Sb=units << 13, URc="URZ", imm8=0xfc, UPp="UPT")),
             0x110: (cls, dict(fields, URb=0, Sb_offset=offset) if "Sb_offset" not in fields else fields),
             0x130: after}
    image = t.patch({o: (sm89(c, **f), control) for o, (c, f) in words.items()})
    if Template(image, "sem_cx").decoded(0x110).klass.name != cls: raise RuntimeError(f"{cls}: patched word decoded differently")
    return image


@cache
def carry_slots():
    t = Template(compile("carry_semantics"), "sem_probe")
    if [o for o in range(0, len(t.code), 16) if t.decoded(o).klass.mnemonic == "BAR"] != list(SLOTS): raise RuntimeError("compiler changed reserved probe slots")
    d = t.decoded(0x180)
    if d.klass.mnemonic != "IMAD" or any(d.env[n] != v for n, v in dict(Ra=2, Rb=5, Rc=6, Rd=11).items()):
        raise RuntimeError("compiler changed probe register assignment")
    return t


def slot_image(sequence, final=None):
    """Carry kernel with `sequence` (encodings) in the BAR slots, the first waiting on every
    scoreboard, and `final` (default NOP) replacing the placeholder at 0x180."""
    t = carry_slots(); control = t.control(0x180)
    words = {o: (w, control | SCOREBOARDS if i == 0 else control) for i, (o, w) in enumerate(zip(SLOTS, sequence))}
    words[0x180] = (sm89("nop_") if final is None else final, control)
    return t.patch(words)


def without_barriers(image):
    """The BAR slots replaced by NOPs (emulated threads are independent)."""
    t = carry_slots()
    return t.patch({o: (sm89("nop_"), t.control(o)) for o in SLOTS}, image)


def rows(triples):
    """Kernel input: one (a, b, c) 32-bit triple per thread."""
    import struct
    return b"".join(struct.pack("<III", *t) for t in triples)


def cx_fixture(name, cls, fields, triples, expected, units=128, offset=4, after=("nop_", {}), **kw):
    """A CX wrapper launch: thread i loads triples[i][0] into R2; the descriptor (c[0][0x160])
    points at the input array, so source B is the word at byte `offset` of the first record."""
    from tests.harness.gpu import Fixture
    return Fixture(name=name, image=cx_image(cls, fields, units, offset, after), kernel="sem_cx", data=rows(triples), expected=expected,
                   visited=(0x110,), modes=("exact", "readable"), **kw)


def slot_fixture(name, sequence, triples, expected, target, final=None, nobarrier_emulation=False, **kw):
    """A carry-kernel launch with `sequence` in the BAR slots; `target` must execute through EXIT."""
    from tests.harness.gpu import Fixture
    image = slot_image(sequence, final)
    return Fixture(name=name, image=image, kernel="sem_probe", data=rows(triples), expected=expected, visited=(target,), exit=True,
                   emulator_image=without_barriers(image) if nobarrier_emulation else None, **kw)
