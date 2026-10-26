"""Scalar integer and bit operations: IMAD/IADD3/LOP3/LEA/SHF/ISETP/PRMT, uniform aliases,
BREV/POPC/FLO/SGXT and MOV."""
import random
from tests.refs.bits import MASK32, sext
from tests.refs.integer import brev, flo, prmt
from tests.harness.manual import retained

@retained("Uniform arithmetic/move p-code cost contracts and zero/special registers.")
def arithmetic(c):
    # Mixed register banks and uniform aliases retain original md operand names.
    rng = random.Random("uniform")
    for i in range(12):
        a, b, d = [rng.getrandbits(32) for _ in range(3)]
        regs = dict(R2=a, R4=b, R6=d, UR2=a, UR4=b, UR6=d)
        common = dict(URd=2, URa=2, URb=4, URc=6)
        c.add("uiadd3__URURUR_URURUR", common, regs, dict(UR2=(a + b + d) & MASK32, __max_ops=2))
        c.add("uimad__URURUR_URURUR", dict(common, fmt="U32"), regs, dict(UR2=(a * b + d) & MASK32, __max_ops=2))
        c.add("umov__UR", dict(URd=2, URb=4), regs, dict(UR2=b, __max_ops=1))
        c.add("umov__UI", dict(URd=2, Sb=b), {}, dict(UR2=b, __max_ops=1))
    # URZ is 63 in SM75-SM90 but 255 in SM100+, where 63 is the ordinary UR63
    # (ptxas emits UIMAD.WIDE.U32 ..., URZ with the eight-bit encoding).
    a, b, d = 0x9abcdef1, 0x87654321, 0x13579bdf
    urz = dict(UR4=a, UR5=b)
    c.add("uimad_wide__URURUR_URURUR", dict(URd=2, URa=4, URb=5, URc="URZ", fmt="U32"), urz, dict(UR2=a * b & MASK32, UR3=a * b >> 32, __native=True))
    c.add("uiadd3__URURUR_URURUR", dict(URd=2, URa=4, URb=5, URc="URZ"), urz, dict(UR2=(a + b) & MASK32, __native=True))
    if c.md.enums["UniformRegister"]["URZ"] != 63:
        c.add("uiadd3__URURUR_URURUR", dict(URd=2, URa=4, URb=5, URc=63), dict(urz, UR63=d), dict(UR2=(a + b + d) & MASK32, __native=True))


@retained("Explicit RZ/URZ encodings and p-code cost regressions are not ordinary value profiles.")
def permute_register_regressions(c):
    # Immediate selectors specialize each byte independently, including sign replication and RZ.
    for opcode in ('PRMT', 'UPRMT'):
        for k in c.of(opcode):
            if 'Sb' not in k.operand_types or k.operand_types['Sb'].type != 'UImm': continue
            uniform = opcode == 'UPRMT'
            dest, left, right = ('URd', 'URa', 'URc') if uniform else ('Rd', 'Ra', 'Rc')
            file, zero = ('UR', 'URZ') if uniform else ('R', 'RZ')
            modes = ('IDX',) if uniform else ('IDX', 'F4E', 'B4E', 'RC8', 'RC16', 'ECL', 'ECR')
            for mode in modes:
                for selector in [v * 0x1111 for v in range(16)] + [0x0123, 0x6420, 0x89ab, 0xffff7654]:
                    for zero_left, zero_right in ((False, True), (True, False), (True, True)):
                        a, b = (0 if zero_left else 0x80ff017f), (0 if zero_right else 0x55aa0081)
                        fields = {dest: 2, left: zero if zero_left else 2, right: zero if zero_right else 6, 'Sb': selector}
                        if not uniform: fields['pmode'] = mode
                        c.add(k.name, fields, {file + '2': a, file + '6': b},
                              {file + '2': prmt(a, selector, b, mode), '__native': True})


    # Byte reversal stays compact without copies solely for byte truncation.
    c.add("prmt__RuIR_RIR", dict(Rd=3, Ra=2, Sb=0x0123, Rc="RZ", pmode="IDX"),
          dict(R2=0x80ff017f), dict(R3=0x7f01ff80, __native=True, __max_ops=14))
    c.add("prmt__RuIR_RIR", dict(Rd=3, Ra=2, Sb=0x7654, Rc="RZ", pmode="IDX"),
          dict(R2=0x80ff017f), dict(R3=0, __native=True, __max_ops=1))


@retained("Special/zero/guard registers, p-code costs, partial MOV pixel masks, and hand-calculated signed/pair arithmetic regressions.")
def fixed(c):
    # Preserve the old mixed-bank comparison's constant predicate encoding; profiles
    # use writable predicate inputs, so a true value alone does not test PT.
    c.add("isetp__RUR_RUR_noEX", dict(Pu=0, Pv=1, Ra=2, URb=4, Pp="PT", icmp="LT", bop="AND", fmt="U32"),
          dict(R2=2, UR4=3), dict(P0=1, P1=0, __native=True))
    c.add("mov__RI", dict(Rd=3, Sb=0xdeadbeef, PixMaskU04=15), {}, dict(R3=0xdeadbeef, __max_ops=1))
    c.add("mov__RR", dict(Rd=3, Rb=2, PixMaskU04=15), dict(R2=0xdeadbeef), dict(R3=0xdeadbeef, __max_ops=1))
    # Compact p-code: ordinary forms lower to a fixed number of operations.
    for cls, fields, registers, result, limit in [
            ("iadd3_noimm__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6), dict(R2=1, R4=2, R6=3), 6, 2),
            ("imad__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32"), dict(R2=2, R4=3, R6=4), 10, 2),
            ("lop3_lut__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, imm8=0x96, Pp="PT"), dict(R2=1, R4=2, R6=4), 7, 3),
            ("shf__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32", dir="R", cw="W", hilo="LO"), dict(R2=3, R4=1, R6=2), 1, 8)]:
        c.add(cls, fields, registers, dict(R8=result, __max_ops=limit))
    c.add("mov__RI", dict(Rd=3, Sb=5, PixMaskU04=15, Pg=0), dict(P0=0, R3=9), dict(R3=9))
    c.add("mov__RI", dict(Rd="RZ", Sb=5, PixMaskU04=15), {}, dict(RZ=0, PT=1))
    c.add("mov__RI", dict(Rd=3, Sb=5, PixMaskU04=1), {}, dict(error="sass_opaque_MOV"))
    c.add("imad_wide__RRR_RRR", dict(Rd="RZ", Ra=2, Rb=4, Rc=6, fmt="U32"), dict(R2=3, R4=4, R6=5, R7=1), dict(RZ=0, PT=1))
    c.add("imad_wide__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc="RZ", fmt="U32"), dict(R2=3, R4=4), dict(R8=12, R9=0))
    c.add("imad_wide__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32"), dict(R2=0xffffffff, R4=2, R6=3, R7=1), dict(R8=1, R9=3))
    c.add("imad__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="S32"), dict(R2=0xfffffffe, R4=3, R6=1), dict(R8=0xfffffffb))
    c.add("imad_hi__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="S32"), dict(R2=0xfffffffe, R4=3, R6=1, R7=0), dict(R8=0xffffffff))
    c.add("iadd3_noimm__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, **{"Ra@negate": 1}), dict(R2=2, R4=3, R6=4), dict(R8=5))
    c.add("imad__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32", **{"Rc@negate": 1}), dict(R2=2, R4=3, R6=4), dict(R8=2))
    c.add("imad_hi__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32"), dict(R2=0xffffffff, R4=2, R6=3, R7=1), dict(R8=3))


@retained("Explicit constant-bank addresses and narrow HI carry boundaries guard the MD addend-width correction.")
def imad_constant(c):
    """IMAD HI/WIDE with a constant-bank addend: 64 bits wide (the md's SM75/SM80 32 is corrected
    in src/sass/md/fixes.py; ptxas adds 64-bit pointer parameters this way)."""
    from sass.semantics.family import predicates
    sizes = predicates(c.arch)
    width = sizes.get("imad_hi__RRC_RRC", next(v for k, v in sizes.items() if k.startswith("imad_hi__")))["ISRC_C_SIZE"]
    memory = [dict(space="cbank", address=(2 << 32) | 16, hex="ffffffff02000000")]
    c.add("imad_wide__RRC_RRC", dict(Rd=8, Pu=0, Ra=2, Rb=4, Sc_bank=2, Sc_addr=16, fmt="U32"),
          dict(R2=0xffffffff, R4=2), dict(R8=0xfffffffd, R9=4, P0=0), memory=memory)
    # A pointer parameter plus a scaled index, as ptxas emits it: the constant's high word counts.
    pointer = 0x00007f12_34560000
    c.add("imad_wide__RRC_RRC", dict(Rd=8, Pu=0, Ra=2, Rb=4, Sc_bank=0, Sc_addr=0x160, fmt="U32"),
          dict(R2=7, R4=4), dict(R8=(pointer + 28) & 0xffffffff, R9=(pointer + 28) >> 32, P0=0),
          memory=[dict(space="cbank", address=0x160, hex=pointer.to_bytes(8, "little").hex())])
    c.add("imad_hi__RRC_RRC", dict(Rd=8, Pu=0, Ra=2, Rb=4, Sc_bank=2, Sc_addr=16, fmt="U32"),
          dict(R2=0xffffffff, R4=2), dict(R8=4, P0=0) if width > 32 else dict(R8=0, P0=1), memory=memory)
    if width == 32:
        # Narrow HI addends include the encoded predicate carry input. With a zero product,
        # C=UINT32_MAX and Pp=0/1 distinguish both the result bit and the carry-out predicate.
        for carry, result, predicate in ((0, 0xffffffff, 0), (1, 0, 1)):
            c.add("imad_hi_x__RRC_RRC", dict(Rd=8, Pu=0, Ra=2, Rb=4, Sc_bank=2, Sc_addr=16, fmt="U32", Pp=1),
                  dict(R2=0, R4=0, P1=carry), dict(R8=result, P0=predicate, __native=True),
                  memory=[dict(space="cbank", address=(2 << 32) | 16, hex="ffffffff")])


@retained("Constant PT/UPT inputs with live predicate outputs supplement the generated register-predicate cases.")
def logic_constants(c):
    for cls, prefix in (('lop3_lut__RRR_RRR', ''), ('ulop3_lut__URURUR_URURUR', 'U')):
        for invert in (0, 1):
            fields = {prefix+'Rd':2, prefix+'Ra':2, prefix+'Rb':4, prefix+'Rc':6,
                      prefix+'Pu':0, prefix+'Pp':prefix+'PT', prefix+'Pp@not':invert, 'imm8':0x96, 'pop':'PAND'}
            c.add(cls, fields, {prefix+'R2':1, prefix+'R4':2, prefix+'R6':4},
                  {prefix+'R2':7, prefix+'P0':1-invert, '__native':True})


@retained("SM107/SM120 IMNMX adds predicate operands/results whose behavior remains explicitly opaque.")
def opaque_imnmx(c):
    if "Pq" in c.classes["imnmx__RRR_RRR"].operand_types:
        c.add("imnmx__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Pp=0, fmt="U32"), dict(R2=1, R4=2, P0=1),
              dict(error="sass_opaque_IMNMX"))


def emulator_cases(c):
    for part in (arithmetic, permute_register_regressions, fixed, imad_constant, logic_constants, opaque_imnmx): part(c)


def gpu_fixtures(arch="SM89"):
    from tests.harness import scalar
    return [scalar.launch("iadd3", lambda a, b, c: (a + b + c) & MASK32, arch=arch),
            scalar.launch("imad", lambda a, b, c: (a * b + c) & MASK32, arch=arch),
            scalar.launch("imad_hi", lambda a, b, c: ((a * b >> 32) + c) & MASK32, arch=arch),
            scalar.launch("lop3", lambda a, b, c: a ^ b ^ c, arch=arch),
            scalar.launch("shf", lambda a, b, c: ((c << 32 | a) >> (b & 31)) & MASK32, arch=arch),
            scalar.launch("iabs", lambda a, *_: abs(sext(a, 32)) & MASK32, arch=arch),
            scalar.launch("imnmx", lambda a, b, _: min(a, b), arch=arch),
            scalar.launch("prmt", lambda a, b, c: prmt(a, c, b, "IDX"), arch=arch),
            scalar.launch("isetp", lambda a, b, _: int(a < b), arch=arch),
            scalar.launch("sel", lambda a, b, c: a if c else b, arch=arch),
            # The 64-bit constant source joins the full product-plus-addend sum before the high word.
            scalar.launch("imad_hi_c", lambda x, y, _: ((x * y + 0x00000002ffffffff) >> 32) & MASK32, arch=arch),
            scalar.launch("isetp_ex", lambda x, y, _: int(x < y), arch=arch),
            scalar.launch("brev", lambda x, *_: brev(x), arch=arch), scalar.launch("popc", lambda x, *_: x.bit_count(), arch=arch),
            scalar.launch("bfind", lambda x, *_: flo(x, False), arch=arch), scalar.launch("bfind_shift", lambda x, *_: flo(x, True), arch=arch),
            scalar.launch("bfind_s32", lambda x, *_: flo(x, False, True), arch=arch), scalar.launch("bfind_shift_s32", lambda x, *_: flo(x, True, True), arch=arch)]
